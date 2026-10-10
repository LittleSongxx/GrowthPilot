"""验证画像提取的独立用量审计，所有模型响应都由本地 HTTP 替身提供。"""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from openai import AsyncOpenAI, InternalServerError
from pydantic import ValidationError

from backend.agent_harness import BackendService, load_scenario
from backend.agent_harness.memory import extraction
from backend.agent_harness.memory.worker import ProfileWorker
from backend.agent_harness.runtime.store import RuntimeStore
from backend.database import DatabaseConfig
from backend.settings import Settings
from tests.db import database_for
from tests.paths import ROOT


@pytest.fixture
def settings(tmp_path):
    return Settings(
        ROOT,
        tmp_path,
        DatabaseConfig.from_env(),
        profile_api_key="test-profile-secret",
        profile_model="test-profile-model",
    )


def install_transport(monkeypatch, respond):
    """保留真实 SDK 的响应解析，只替换 HTTP 传输，避免访问外部服务。"""
    monkeypatch.setattr(
        extraction,
        "AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        ),
    )


def completion(*, usage=None, content='{"updates":[]}', finish_reason="stop"):
    body = {
        "id": "profile-test-response",
        "object": "chat.completion",
        "created": 1,
        "model": "test-profile-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def assert_unknown_usage(diagnostics):
    assert diagnostics["usage"] is None
    for field in (
        "input_tokens",
        "output_tokens",
        "cache_input_tokens",
        "cache_creation_input_tokens",
    ):
        assert diagnostics[field] is None


@pytest.mark.parametrize(
    "cache_fields",
    [
        {"prompt_cache_hit_tokens": 7, "cache_creation_input_tokens": 3},
        {"prompt_tokens_details": {"cached_tokens": 7, "cache_creation_tokens": 3}},
    ],
)
async def test_success_records_usage_and_cache_tokens(settings, monkeypatch, cache_fields):
    usage = {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25, **cache_fields}
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return completion(usage=usage)

    install_transport(monkeypatch, respond)
    diagnostics = {}
    result = await extraction.extract_profile(settings, {"messages": []}, diagnostics=diagnostics)

    assert result == {"updates": []}
    assert len(requests) == 1
    assert diagnostics["model_name"] == settings.profile_model
    assert diagnostics["request_started"] is True
    assert diagnostics["response_received"] is True
    assert diagnostics["status"] == "completed"
    assert diagnostics["finished_reason"] == "stop"
    assert diagnostics["input_tokens"] == 20
    assert diagnostics["output_tokens"] == 5
    assert diagnostics["cache_input_tokens"] == 7
    assert diagnostics["cache_creation_input_tokens"] == 3
    for key, value in usage.items():
        if isinstance(value, dict):
            assert diagnostics["usage"][key].items() >= value.items()
        else:
            assert diagnostics["usage"][key] == value
    assert diagnostics["duration"] >= 0
    assert diagnostics.get("error_category") is None


async def test_success_without_usage_keeps_unknown_values(settings, monkeypatch):
    install_transport(monkeypatch, lambda request: completion())
    diagnostics = {}

    assert await extraction.extract_profile(
        settings, {"messages": []}, diagnostics=diagnostics
    ) == {"updates": []}

    assert diagnostics["status"] == "completed"
    assert diagnostics["response_received"] is True
    assert_unknown_usage(diagnostics)


@pytest.mark.parametrize("model,token_parameter,temperature", [
    ("gpt-4.1-mini", "max_tokens", 0.2),
    ("qwen-plus", "max_tokens", 0.3),
    ("gpt-5-mini", "max_completion_tokens", None),
])
async def test_extraction_compatible_models_omit_deepseek_parameters(
    settings, monkeypatch, model, token_parameter, temperature,
):
    configured = replace(
        settings, profile_model=model, profile_max_tokens_param=token_parameter,
        profile_temperature=temperature, profile_max_tokens=2500,
        agent_api_key="agent-secret-never-send", profile_base_url="https://profiles.example/v1",
    )
    requests = []

    def respond(request):
        assert str(request.url) == "https://profiles.example/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer " + configured.profile_api_key
        requests.append(json.loads(request.content))
        return completion()

    install_transport(monkeypatch, respond)
    assert await extraction.extract_profile(configured, {"messages": []}) == {"updates": []}

    assert len(requests) == 1
    assert requests[0]["model"] == model
    if temperature is None:
        assert "temperature" not in requests[0]
    else:
        assert requests[0]["temperature"] == temperature
    assert requests[0][token_parameter] == configured.profile_max_tokens
    assert len({"max_tokens", "max_completion_tokens"} & requests[0].keys()) == 1
    assert "thinking" not in requests[0]
    assert "reasoning_effort" not in requests[0]


async def test_extraction_inherits_replaced_agent_connection(settings, monkeypatch):
    inherited = replace(
        settings, profile_api_key="", profile_base_url="", profile_model="",
        agent_api_key="original-secret", agent_base_url="https://original.example/v1",
        agent_model="original-model",
    )
    changed = replace(
        inherited, agent_api_key="replacement-secret",
        agent_base_url="https://replacement.example/v1", agent_model="replacement-model",
    )
    requests = []

    def respond(request):
        assert str(request.url) == "https://replacement.example/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer replacement-secret"
        requests.append(json.loads(request.content))
        return completion()

    install_transport(monkeypatch, respond)
    diagnostics = {}
    await extraction.extract_profile(changed, {"messages": []}, diagnostics=diagnostics)

    assert requests[0]["model"] == "replacement-model"
    assert diagnostics["model_name"] == "replacement-model"
    assert changed.profile_api_key == changed.profile_base_url == changed.profile_model == ""


async def test_cache_misses_are_not_reported_as_cache_creation(settings, monkeypatch):
    usage = {
        "prompt_tokens": 20,
        "completion_tokens": 5,
        "total_tokens": 25,
        "prompt_cache_hit_tokens": 7,
        "prompt_cache_miss_tokens": 13,
    }
    install_transport(monkeypatch, lambda request: completion(usage=usage))
    diagnostics = {}

    await extraction.extract_profile(settings, {"messages": []}, diagnostics=diagnostics)

    assert diagnostics["cache_input_tokens"] == 7
    assert diagnostics["cache_creation_input_tokens"] is None
    assert diagnostics["usage"]["prompt_cache_miss_tokens"] == 13


@pytest.mark.parametrize(
    "content,finish_reason,error_type",
    [
        ("无法解析的 JSON 与客户隐私", "stop", ValidationError),
        ('{"updates":[]}', "length", ValueError),
    ],
)
async def test_invalid_output_still_preserves_billable_usage(
    settings, monkeypatch, content, finish_reason, error_type
):
    usage = {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
    install_transport(
        monkeypatch,
        lambda request: completion(usage=usage, content=content, finish_reason=finish_reason),
    )
    diagnostics = {}

    with pytest.raises(error_type):
        await extraction.extract_profile(settings, {"messages": []}, diagnostics=diagnostics)

    assert diagnostics["status"] == "error"
    assert diagnostics["request_started"] is True
    assert diagnostics["response_received"] is True
    assert diagnostics["finished_reason"] == finish_reason
    assert diagnostics["input_tokens"] == 20
    assert diagnostics["output_tokens"] == 5
    assert diagnostics["error_category"] == error_type.__name__
    assert "客户隐私" not in json.dumps(diagnostics, ensure_ascii=False)


async def test_http_error_keeps_unknown_usage_without_error_body(settings, monkeypatch):
    private_error = "供应商错误正文和密钥 test-profile-secret"
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(500, json={"error": {"message": private_error}})

    install_transport(monkeypatch, respond)
    diagnostics = {}

    with pytest.raises(InternalServerError):
        await extraction.extract_profile(settings, {"messages": []}, diagnostics=diagnostics)

    assert len(requests) == 1
    assert diagnostics["status"] == "error"
    assert diagnostics["request_started"] is True
    assert diagnostics["response_received"] is False
    assert diagnostics["error_category"] == "InternalServerError"
    assert_unknown_usage(diagnostics)
    public = json.dumps(diagnostics, ensure_ascii=False)
    assert private_error not in public
    assert settings.profile_api_key not in public


@pytest.fixture
def persistence_settings(settings, tmp_path):
    return replace(settings, app_db=database_for(tmp_path / "profiles"))


@pytest.fixture
def closed_profile_session(persistence_settings):
    scenario = load_scenario(ROOT / "backend/scenarios/meituan_rider/scenario.yaml")
    service = BackendService(persistence_settings.app_db, scenario)
    store = RuntimeStore(persistence_settings.app_db)
    ctx = service.create_session("profile-usage-customer")
    store.create(ctx.id, scenario, "text")
    service.add_message(ctx, "我在上海")
    service.end_session(ctx)
    yield service, store, ctx
    service.close()


async def test_worker_records_each_retry_as_separate_profile_usage(
    persistence_settings, closed_profile_session, monkeypatch
):
    service, store, ctx = closed_profile_session
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(500, json={"error": {"message": "不可公开的响应正文"}})
        return completion(usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25})

    install_transport(monkeypatch, respond)
    worker = ProfileWorker(persistence_settings)

    assert await worker.run_once()
    assert service.profiles.state(ctx)["status"] == "retrying"
    with service._connection():
        service.db.execute("UPDATE profile_jobs SET next_attempt=0 WHERE session_id=%s", (ctx.id,))
    assert await worker.run_once()
    assert service.profiles.state(ctx)["status"] == "completed"
    assert not await worker.run_once()

    events = store.events(ctx.id, limit=100)
    profile_events = [event["data"] for event in events if event["type"] == "profile_usage"]
    assert len(requests) == len(profile_events) == 2
    assert [event["attempt"] for event in profile_events] == [1, 2]
    assert [event["status"] for event in profile_events] == ["error", "completed"]
    assert all(event["component"] == "profile_extraction" for event in profile_events)
    assert_unknown_usage(profile_events[0])
    assert profile_events[1]["input_tokens"] == 20
    assert profile_events[1]["output_tokens"] == 5
    assert not any(event["type"] == "usage" for event in events)
    assert "不可公开的响应正文" not in json.dumps(events, ensure_ascii=False)


async def test_worker_accepts_legacy_two_argument_extractor_without_fake_usage(
    persistence_settings, closed_profile_session
):
    service, store, ctx = closed_profile_session
    calls = []

    async def legacy_extractor(received_settings, payload):
        calls.append((received_settings, payload))
        return {"updates": []}

    worker = ProfileWorker(persistence_settings, extractor=legacy_extractor)
    assert await worker.run_once()

    assert service.profiles.state(ctx)["status"] == "completed"
    assert len(calls) == 1 and calls[0][0] is persistence_settings
    assert calls[0][1]["messages"][0]["text"] == "我在上海"
    assert not any(event["type"] in {"usage", "profile_usage"} for event in store.events(ctx.id))


async def test_worker_cancellation_records_unknown_usage_and_restores_pending_job(
    persistence_settings, closed_profile_session, monkeypatch
):
    service, store, ctx = closed_profile_session
    request_started = asyncio.Event()

    async def respond(request):
        request_started.set()
        await asyncio.Event().wait()

    install_transport(monkeypatch, respond)
    worker = ProfileWorker(persistence_settings)
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(request_started.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert service.profiles.state(ctx) == {
        "status": "pending",
        "attempts": 0,
        "error": "worker_interrupted",
    }
    events = store.events(ctx.id)
    profile_events = [event["data"] for event in events if event["type"] == "profile_usage"]
    assert len(profile_events) == 1
    diagnostics = profile_events[0]
    assert diagnostics["status"] == "cancelled"
    assert diagnostics["attempt"] == 1
    assert diagnostics["request_started"] is True
    assert diagnostics["response_received"] is False
    assert_unknown_usage(diagnostics)
    assert not any(event["type"] == "usage" for event in events)


async def test_usage_storage_failure_does_not_retry_successful_extraction(
    persistence_settings, closed_profile_session, monkeypatch, caplog
):
    service, store, ctx = closed_profile_session
    message = service.profiles.payload(ctx)["messages"][0]
    patch = {
        "updates": [
            {
                "field": "area",
                "value": "上海",
                "status": "known",
                "evidence": {"message_id": message["id"], "quote": "我在上海"},
            }
        ]
    }
    requests = []

    def respond(request):
        requests.append(request)
        return completion(
            content=json.dumps(patch, ensure_ascii=False),
            usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        )

    install_transport(monkeypatch, respond)
    with service._connection():
        service.db.execute("""
            CREATE FUNCTION fail_profile_usage() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION '审计失败正文及密钥 test-profile-secret' USING ERRCODE = '23514';
            END;
            $$;
            CREATE TRIGGER fail_profile_usage BEFORE INSERT ON runtime_events
            FOR EACH ROW WHEN (NEW.type='profile_usage')
            EXECUTE FUNCTION fail_profile_usage();
        """, prepare=False)
    worker = ProfileWorker(persistence_settings)

    with caplog.at_level("WARNING"):
        assert await worker.run_once()

    assert service.profiles.state(ctx) == {"status": "completed", "attempts": 1, "error": None}
    assert service.read_memory(ctx)["data"]["fields"][0]["value"] == "上海"
    assert not await worker.run_once()
    assert len(requests) == 1
    assert not any(event["type"] == "profile_usage" for event in store.events(ctx.id))
    assert "CheckViolation" in caplog.text
    assert "审计失败正文" not in caplog.text
    assert persistence_settings.profile_api_key not in caplog.text
