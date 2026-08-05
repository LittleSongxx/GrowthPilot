"""使用固定 AgentScope 和假供应商 SDK，验证通话结束不增加模型生成。"""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from agentscope.model import OpenAIChatModel
from openai.types.chat import ChatCompletionChunk

from backend.api.app import create_app
from tests.runtime.test_conversation import new_session
from tests.runtime.test_conversation import setup as setup  # noqa: PLC0414
from tests.runtime.test_speech_pipeline import send_audio


def tool_reply(name, arguments):
    """只模拟供应商返回的工具调用，不绕过框架的权限、参数和结果处理。"""
    return {
        "tool_calls": [
            {
                "index": 0,
                "id": f"call-{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ]
    }, "tool_calls"


class ScriptedSDK:
    """保留真实模型格式化；每个脚本步骤对应一次实际 SDK 请求。"""

    def __init__(self):
        self.steps = []
        self.captures = []

    async def create(self, **kwargs):
        self.captures.append(kwargs)
        assert self.steps, "出现了脚本之外的额外模型请求"
        step = self.steps.pop(0)
        if callable(step):
            step = await step()
        delta, reason = step
        chunk = ChatCompletionChunk(
            id=f"model-{len(self.captures)}",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[{"index": 0, "delta": delta, "finish_reason": reason}],
        )

        class Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def __aiter__(self):
                async def chunks():
                    yield chunk

                return chunks()

        return Stream()


@pytest.fixture
async def call_stack(setup, monkeypatch):
    host, _, _ = setup
    host.settings = replace(host.settings, agent_api_key="test-no-network")
    sdk = ScriptedSDK()
    unused_clients = []
    original = OpenAIChatModel.__init__

    def intercept(self, *args, **kwargs):
        original(self, *args, **kwargs)
        unused_clients.append(self.client)
        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=sdk.create)),
            close=AsyncMock(),
        )

    monkeypatch.setattr(OpenAIChatModel, "__init__", intercept)
    app = create_app(host.settings, host)
    speech = SimpleNamespace(
        transcribe=AsyncMock(return_value=("我暂时没有其他问题，再见。", None)),
        synthesize=AsyncMock(return_value=(b"RIFF-test-wave", {"characters": 6})),
    )
    app.state.speech = speech
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787"
        ) as http:
            sid, auth, _ = await new_session(http)
            yield host, http, speech, host.get(sid), auth, sdk
    finally:
        for client in unused_clients:
            await client.close()


def assert_closed_tool_context(rt):
    """工具有对应结果，框架能够继续接收下一条客户输入。"""
    assert not rt.agent.state.get_unfinished_tool_calls(rt.agent.name)
    calls = {}
    results = {}
    for message in rt.agent.state.context:
        calls.update({block.id: block for block in message.get_content_blocks("tool_call")})
        results.update({block.id: block for block in message.get_content_blocks("tool_result")})
    assert calls.keys() == results.keys()
    assert all(block.state == "finished" for block in calls.values())
    return results


@pytest.mark.parametrize("local_goodbye", [False, True])
async def test_evaluation_times_final_first_text_through_real_framework(
    call_stack, monkeypatch, local_goodbye,
):
    """真实框架处理工具和告别；本地供应商替身控制首块与回复结束的时间差。"""
    from backend.agent_harness.runtime import events as events_module
    from evaluation.common import trace as trace_module

    host, _, _, rt, _, sdk = call_stack
    elapsed = [0.0]
    clock = SimpleNamespace(monotonic=lambda: elapsed[0])
    monkeypatch.setattr(events_module, "time", clock)
    monkeypatch.setattr(trace_module, "time", clock)
    final_text = "您需要的说明已完成，再见。"
    if local_goodbye:
        rt.settings = replace(rt.settings, agent_max_iterations=1)

    async def first_model():
        elapsed[0] = 2.0
        name, arguments = (
            ("end_call", {"message": final_text}) if local_goodbye
            else ("search_driver_service_points", {"location": "杭州"})
        )
        delta, reason = tool_reply(name, arguments)
        delta["content"] = "我先处理一下。"
        return delta, reason

    sdk.steps = [first_model]
    if not local_goodbye:
        sdk.steps.append(({"content": final_text}, "stop"))
    process = rt.events.process

    async def observe(event):
        await process(event)
        data = event.model_dump(mode="json")
        if data["type"] == "TOOL_RESULT_END":
            elapsed[0] = 8.0
        if data["type"] == "TEXT_BLOCK_DELTA" and data.get("delta") == final_text:
            elapsed[0] = 12.0

    monkeypatch.setattr(rt.events, "process", observe)
    turn = {"turn": 1, "origin": "customer", "user_message": "请说明接下来的安排。"}
    await trace_module.record_agent_turn(host, rt, turn)
    assert turn["reply"] == final_text
    assert turn["first_token_seconds"] == 8.0
    assert turn["elapsed_seconds"] == 12.0
    assert len(sdk.captures) == (1 if local_goodbye else 2)
    assert not sdk.steps


@pytest.mark.parametrize(
    ("max_iters", "finished_reason"), [(4, "completed"), (1, "exceed_max_iters")]
)
async def test_end_call_uses_one_model_request_and_preserves_native_outcome(
    call_stack, max_iters, finished_reason
):
    host, http, speech, rt, auth, sdk = call_stack
    rt.settings = replace(rt.settings, agent_max_iterations=max_iters)
    goodbye = "好的，祝您生活愉快，再见。"
    sdk.steps = [tool_reply("end_call", {"message": goodbye})]

    response = await send_audio(http, rt, auth)

    assert response.status_code == 200, response.text
    assert len(sdk.captures) == 1 and not sdk.steps
    assert response.json()["end_after_playback"] is True
    assert rt.end_after_playback is True
    assert rt.service.session(rt.ctx)["state"] == "active"
    mid = response.json()["reply_message_id"]
    assert rt.service.message(rt.ctx, mid)["text"] == goodbye
    results = assert_closed_tool_context(rt)
    assert results["call-end_call"].state == "success"
    assert any(
        block.text == goodbye
        for message in rt.agent.state.context
        for block in message.get_content_blocks("text")
    )
    events = host.store.events(rt.id, limit=1000)
    outcomes = [e["data"] for e in events if e["type"] == "reply_outcome"]
    assert outcomes[-1]["finished_reason"] == finished_reason
    local_model_events = [
        event["data"]
        for event in events
        if event["type"] == "framework" and event["data"].get("source") == "end_call"
    ]
    assert [event["type"] for event in local_model_events] == ["MODEL_CALL_START", "MODEL_CALL_END"]
    assert all(event["model_name"] is None for event in local_model_events)
    assert len([event for event in events if event["type"] == "usage"]) == 1
    audio = await http.get(f"/api/sessions/{rt.id}/voice/audio/{mid}", headers=auth)
    assert audio.status_code == 200
    speech.synthesize.assert_awaited_once_with(goodbye)
    assert len(sdk.captures) == 1


async def test_business_tool_then_end_call_needs_only_two_model_requests(call_stack):
    host, http, _, rt, auth, sdk = call_stack
    goodbye = "服务点信息已经说明，您有需要时再联系，再见。"
    sdk.steps = [
        tool_reply("search_driver_service_points", {"location": "杭州"}),
        tool_reply("end_call", {"message": goodbye}),
    ]

    response = await send_audio(http, rt, auth)

    assert response.status_code == 200, response.text
    assert response.json()["end_after_playback"] is True
    assert len(sdk.captures) == 2 and not sdk.steps
    assert rt.service.message(rt.ctx, response.json()["reply_message_id"])["text"] == goodbye
    results = assert_closed_tool_context(rt)
    assert {result.name for result in results.values()} == {
        "search_driver_service_points",
        "end_call",
    }
    assert all(result.state == "success" for result in results.values())
    assert any(message["role"] == "tool" for message in sdk.captures[1]["messages"])
    assert any(
        event["type"] == "business_query_audit" for event in host.store.events(rt.id, limit=1000)
    )


async def test_new_input_and_interrupt_clear_goodbye_before_next_native_turn(call_stack):
    _, http, speech, rt, auth, sdk = call_stack
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def wait_for_interrupt():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    sdk.steps = [
        tool_reply("end_call", {"message": "好的，再见。"}),
        wait_for_interrupt,
        ({"content": "好的，您还想了解哪些条件？"}, "stop"),
    ]
    first = await send_audio(http, rt, auth)
    assert first.status_code == 200 and first.json()["end_after_playback"] is True
    speech.transcribe.return_value = ("等一下，我还有问题。", None)
    pending = asyncio.create_task(send_audio(http, rt, auth))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert rt.end_after_playback is False
        interrupted = await http.post(
            f"/api/sessions/{rt.id}/voice/interrupt", headers=auth, json={}
        )
        assert interrupted.status_code == 200
        assert (await asyncio.wait_for(pending, 2)).status_code == 409
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    assert cancelled.is_set() and rt.end_after_playback is False
    assert rt.service.session(rt.ctx)["state"] == "active"
    speech.transcribe.return_value = ("我想知道注册条件。", None)

    resumed = await send_audio(http, rt, auth)

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["end_after_playback"] is False
    assert rt.service.message(rt.ctx, resumed.json()["reply_message_id"])["text"] == (
        "好的，您还想了解哪些条件？"
    )
    assert len(sdk.captures) == 3 and not sdk.steps
    assert rt.end_after_playback is False
    assert_closed_tool_context(rt)


@pytest.mark.parametrize("action", ["interrupt", "retain_transcript", "input_only"])
async def test_pending_goodbye_is_cleared_when_customer_continues(call_stack, action):
    _, http, speech, rt, auth, sdk = call_stack
    sdk.steps = [
        tool_reply("end_call", {"message": "好的，再见。"}),
        ({"content": "我在听，请继续。"}, "stop"),
    ]
    first = await send_audio(http, rt, auth)
    assert first.status_code == 200 and first.json()["end_after_playback"] is True
    speech.transcribe.return_value = ("等一下，我还没说完。", None)

    if action == "interrupt":
        response = await http.post(
            f"/api/sessions/{rt.id}/voice/interrupt",
            headers=auth,
            json={"message_id": first.json()["reply_message_id"]},
        )
        assert response.status_code == 200
    elif action == "retain_transcript":
        rt.retain_transcript("等一下，我还没说完。")
    else:
        response = await http.post(
            f"/api/sessions/{rt.id}/voice/turn?input_only=true",
            headers={**auth, "Content-Type": "audio/webm"},
            content=b"browser-audio",
        )
        assert response.status_code == 200, response.text
        assert response.json()["reply_message_id"] is None
        assert response.json()["end_after_playback"] is False
    assert rt.end_after_playback is False
    assert len(sdk.captures) == 1

    resumed = await send_audio(http, rt, auth)

    assert resumed.status_code == 200 and resumed.json()["end_after_playback"] is False
    assert (
        rt.service.message(rt.ctx, resumed.json()["reply_message_id"])["text"] == "我在听，请继续。"
    )
    assert len(sdk.captures) == 2 and not sdk.steps
    assert_closed_tool_context(rt)


async def test_plain_reply_keeps_default_format_and_end_call_is_host_only(call_stack):
    host, http, _, rt, auth, sdk = call_stack
    text = "可以，您想先了解报名流程还是服务点信息？"
    sdk.steps = [({"content": text}, "stop")]

    response = await send_audio(http, rt, auth)

    assert response.status_code == 200, response.text
    assert response.json()["end_after_playback"] is False
    assert rt.service.message(rt.ctx, response.json()["reply_message_id"])["text"] == text
    assert len(sdk.captures) == 1 and not sdk.steps
    assert "response_format" not in sdk.captures[0]
    tool_names = {tool["function"]["name"] for tool in sdk.captures[0]["tools"]}
    assert "end_call" in tool_names
    assert "end_call" in rt.tools
    for service in host.services.values():
        assert "end_call" not in {item["id"] for item in service.scenario.data["tools"]}
    assert rt.end_after_playback is False


async def test_internal_text_input_also_clears_pending_goodbye(call_stack):
    _, http, _, rt, auth, sdk = call_stack
    sdk.steps = [
        tool_reply("end_call", {"message": "好的，再见。"}),
        ({"content": "可以，请说您的问题。"}, "stop"),
    ]
    first = await send_audio(http, rt, auth)
    assert first.status_code == 200 and rt.end_after_playback is True

    reply = await rt.message("等一下，我还想问一个问题。")

    assert reply == "可以，请说您的问题。"
    assert rt.end_after_playback is False
    assert len(sdk.captures) == 2 and not sdk.steps
    assert_closed_tool_context(rt)


async def test_end_intent_is_cleared_before_new_audio_transcription_finishes(call_stack):
    _, http, speech, rt, auth, sdk = call_stack
    entered, release = asyncio.Event(), asyncio.Event()
    sdk.steps = [
        tool_reply("end_call", {"message": "好的，再见。"}),
        ({"content": "好的，请继续。"}, "stop"),
    ]
    first = await send_audio(http, rt, auth)
    assert first.status_code == 200 and rt.end_after_playback is True

    async def transcribe(*_args):
        entered.set()
        await release.wait()
        return "等一下。", None

    speech.transcribe.side_effect = transcribe
    pending = asyncio.create_task(send_audio(http, rt, auth))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert rt.end_after_playback is False
        assert len(sdk.captures) == 1
    finally:
        release.set()
        response = await asyncio.wait_for(pending, 2)
    assert response.status_code == 200 and response.json()["end_after_playback"] is False
    assert len(sdk.captures) == 2 and not sdk.steps
