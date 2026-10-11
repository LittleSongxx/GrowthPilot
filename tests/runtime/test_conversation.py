"""验证会话 API 和 AgentScope 原生调度，不调用外部服务或模型。"""

import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest

from backend.agent_harness import BackendService, load_scenario
from backend.agent_harness.prompts import system_prompt
from backend.agent_harness.runtime.session_manager import SessionManager
from backend.api.app import create_app
from backend.settings import Settings
from tests.db import database_for


@pytest.fixture
async def setup(tmp_path):
    from pathlib import Path

    settings = Settings(
        project_root=Path(__file__).resolve().parents[2],
        runtime_dir=tmp_path,
        app_db=database_for(tmp_path / "app"),
        agent_api_key="",
        dashscope_api_key="",
        session_secret="session-test",
    )
    services = {}
    for path in (settings.project_root / "backend/scenarios").glob("*/scenario.yaml"):
        s = load_scenario(path)
        services[s.id] = BackendService(settings.app_db, s)
    host = SessionManager(settings, services)
    app = create_app(settings, host)
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787"
    )
    yield host, http, None
    await http.aclose()
    await host.close()


async def new_session(http, scenario="didi_driver", customer="test-customer", channel="text"):
    response = await http.post(
        "/api/sessions", json={"scenario_id": scenario, "customer_id": customer}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    return data["session_id"], {"Authorization": "Bearer " + data["token"]}, data


@pytest.mark.asyncio
async def test_active_customer_creation_has_actionable_error(setup):
    _, http, _ = setup
    await new_session(http)
    response = await http.post(
        "/api/sessions", json={"scenario_id": "didi_driver", "customer_id": "test-customer"}
    )
    assert response.status_code == 409
    assert "先停止原会话" in response.json()["detail"]
    assert "PermissionError" not in response.text


@pytest.mark.asyncio
async def test_retention_revokes_cached_runtime_http_access(setup):
    host, http, _ = setup
    sid, auth, _ = await new_session(http)
    rt = host.get(sid)
    await rt.stop("retention_test")
    expired_at = time.time() + 31 * 86400
    plan = rt.service.plan_retention(now=expired_at)
    claimed = rt.service.begin_retention(plan["plan_hash"], now=expired_at)
    assert claimed["status"] == "success"
    for endpoint in ["state", "events"]:
        response = await http.get(f"/api/sessions/{sid}/{endpoint}", headers=auth)
        assert response.status_code == 404
        assert response.json() == {"detail": "session_not_found"}
    response = await http.post(f"/api/sessions/{sid}/voice/start", headers=auth, json={})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_text_final_answer_excludes_intermediate_tool_narration(setup):
    host, http, _ = setup
    sid, _, _ = await new_session(http)
    rt = host.get(sid)
    await rt.events.process({"type": "REPLY_START", "reply_id": "r1", "role": "assistant"})
    await rt.events.process({"type": "MODEL_CALL_START", "reply_id": "r1"})
    await rt.events.process(
        {"type": "TEXT_BLOCK_DELTA", "reply_id": "r1", "delta": "I'll query tools first."}
    )
    await rt.events.process({"type": "MODEL_CALL_START", "reply_id": "r1"})
    await rt.events.process(
        {
            "type": "TEXT_BLOCK_DELTA",
            "reply_id": "r1",
            "delta": "可以按官方说明下载 App，操作遇到问题时告诉我页面提示。",
        }
    )
    await rt.events.process({"type": "REPLY_END", "reply_id": "r1", "finished_reason": "completed"})
    assert (
        rt.service.snapshot(rt.ctx)["messages"][-1]["text"]
        == "可以按官方说明下载 App，操作遇到问题时告诉我页面提示。"
    )
    assert any("I'll query" in str(event) for event in host.store.events(sid))


@pytest.mark.asyncio
async def test_usage_keeps_native_model_end_token_fields_and_unknown_null(setup):
    from agentscope.event import ModelCallEndEvent

    host, http, _ = setup
    sid, _, _ = await new_session(http)
    rt = host.get(sid)
    event = ModelCallEndEvent(
        reply_id="native-usage",
        input_tokens=123,
        output_tokens=45,
        cache_input_tokens=67,
        cache_creation_input_tokens=8,
    )
    await rt.events.process(event)
    usage = [e["data"] for e in host.store.events(sid) if e["type"] == "usage"][-1]
    assert {
        key: usage[key]
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_input_tokens",
            "cache_creation_input_tokens",
        )
    } == {
        "input_tokens": 123,
        "output_tokens": 45,
        "cache_input_tokens": 67,
        "cache_creation_input_tokens": 8,
    }
    assert usage["model_name"] is None and usage["duration"] is None
    assert usage["finished_reason"] == event.finished_reason.value
    await rt.events.process({"type": "MODEL_CALL_END", "reply_id": "missing-usage"})
    missing = [e["data"] for e in host.store.events(sid) if e["type"] == "usage"][-1]
    assert missing["input_tokens"] is None and missing["output_tokens"] is None


@pytest.mark.asyncio
async def test_voice_message_annotations_never_claim_complete_playback(setup):
    host, http, _ = setup
    sid, auth, _ = await new_session(http, channel="voice")
    rt = host.get(sid)
    for reply, reason in [("cut-off", "interrupted"), ("generated", "completed")]:
        await rt.events.process({"type": "REPLY_START", "reply_id": reply, "role": "assistant"})
        await rt.events.process(
            {"type": "TEXT_BLOCK_DELTA", "reply_id": reply, "delta": "这是生成的语音文字。"}
        )
        await rt.events.process({"type": "REPLY_END", "reply_id": reply, "finished_reason": reason})
    response = await http.get(f"/api/sessions/{sid}/state", headers=auth)
    replies = {m["reply_id"]: m for m in response.json()["messages"] if m["origin"] == "agent"}
    assert replies["cut-off"]["interrupted"] is True
    assert replies["generated"]["interrupted"] is False
    assert all(
        m["text_evidence"] == "generated_text" and m["playback_completed"] is None
        for m in replies.values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario_id", ["didi_driver", "meituan_rider", "meituan_merchant", "financial_consultation"]
)
async def test_native_model_payload_is_business_language_including_tool_results(
    setup, monkeypatch, scenario_id
):
    """在框架原生调度后捕获实际发给服务供应商的请求内容，不访问网络。"""
    from types import SimpleNamespace

    from agentscope.model import OpenAIChatModel
    from openai.types.chat import ChatCompletionChunk

    host, http, _ = setup
    host.settings = replace(host.settings, agent_api_key="test-no-network")
    service = host.services[scenario_id]
    previous = service.create_session("model-visible-audit")
    source = service.add_message(previous, "我最关心注册流程")["data"]["message_id"]
    service.write_memory(previous, "concern", "注册流程", source, "注册流程")
    service.end_session(previous)
    sid, _, _ = await new_session(http, scenario=scenario_id, customer="model-visible-audit")
    rt = host.get(sid)
    captures = []
    skill = rt.service.scenario.data["skills"][0]["id"]
    policy_calls = []
    dynamic_name, dynamic_args, expected_match_type = {
        "meituan_rider": ("search_nearby_stations", {"location": "长沙"}, "city_profile"),
        "didi_driver": (
            "search_driver_service_points",
            {"location": "云南省楚雄州南华县"},
            "generic_fallback",
        ),
        "meituan_merchant": (
            "query_delivery_options",
            {"area": "长沙", "store_category": "鲜花店"},
            "generic_fallback",
        ),
        "financial_consultation": (
            "query_product_options",
            {"purpose": "比较费用计息方式"},
            "generic_fallback",
        ),
    }[scenario_id]

    def search_policy(query, bound_scenario_id):
        policy_calls.append((query, bound_scenario_id))
        return {
            "status": "success",
            "data": {
                "query": query,
                "corpus_version": "capture-corpus",
                "clauses": [{"text": "来自检索的条款原文", "url": "https://www.meituan.com/"}],
            },
            "evidence": ["policy:captured-clause"],
            "retryable": False,
        }

    host._policy_index = SimpleNamespace(search=search_policy)

    class Stream:
        def __init__(self, chunk):
            self.chunk = chunk

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def __aiter__(self):
            async def chunks():
                yield self.chunk

            return chunks()

    async def create(**kwargs):
        captures.append(kwargs)
        if len(captures) == 1:
            calls = [
                {
                    "index": i,
                    "id": f"call-{i}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                for i, (name, args) in enumerate(
                    [
                        (dynamic_name, dynamic_args),
                        ("search_policy", {"query": "报名需要哪些材料？"}),
                        ("Skill", {"skill": skill}),
                    ]
                )
            ]
            delta, reason = {"tool_calls": calls}, "tool_calls"
        else:
            delta, reason = {"content": "我会依据实际资料说明，您现在方便交流吗？"}, "stop"
        return Stream(
            ChatCompletionChunk(
                id="capture",
                created=1,
                model="fake",
                object="chat.completion.chunk",
                choices=[{"index": 0, "delta": delta, "finish_reason": reason}],
            )
        )

    original = OpenAIChatModel.__init__

    def intercept(self, *args, **kwargs):
        original(self, *args, **kwargs)
        from unittest.mock import AsyncMock

        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock()
        )

    monkeypatch.setattr(OpenAIChatModel, "__init__", intercept)
    await rt.message("我想先了解流程。")
    assert len(captures) >= 2
    assert all("response_format" not in capture for capture in captures)
    # 原生模型格式化已合并系统提示、技能元数据、工具结构定义和工具结果。
    all_visible = json.dumps(captures, ensure_ascii=False)
    system_content = "\n".join(
        message["content"] for message in captures[0]["messages"] if message["role"] == "system"
    )
    assert system_content.startswith(system_prompt(rt.service.scenario, rt.business_summary()))
    assert '"profile"' in system_content and '"value": "注册流程"' in system_content
    for phrase in [
        '"workspace_id"',
        '"source_type"',
        '"business_data"',
        "query_operation",
        "prepare_guidance",
        "deliver_guidance",
        "end_session",
        "stop_contact",
    ]:
        assert phrase not in all_visible
    assert any(m.get("role") == "tool" for m in captures[-1]["messages"])
    assert policy_calls == [("报名需要哪些材料？", scenario_id)]
    assert "来自检索的条款原文" in all_visible and "capture-corpus" in all_visible
    assert "policy:captured-clause" in all_visible
    tools = {item["function"]["name"] for item in captures[0]["tools"]}
    assert "search_policy" in tools and "query_fact" not in tools
    assert '"facts"' not in all_visible
    events = host.store.events(sid, limit=1000)
    starts = [e["data"] for e in events if e["type"] == "tool_start"]
    results = [e["data"] for e in events if e["type"] == "tool_result"]
    assert {e["name"] for e in starts} == {"search_policy", "Skill", dynamic_name}
    assert {e["name"] for e in results} == {"search_policy", "Skill", dynamic_name}
    assert all(e["state"] == "success" for e in results)
    assert not {"read_memory", "write_memory"} & tools
    dynamic = next(e["result"] for e in results if e["name"] == dynamic_name)
    assert dynamic["status"] == "success"
    assert dynamic["data"]["items"]
    audit = next(
        e["data"]["result"]
        for e in events
        if e["type"] == "business_query_audit" and e["data"]["name"] == dynamic_name
    )
    assert audit["data"]["source_type"] == "local_catalog"
    assert audit["data"]["match_type"] == expected_match_type
    assert audit["data"]["template_provenance"]
    model_result = next(
        m
        for m in captures[-1]["messages"]
        if m.get("role") == "tool" and '"items"' in str(m.get("content"))
    )
    visible_result = json.loads(model_result["content"])
    assert visible_result == dynamic
    assert visible_result["data"]["requested_filters"] == audit["data"]["requested_filters"]
    assert visible_result["data"]["coverage_level"] == audit["data"]["coverage_level"]
    if scenario_id in {"meituan_rider", "didi_driver"}:
        contacts = [(item["address"], item["phone"]) for item in dynamic["data"]["items"]]
        assert contacts == [(item["address"], item["phone"]) for item in audit["data"]["items"]]
        assert all(address and len(phone) == 11 for address, phone in contacts)
        assert len(set(contacts)) == len(contacts)
        assert all(item["distance_meters"] is None for item in dynamic["data"]["items"])
    for hidden_key in [
        "source_type",
        "template_provenance",
        "match_type",
        "is_fallback",
        "disclosure",
        "pricing_basis",
    ]:
        assert f'"{hidden_key}"' not in model_result["content"]
    loaded = [e["data"] for e in events if e["type"] == "skill_loaded"]
    assert len(loaded) == 1 and loaded[0]["skill_id"] == skill
    import hashlib

    body = next(e["result"]["text"] for e in results if e["name"] == "Skill")
    assert loaded[0]["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert not runtime_private_hooks_present(rt)


@pytest.mark.asyncio
async def test_missing_policy_corpus_is_honest_and_does_not_load_embedding_model(
    setup, monkeypatch
):
    import backend.agent_harness.rag.index as knowledge

    def unexpected_model_load(*args, **kwargs):
        raise AssertionError("An empty knowledge base must not download or load a model")

    monkeypatch.setattr(knowledge, "_load_model", unexpected_model_load)
    host, http, _ = setup
    assert host._policy_index is None
    sid, _, _ = await new_session(http, scenario="meituan_rider", customer="empty-knowledge")
    runtime = host.get(sid)
    result = await runtime.tools["search_policy"](query="杭州骑手报名要交钱吗？")
    assert result["status"] == "no_data"
    assert result["data"]["reason"] == "knowledge_not_indexed"
    assert result["data"]["clauses"] == [] and result["evidence"] == []
    assert host.policy_index is host.policy_index
    assert host.policy_index.database == host.settings.app_db
    assert host.policy_index.status()["status"] == "no_data"
    with runtime.service._connection():
        assert runtime.service.db.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()["count"] == 0
        assert runtime.service.db.execute("SELECT COUNT(*) FROM rag_corpora").fetchone()["count"] == 0


@pytest.mark.asyncio
async def test_business_functions_preserve_host_close_and_session_deadline(setup):
    host, http, _ = setup
    sid, _, _ = await new_session(http, customer="tool-boundaries")
    runtime = host.get(sid)
    for _ in range(10):
        assert (await runtime.tools["search_policy"](query="报名条件"))["status"] == "no_data"
    with runtime.service._connection():
        runtime.service.db.execute("UPDATE sessions SET started=%s WHERE id=%s", (time.time() - 301, sid))
    assert (await runtime.tools["search_policy"](query="报名条件"))["data"][
        "reason"
    ] == "session_timeout"
    await asyncio.sleep(0)
    await runtime.stop_done.wait()
    assert host.store.get(sid)["status"] == "closed"
    sid, _, _ = await new_session(http, customer="tool-stopped")
    runtime = host.get(sid)
    await runtime.stop("user_stop")
    assert (await runtime.tools["search_policy"](query="收入怎么计算？"))["status"] == "denied"


def runtime_private_hooks_present(runtime):
    """运行时不另行实现工具分发器或调用计数器。"""
    return any(
        hasattr(runtime, name) for name in ("call_tool", "_call_tool", "claim_tool", "tool_count")
    )


@pytest.mark.asyncio
async def test_authoritative_session_state_and_memory_write_need_no_snapshot(setup, monkeypatch):
    host, http, _ = setup
    sid, _, _ = await new_session(http, customer="simple-boundaries")
    rt = host.get(sid)
    source = rt.service.add_message(rt.ctx, "我在厦门")
    # 已废弃的重复运行时字段不能控制当前执行。
    with host.store.connect() as db:
        db.execute(
            "UPDATE runtime_sessions SET status='closed',started_at=0 WHERE session_id=%s", (sid,)
        )

    def unexpected_snapshot(_ctx):
        raise AssertionError("Small checks and Memory writes must not build a full snapshot")

    monkeypatch.setattr(rt.service, "snapshot", unexpected_snapshot)
    result = rt.service.write_memory(
        rt.ctx, field="city", value="厦门", message_id=source["data"]["message_id"], quote="厦门"
    )
    assert result["status"] == "success"
    assert host.store.get(sid)["status"] == "active"
    await rt.stop()
    assert host.store.get(sid)["status"] == "closed"
    assert (await rt.tools["search_policy"](query="报名条件"))["status"] == "denied"
    other = await host.create(rt.ctx.scenario_id, rt.ctx.customer_id)
    assert other.service.read_memory(other.ctx)["data"]["fields"][0]["value"] == "厦门"


@pytest.mark.asyncio
async def test_host_stop_is_idempotent_and_has_no_external_business_dependency(setup):
    host, http, _ = setup
    sid, auth, data = await new_session(http)
    runtime = host.get(sid)
    assert not hasattr(runtime, "business")
    assert (
        not {"progress", "operations", "cards", "resources", "contact_allowed"}
        & data["state"].keys()
    )
    runtime.service.add_message(runtime.ctx, "我已经注册好了")
    state = (await http.get(f"/api/sessions/{sid}/state", headers=auth)).json()
    assert not {"progress", "final_met", "newly_completed"} & state.keys()
    assert (await http.get(f"/api/sessions/{sid}/state")).status_code == 401
    await asyncio.gather(runtime.stop("user_stop"), runtime.stop("shutdown"))
    assert runtime.service.session(runtime.ctx)["state"] == "closed"
    assert len([event for event in host.store.events(sid) if event["type"] == "closed"]) == 1
    assert (
        await http.post(f"/api/sessions/{sid}/voice/start", headers=auth, json={})
    ).status_code == 409
    fresh = await host.create(runtime.ctx.scenario_id, runtime.ctx.customer_id)
    assert fresh.id != sid


@pytest.mark.asyncio
async def test_conversation_keeps_refusal_as_dialogue_and_host_owns_close(setup):
    from types import SimpleNamespace

    from agentscope.state import AgentState

    host, http, _ = setup
    sid, _, _ = await new_session(http)
    runtime = host.get(sid)

    async def reply_stream(message):
        yield {"type": "REPLY_START", "reply_id": "goodbye", "role": "assistant"}
        yield {
            "type": "TEXT_BLOCK_DELTA",
            "reply_id": "goodbye",
            "delta": "好的，不打扰您了，再见。",
        }
        yield {"type": "REPLY_END", "reply_id": "goodbye", "finished_reason": "completed"}

    runtime.agent = SimpleNamespace(
        state=AgentState(session_id=runtime.id), reply_stream=reply_stream
    )
    # 旧版元数据不能在回复后悄悄关闭新会话。
    runtime.save_metadata(end_after_reply="dnc")
    assert await runtime.message("我不感兴趣，再见") == "好的，不打扰您了，再见。"
    assert runtime.service.session(runtime.ctx)["state"] == "active"
    assert not {"end_session", "stop_contact"} & runtime.tools.keys()
    await runtime.stop("user_stop")
    assert runtime.service.session(runtime.ctx)["state"] == "closed"


@pytest.mark.asyncio
async def test_frozen_scenario_history_keeps_original_context_and_cannot_run(setup):
    import copy

    from backend.agent_harness.runtime.snapshots import digest
    from backend.agent_harness.scenario import Scenario

    host, _http, _ = setup
    current = host.services["didi_driver"].scenario
    data = copy.deepcopy(current.data)
    data["scenario"]["version"] = "historical"
    old = Scenario(
        data,
        current.root,
        digest({"config": data, "resources": current.resources}),
        current.resources,
    )
    legacy_service = BackendService(host.settings.app_db, old)
    try:
        ctx = legacy_service.create_session("historic-customer")
        legacy_service.add_message(ctx, "这是一段历史对话")
        host.store.create(ctx.id, old, "text")
        legacy_service.end_session(ctx, "historical")
    finally:
        legacy_service.close()
    frozen = host.get(ctx.id)
    assert frozen.service.read_only
    assert frozen.service.scenario.version == "historical"
    assert frozen.service.snapshot(frozen.ctx)["messages"][0]["text"] == "这是一段历史对话"
    with pytest.raises(RuntimeError, match="historical_session_read_only"):
        await frozen.message("继续")
    assert (await frozen.tools["search_policy"](query="报名条件"))["status"] == "denied"


@pytest.mark.asyncio
async def test_financial_product_result_preserves_customer_purpose_filter(setup):
    host, http, _ = setup
    sid, _, _ = await new_session(
        http, scenario="financial_consultation", customer="purpose-filter"
    )
    runtime = host.get(sid)
    purpose = "了解借款费用"
    result = await runtime.tools["query_product_options"](purpose=purpose)
    assert result["status"] == "success"
    assert result["data"]["filters"]["purpose"] == purpose
    assert "source_type" not in result["data"]
    assert result["data"]["items"]
    assert all(
        purpose in item["eligibility_filters"]["purposes"] for item in result["data"]["items"]
    )


@pytest.mark.asyncio
async def test_native_loop_uses_current_customer_intent_without_profile_tool_roundtrip(
    setup, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from agentscope.model import OpenAIChatModel
    from openai.types.chat import ChatCompletionChunk

    host, http, _ = setup
    host.settings = replace(host.settings, agent_api_key="test-no-network")
    sid, _, _ = await new_session(
        http, scenario="meituan_merchant", customer="memory-denied-dialogue"
    )
    runtime = host.get(sid)
    captures = []

    class Stream:
        def __init__(self, chunk):
            self.chunk = chunk

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def __aiter__(self):
            async def chunks():
                yield self.chunk

            return chunks()

    async def completion(**kwargs):
        captures.append(kwargs)
        if len(captures) == 1:
            name, arguments = (
                "query_merchant_offers",
                {"area": "丽江", "store_category": "鲜花店", "is_new_merchant": False},
            )
        else:
            name = None
        if name:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call-{len(captures)}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ]
            }
            finish = "tool_calls"
        else:
            delta, finish = (
                {"content": "按您已经经营两年的情况，可以先了解适用已有门店的活动条件。"},
                "stop",
            )
        return Stream(
            ChatCompletionChunk(
                id="memory-denial",
                created=1,
                model="fake",
                object="chat.completion.chunk",
                choices=[{"index": 0, "delta": delta, "finish_reason": finish}],
            )
        )

    original = OpenAIChatModel.__init__

    def intercept(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=completion)), close=AsyncMock()
        )

    monkeypatch.setattr(OpenAIChatModel, "__init__", intercept)
    reply = await runtime.message(
        "我在丽江开鲜花店，不是新商家，已经做了两年，想了解能参加的活动。"
    )
    assert len(captures) == 2
    assert all("response_format" not in capture for capture in captures)
    results = [
        event["data"]
        for event in host.store.events(sid, limit=1000)
        if event["type"] == "tool_result"
    ]
    assert [event["name"] for event in results] == ["query_merchant_offers"]
    offers = results[0]["result"]
    assert offers["status"] == "success"
    assert offers["data"]["requested_filters"]["is_new_merchant"] is False
    assert all(
        item["eligibility_filters"]["is_new_merchant"] is False for item in offers["data"]["items"]
    )
    assert runtime.service.read_memory(runtime.ctx, "is_new_merchant")["status"] == "no_data"
    assert runtime.service.session(runtime.ctx)["state"] == "active"
    assert "已记住" not in reply and "是/不是" not in reply


@pytest.mark.asyncio
async def test_business_audit_is_private_and_public_event_cursor_advances(setup):
    host, http, _ = setup
    sid, auth, _ = await new_session(http, scenario="meituan_rider", customer="private-audit")
    runtime = host.get(sid)
    before = host.store.events(sid, limit=1000)
    after = before[-1]["seq"] if before else 0
    result = await runtime.tools["search_nearby_stations"](location="长沙")
    stored = host.store.events(sid, after=after)
    assert len(stored) == 1 and stored[0]["type"] == "business_query_audit"
    assert stored[0]["data"]["result"]["data"]["source_type"] == "local_catalog"
    response = await http.get(f"/api/sessions/{sid}/events?after={after}", headers=auth)
    assert response.status_code == 200
    public = response.json()
    assert public["events"] == []
    assert public["last_seq"] == stored[0]["seq"]
    seq = runtime.emit("tool_result", {"name": "search_nearby_stations", "result": result})
    response = await http.get(f"/api/sessions/{sid}/events?after={after}", headers=auth)
    public = response.json()
    assert [event["type"] for event in public["events"]] == ["tool_result"]
    assert public["last_seq"] == seq
    assert "template_provenance" not in response.text
    assert "source_type" not in response.text


@pytest.mark.asyncio
async def test_old_workspace_history_is_read_only_and_isolated_from_current_customers(setup):
    host, http, _ = setup
    current = host.services["didi_driver"]
    original = BackendService(
        host.settings.app_db, current.scenario, workspace_id="prior_workspace"
    )
    try:
        ctx = original.create_session("same-customer")
        original.add_message(ctx, "之前咨询过司机注册")
        original.end_session(ctx)
        host.store.create(ctx.id, current.scenario, "text")
    finally:
        original.close()
    historical = host.get(ctx.id)
    assert historical.ctx.workspace_id == "prior_workspace"
    assert historical.service.read_only
    assert (
        historical.service.snapshot(historical.ctx)["messages"][0]["text"] == "之前咨询过司机注册"
    )
    assert (await historical.tools["search_policy"](query="报名条件"))["status"] == "denied"
    with pytest.raises(RuntimeError, match="historical_session_read_only"):
        await historical.message("继续")
    sid, _, _ = await new_session(http, customer="same-customer")
    fresh = host.get(sid)
    assert fresh.ctx.workspace_id == "growth"
    assert fresh.service.snapshot(fresh.ctx)["messages"] == []
    assert fresh.service.read_memory(fresh.ctx)["data"]["fields"] == []
