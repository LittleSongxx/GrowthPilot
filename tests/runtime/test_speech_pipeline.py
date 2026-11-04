"""语音仅改变传输方式，不改变 Agent 的权限或文本证据契约。"""

import asyncio
import base64
import io
import json
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from agentscope.state import AgentState

from backend.api.app import create_app
from backend.channels.speech import MAX_AUDIO_BYTES, SpeechAPI, SpeechUnavailable
from tests.runtime.test_conversation import new_session
from tests.runtime.test_conversation import setup as setup  # noqa: PLC0414


def sse_audio(pcm=b"\0\0" * 240, characters=2):
    """模拟供应商音频事件与最终使用量，不包含可下载文件。"""
    values = [
        {"output": {"audio": {"data": base64.b64encode(pcm).decode()}}},
        {
            "output": {"finish_reason": "stop", "audio": {"data": ""}},
            "usage": {"characters": characters},
        },
    ]
    return "".join("data: " + json.dumps(value) + "\n\n" for value in values).encode()


class Agent:
    def __init__(self, runtime, act=None):
        self.runtime, self.act = runtime, act
        self.state = AgentState(session_id=runtime.id)
        self.inputs = []

    async def reply_stream(self, message):
        self.inputs.append(message)
        rid = f"reply-{len(self.inputs)}"
        yield {"type": "REPLY_START", "reply_id": rid, "role": "assistant"}
        if self.act:
            await self.act()
        yield {"type": "TEXT_BLOCK_DELTA", "reply_id": rid, "delta": "这是业务回复。"}
        yield {"type": "REPLY_END", "reply_id": rid, "finished_reason": "completed"}


@pytest.fixture
async def speech_stack(setup):
    host, _, _ = setup
    app = create_app(host.settings, host)
    app.state.speech = SimpleNamespace(
        transcribe=AsyncMock(return_value=("我在厦门，请记住我的城市。", {"seconds": 2})),
        synthesize=AsyncMock(return_value=(b"RIFF-test-wave", {"characters": 7})),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787"
    ) as http:
        sid, auth, _ = await new_session(http)
        rt = host.get(sid)
        rt.agent = Agent(rt)
        yield host, http, app.state.speech, rt, auth


async def send_audio(http, rt, auth, content=b"test-browser-webm", mime="audio/webm;codecs=opus"):
    return await http.post(
        f"/api/sessions/{rt.id}/voice/turn", headers={**auth, "Content-Type": mime}, content=content
    )


async def test_asr_evidence_precedes_agent_and_profile_is_updated_after_call(speech_stack):
    host, http, speech, rt, auth = speech_stack

    async def remember():
        messages = rt.service.snapshot(rt.ctx)["messages"]
        source = messages[-1]
        assert source["origin"] == "customer_transcript" and source["final"]
        assert not {"read_memory", "write_memory"} & rt.tools.keys()
        assert rt.service.read_memory(rt.ctx)["status"] == "no_data"

    async def extract(_settings, payload):
        source = next(m for m in payload["messages"] if m["origin"] == "customer_transcript")
        return {
            "updates": [
                {
                    "field": "city",
                    "value": "厦门",
                    "evidence": {"message_id": source["id"], "quote": "我在厦门"},
                }
            ]
        }

    await host.profile_worker.close()
    host.profile_worker.extractor = extract

    rt.agent.act = remember
    response = await send_audio(http, rt, auth)
    assert response.status_code == 200, response.text
    assert len(rt.agent.inputs) == 1 and rt.agent.inputs[0].name == "客户"
    assert rt.service.read_memory(rt.ctx)["status"] == "no_data"
    await rt.stop()
    assert await host.profile_worker.run_once()
    memory = rt.service.read_memory(rt.ctx, "city")["data"]["fields"][0]
    assert memory["source_kind"] == "customer_transcript" and memory["value"] == "厦门"
    mid = response.json()["reply_message_id"]
    audio = await http.get(f"/api/sessions/{rt.id}/voice/audio/{mid}", headers=auth)
    assert audio.content == b"RIFF-test-wave" and audio.headers["cache-control"] == "no-store"
    speech.synthesize.assert_awaited_once_with("这是业务回复。")
    assert "test-browser-webm" not in json.dumps(host.store.events(rt.id))
    assert "progress" not in rt.service.snapshot(rt.ctx)


async def test_opening_once_uses_agent_without_customer_evidence(speech_stack):
    _, http, _, rt, auth = speech_stack
    for _ in range(2):
        response = await http.post(f"/api/sessions/{rt.id}/voice/start", headers=auth)
        assert response.status_code == 200
    assert len(rt.agent.inputs) == 1
    assert all(message.name == "host_control" for message in rt.agent.inputs)
    assert not rt.service.snapshot(rt.ctx)["memory"]


async def test_asr_intent_is_given_to_agent_without_phrase_matching(speech_stack):
    _host, http, speech, rt, auth = speech_stack
    speech.transcribe.return_value = ("不要再联系我", None)
    response = await send_audio(http, rt, auth)
    assert response.status_code == 200
    assert len(rt.agent.inputs) == 1
    assert "不要再联系我" in str(rt.agent.inputs[0].content)
    # 意图保留在对话中；只有宿主控制通话生命周期。
    assert rt.service.snapshot(rt.ctx)["session"]["state"] == "active"
    assert "session_intents" not in rt.service.snapshot(rt.ctx)


async def test_stop_during_asr_cancels_request_and_never_runs_agent(speech_stack):
    host, http, speech, rt, auth = speech_stack
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def delayed(*_args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    speech.transcribe.side_effect = delayed
    pending = asyncio.create_task(send_audio(http, rt, auth))
    await started.wait()
    concurrent = await send_audio(http, rt, auth)
    assert concurrent.status_code == 409
    stopped = await http.post(f"/api/sessions/{rt.id}/stop", headers=auth, json={})
    assert stopped.status_code == 200 and (await pending).status_code == 409
    assert cancelled.is_set() and rt.task is None and not rt.lock.locked()
    assert rt.agent.inputs == [] and host.store.get(rt.id)["status"] == "closed"
    assert not rt.service.snapshot(rt.ctx)["messages"]


async def test_tts_retry_does_not_repeat_agent_tools_or_accept_arbitrary_text(speech_stack):
    _, http, speech, rt, auth = speech_stack
    result = await send_audio(http, rt, auth)
    mid = result.json()["reply_message_id"]
    speech.synthesize.side_effect = SpeechUnavailable("语音服务暂不可用。")
    assert (
        await http.get(f"/api/sessions/{rt.id}/voice/audio/{mid}", headers=auth)
    ).status_code == 502
    speech.synthesize.side_effect = None
    assert (
        await http.get(f"/api/sessions/{rt.id}/voice/audio/{mid}", headers=auth)
    ).status_code == 200
    assert len(rt.agent.inputs) == 1
    assert (
        await http.get(f"/api/sessions/{rt.id}/voice/audio/arbitrary-text", headers=auth)
    ).status_code == 404
    assert (await http.get(f"/api/sessions/{rt.id}/voice/audio/{mid}")).status_code == 401


@pytest.mark.parametrize(
    ("audio", "mime", "status"),
    [
        (b"", "audio/webm", 400),
        (b"data", "text/plain", 415),
        (b"x" * (MAX_AUDIO_BYTES + 1), "audio/webm", 413),
    ],
    ids=["empty", "unsupported_type", "too_large"],
)
async def test_invalid_recording_never_reaches_provider(speech_stack, audio, mime, status):
    _, http, speech, rt, auth = speech_stack
    assert (await send_audio(http, rt, auth, audio, mime)).status_code == status
    speech.transcribe.assert_not_called()
    assert rt.task is None


async def test_empty_asr_and_provider_failure_do_not_create_customer_messages(speech_stack):
    _, http, speech, rt, auth = speech_stack
    speech.transcribe.return_value = ("", None)
    assert (await send_audio(http, rt, auth)).status_code == 422
    speech.transcribe.side_effect = SpeechUnavailable("语音服务暂不可用。")
    assert (await send_audio(http, rt, auth)).status_code == 502
    assert rt.agent.inputs == [] and rt.service.snapshot(rt.ctx)["messages"] == []
    assert rt.task is None


async def test_removed_public_text_and_switch_routes(speech_stack):
    _, http, _, rt, auth = speech_stack
    for path in (
        "messages",
        "resume",
        "voice/offer",
        "cards/anything/confirm",
        "cards/anything/received",
    ):
        assert (
            await http.post(f"/api/sessions/{rt.id}/{path}", headers=auth, json={})
        ).status_code == 405
    assert (
        await http.post(
            "/api/sessions",
            json={"scenario_id": "didi_driver", "customer_id": "another", "channel": "text"},
        )
    ).status_code == 422
    # 内部文本入口仍可供评测使用，无需调用任何语音 API。
    assert await rt.message("你好") == "这是业务回复。"


async def test_conversation_state_and_health_have_no_external_business_controls(speech_stack):
    _, http, _, rt, auth = speech_stack
    await send_audio(http, rt, auth)
    result = await http.get(f"/api/sessions/{rt.id}/state", headers=auth)
    assert result.status_code == 200
    state = result.json()
    assert {"session", "messages", "memory", "audit", "field_definitions"} <= state.keys()
    assert not {"cards", "operations", "progress", "milestone_definitions"} & state.keys()
    assert state["messages"][-1]["source_channel"] == "voice"
    health = (await http.get("/api/health")).json()
    assert health["business_data_mode"] == "local_catalog"
    assert health["policy_source"] == "rag"
    denied = await http.post(f"/api/sessions/{rt.id}/stop", headers=auth, json={"dnc": True})
    assert denied.status_code == 422
    assert rt.service.snapshot(rt.ctx)["session"]["state"] == "active"


async def test_cloud_requests_contain_only_audio_or_reply_and_no_business_tools(setup):
    host, _, _ = setup
    from dataclasses import replace

    settings = replace(
        host.settings, dashscope_api_key="private-key", dashscope_base_url="https://speech.example"
    )
    calls = []

    def provider(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer private-key"
        body = json.loads(request.content)
        assert "tools" not in body
        if "messages" in body:
            encoded = body["messages"][0]["content"][0]["input_audio"]["data"]
            assert encoded == "data:audio/webm;base64," + base64.b64encode(b"webm").decode()
            return httpx.Response(200, json={"choices": [{"message": {"content": "你好"}}]})
        assert body["input"] == {"text": "回复", "voice": "Cherry", "language_type": "Chinese"}
        assert request.headers["X-DashScope-SSE"] == "enable"
        return httpx.Response(200, content=sse_audio(b"\1\0\2\0"))

    speech = SpeechAPI(settings, transport=httpx.MockTransport(provider))
    assert (await speech.transcribe(b"webm", "audio/webm"))[0] == "你好"
    audio, _ = await speech.synthesize("回复")
    with wave.open(io.BytesIO(audio)) as wav:
        assert wav.getnframes() == 2 and wav.getframerate() == 24000
        assert wav.readframes(2) == b"\1\0\2\0"
    assert len(calls) == 2 and all(call.url.host == "speech.example" for call in calls)


async def test_provider_http_failure_does_not_expose_keys_audio_or_signed_urls(setup):
    from dataclasses import replace

    host, _, _ = setup
    settings = replace(host.settings, dashscope_api_key="private-key")
    speech = SpeechAPI(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, text="Authorization: private-key")
        ),
    )
    with pytest.raises(SpeechUnavailable) as failure:
        await speech.transcribe(b"private-audio", "audio/webm")
    assert "private" not in str(failure.value)


async def test_stop_during_agent_releases_client_and_does_not_play_stale_reply(speech_stack):
    host, http, _, rt, auth = speech_stack
    entered = asyncio.Event()

    async def waiting_tool():
        entered.set()
        await asyncio.Event().wait()

    rt.agent.act = waiting_tool
    rt.model_client = SimpleNamespace(close=AsyncMock())
    model_client = rt.model_client
    pending = asyncio.create_task(send_audio(http, rt, auth))
    await entered.wait()
    response = await http.post(f"/api/sessions/{rt.id}/stop", headers=auth, json={})
    assert response.status_code == 200 and (await pending).status_code == 409
    model_client.close.assert_awaited_once()
    assert rt.model_client is None and not rt.lock.locked()
    assert rt.events.last_message_id is None
    assert host.store.get(rt.id)["status"] == "closed"


async def test_idle_session_deadline_releases_customer_reservation(setup):
    from dataclasses import replace

    host, _, _ = setup
    host.settings = replace(host.settings, session_max_seconds=0.05)
    rt = await host.create("didi_driver", "idle-expiry")
    await asyncio.wait_for(rt.stop_done.wait(), 1)
    assert host.store.get(rt.id)["status"] == "closed"
    replacement = await host.create("didi_driver", "idle-expiry")
    assert replacement.id != rt.id


async def test_long_reply_tts_preserves_all_text_and_returns_one_wav(setup):
    from dataclasses import replace

    host, _, _ = setup
    settings = replace(host.settings, dashscope_api_key="private-key")
    sentences = []

    def provider(request):
        assert request.method == "POST"
        body = json.loads(request.content)
        sentences.append(body["input"]["text"])
        assert len(sentences[-1]) <= 600
        return httpx.Response(200, content=sse_audio(characters=len(sentences[-1])))

    text = "已确认的业务回复。" * 160
    speech = SpeechAPI(settings, transport=httpx.MockTransport(provider))
    data, usage = await speech.synthesize(text)
    assert "".join(sentences) == text
    with wave.open(io.BytesIO(data)) as audio:
        assert audio.getnframes() == 240 * len(sentences)
    assert len(usage["chunks"]) == len(sentences)


async def test_stream_tts_yields_before_provider_finishes_and_closes_connection(setup):
    from dataclasses import replace

    host, _, _ = setup
    released, closed = asyncio.Event(), asyncio.Event()

    class DelayedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            event = {"output": {"audio": {"data": base64.b64encode(b"\1\0").decode()}}}
            yield ("data: " + json.dumps(event) + "\n\n").encode()
            await released.wait()
            yield sse_audio()

        async def aclose(self):
            closed.set()

    speech = SpeechAPI(
        replace(host.settings, dashscope_api_key="private-key"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=DelayedStream())),
    )
    stream = speech.stream_synthesize("第一句话。第二句话。")
    assert await asyncio.wait_for(anext(stream), 1) == b"\1\0"
    assert not released.is_set()
    await stream.aclose()
    assert closed.is_set()


async def test_transcribe_provider_malformed_response_is_sanitized():
    speech = SpeechAPI(
        type(
            "Settings",
            (),
            {
                "dashscope_api_key": "private-key",
                "dashscope_base_url": "https://example.invalid",
                "asr_model": "asr",
            },
        )(),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": []})),
    )
    with pytest.raises(SpeechUnavailable) as error:
        await speech.transcribe(b"RIFF", "audio/wav")
    assert "list index" not in str(error.value)


@pytest.mark.parametrize(
    "event",
    [
        {"code": "InvalidApiKey", "message": "private-key"},
        {"output": {"audio": {"data": "invalid-base64"}}},
        {"output": {"audio": {"data": base64.b64encode(b"\0").decode()}}},
    ],
)
async def test_stream_tts_provider_errors_are_sanitized(setup, event):
    from dataclasses import replace

    host, _, _ = setup
    speech = SpeechAPI(
        replace(host.settings, dashscope_api_key="private-key"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=("data: " + json.dumps(event) + "\n\n").encode()
            )
        ),
    )
    with pytest.raises(SpeechUnavailable) as error:
        await anext(speech.stream_synthesize("回复"))
    assert "private" not in str(error.value)


async def test_interrupt_cancels_agent_keeps_session_and_recovers_next_turn(speech_stack):
    host, http, speech, rt, auth = speech_stack
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def wait(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    rt.agent.act = wait
    pending = asyncio.create_task(send_audio(http, rt, auth))
    await entered.wait()
    interrupted = await http.post(f"/api/sessions/{rt.id}/voice/interrupt", headers=auth, json={})
    assert interrupted.json() == {"generation": 1}
    assert (await pending).status_code == 409
    assert cancelled.is_set() and not rt.lock.locked() and rt.task is None
    assert host.store.get(rt.id)["status"] == "active"
    speech.transcribe.side_effect = None
    rt.agent.act = None
    resumed = await http.post(
        f"/api/sessions/{rt.id}/voice/turn?generation=1",
        headers={**auth, "Content-Type": "audio/wav"},
        content=b"audio",
    )
    assert resumed.status_code == 200 and resumed.json()["reply_message_id"]
    assert "HOST_CONTROL" in str(rt.agent.inputs[-1].content)
    assert all(
        "HOST_CONTROL" not in message["text"] for message in rt.service.snapshot(rt.ctx)["messages"]
    )
    stale = await http.post(
        f"/api/sessions/{rt.id}/voice/turn?generation=0",
        headers={**auth, "Content-Type": "audio/wav"},
        content=b"audio",
    )
    assert stale.status_code == 409 and stale.json()["detail"] == "voice_generation_stale"


@pytest.mark.parametrize("action", ["interrupt", "stop"])
async def test_full_wav_synthesis_is_cancelled_by_interrupt_or_hangup(speech_stack, action):
    host, http, speech, rt, auth = speech_stack
    reply = (await send_audio(http, rt, auth)).json()["reply_message_id"]
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def slow_tts(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    speech.synthesize.side_effect = slow_tts
    pending = asyncio.create_task(
        http.get(f"/api/sessions/{rt.id}/voice/audio/{reply}", headers=auth)
    )
    await entered.wait()
    suffix = "voice/interrupt" if action == "interrupt" else "stop"
    response = await http.post(f"/api/sessions/{rt.id}/{suffix}", headers=auth, json={})
    assert response.status_code == 200 and (await pending).status_code == 409
    assert cancelled.is_set() and not rt.speech_tasks
    assert host.store.get(rt.id)["status"] == ("active" if action == "interrupt" else "closed")


async def open_asgi_audio(http, rt, auth, reply):
    """直接观察 ASGI 首块，不让测试客户端把整个流先缓冲完。"""
    incoming, sent, first_audio = asyncio.Queue(), [], asyncio.Event()
    await incoming.put({"type": "http.request", "body": b"", "more_body": False})

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            first_audio.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/api/sessions/{rt.id}/voice/audio/{reply}",
        "raw_path": f"/api/sessions/{rt.id}/voice/audio/{reply}".encode(),
        "query_string": b"stream=true&generation=0",
        "root_path": "",
        "headers": [(b"host", b"localhost"), (b"authorization", auth["Authorization"].encode())],
        "client": ("127.0.0.1", 1234),
        "server": ("localhost", 8787),
    }
    task = asyncio.create_task(http._transport.app(scope, incoming.get, send))
    return task, incoming, sent, first_audio


@pytest.mark.parametrize("action", ["interrupt", "stop", "disconnect"])
async def test_stream_audio_first_chunk_and_cancellation(speech_stack, action):
    host, http, speech, rt, auth = speech_stack
    reply = (await send_audio(http, rt, auth)).json()["reply_message_id"]
    cancelled, finished = asyncio.Event(), asyncio.Event()

    async def pcm_stream(text, **kwargs):
        try:
            yield b"\1\0" * 480
            await finished.wait()
            yield b"\2\0" * 480
        finally:
            # 上游连接关闭也需要异步等待，不能只验证同步 finally 曾被进入。
            await asyncio.sleep(0.01)
            cancelled.set()

    speech.stream_synthesize = pcm_stream
    pending, incoming, sent, first = await open_asgi_audio(http, rt, auth, reply)
    await asyncio.wait_for(first.wait(), 1)
    assert not finished.is_set() and not pending.done()
    headers = dict(next(item for item in sent if item["type"] == "http.response.start")["headers"])
    assert headers[b"content-type"] == b"audio/pcm"
    assert headers[b"x-audio-sample-rate"] == b"24000"
    if action == "disconnect":
        await incoming.put({"type": "http.disconnect"})
    else:
        suffix = "voice/interrupt" if action == "interrupt" else "stop"
        assert (
            await http.post(f"/api/sessions/{rt.id}/{suffix}", headers=auth, json={})
        ).status_code == 200
    await asyncio.wait_for(pending, 2)
    await asyncio.wait_for(cancelled.wait(), 1)
    assert not rt.speech_tasks
    assert b"\2\0" * 480 not in [item.get("body") for item in sent]
    if action == "interrupt":
        stale = await http.get(
            f"/api/sessions/{rt.id}/voice/audio/{reply}?stream=true&generation=0", headers=auth
        )
        assert stale.status_code == 409 and stale.json()["detail"] == "voice_generation_stale"
        assert host.store.get(rt.id)["status"] == "active"


@pytest.mark.parametrize("action", ["interrupt", "disconnect"])
async def test_stream_cancel_before_first_audio_closes_provider(speech_stack, action):
    _, http, speech, rt, auth = speech_stack
    reply = (await send_audio(http, rt, auth)).json()["reply_message_id"]
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def pcm_stream(text, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
            yield b"\0\0"
        finally:
            cancelled.set()

    speech.stream_synthesize = pcm_stream
    pending, incoming, sent, _ = await open_asgi_audio(http, rt, auth, reply)
    await entered.wait()
    if action == "interrupt":
        await http.post(f"/api/sessions/{rt.id}/voice/interrupt", headers=auth, json={})
    else:
        await incoming.put({"type": "http.disconnect"})
    await asyncio.wait_for(pending, 2)
    assert cancelled.is_set() and not rt.speech_tasks
    assert not any(item.get("body", b"").startswith(b"\0\0") for item in sent)


async def test_stream_failure_before_first_audio_returns_actionable_error(speech_stack):
    _, http, speech, rt, auth = speech_stack
    reply = (await send_audio(http, rt, auth)).json()["reply_message_id"]

    async def broken(text, **kwargs):
        raise SpeechUnavailable("云端语音服务暂不可用。")
        yield b""

    speech.stream_synthesize = broken
    result = await http.get(f"/api/sessions/{rt.id}/voice/audio/{reply}?stream=true", headers=auth)
    assert result.status_code == 502 and not rt.speech_tasks
    assert "暂不可用" in result.json()["detail"]


async def test_stream_cancel_before_producer_first_run_does_not_wait_forever(speech_stack):
    from fastapi import HTTPException
    from starlette.requests import Request

    _, http, speech, rt, auth = speech_stack
    reply = (await send_audio(http, rt, auth)).json()["reply_message_id"]
    entered = asyncio.Event()

    async def pcm_stream(text, **kwargs):
        entered.set()
        await asyncio.Event().wait()
        yield b"\0\0"

    async def receive():
        await asyncio.Event().wait()

    speech.stream_synthesize = pcm_stream
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", auth["Authorization"].encode())],
        },
        receive,
    )
    speak = next(route.endpoint for route in http._transport.app.routes if route.name == "speak")
    pending = asyncio.create_task(speak(rt.id, reply, request, stream=True, generation=0))
    interrupted = asyncio.create_task(rt.interrupt(reply))
    assert await interrupted == 1
    with pytest.raises(HTTPException) as failure:
        await asyncio.wait_for(pending, 1)
    assert failure.value.status_code == 409 and not entered.is_set() and not rt.speech_tasks


@pytest.mark.parametrize("stage", ["model", "tool"])
async def test_native_agent_cancellation_propagates_and_next_turn_recovers(speech_stack, stage):
    from agentscope.credential import OpenAICredential
    from agentscope.model import OpenAIChatModel
    from agentscope.permission import PermissionBehavior, PermissionDecision
    from agentscope.tool import FunctionTool, Toolkit
    from openai.types.chat import ChatCompletionChunk

    from backend.agent_harness.agent import create_agent

    _, http, _, rt, auth = speech_stack
    entered, calls = asyncio.Event(), []

    class Stream:
        def __init__(self, first):
            self.first = first

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def __aiter__(self):
            return self.chunks()

        async def chunks(self):
            if self.first and stage == "tool":
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "wait_tool", "arguments": "{}"},
                        }
                    ]
                }
                finish = "tool_calls"
            else:
                delta = {"content": "尚未播放的文字" if self.first else "新的回复"}
                finish = None if self.first else "stop"
            yield ChatCompletionChunk(
                id="mock",
                created=1,
                model="mock",
                object="chat.completion.chunk",
                choices=[{"index": 0, "delta": delta, "finish_reason": finish}],
            )
            if self.first and stage == "model":
                entered.set()
                await asyncio.Event().wait()

    async def create(**kwargs):
        calls.append(kwargs)
        return Stream(len(calls) == 1)

    async def wait_tool() -> str:
        """等待取消，验证框架清理工具后仍能正常处理下一轮。"""
        entered.set()
        await asyncio.Event().wait()
        return "完成"

    model = OpenAIChatModel(
        credential=OpenAICredential(api_key="test-only"),
        model="deepseek-chat",
        stream=True,
    )
    await model.client.close()
    model.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=AsyncMock(),
    )
    rt.model_client = model.client
    rt.agent = create_agent(
        model=model,
        prompt="测试语音插话",
        state=AgentState(session_id=rt.id),
        max_iterations=3,
        toolkit=Toolkit(
            tools=[
                FunctionTool(
                    wait_tool,
                    is_concurrency_safe=False,
                    permission=PermissionDecision(
                        behavior=PermissionBehavior.ALLOW, message="测试工具"
                    ),
                )
            ]
        ),
    )
    pending = asyncio.create_task(send_audio(http, rt, auth))
    await asyncio.wait_for(entered.wait(), 2)
    await http.post(f"/api/sessions/{rt.id}/voice/interrupt", headers=auth, json={})
    assert (await pending).status_code == 409
    assert not rt.agent.state.get_unfinished_tool_calls(rt.agent.name)
    resumed = await send_audio(http, rt, auth)
    assert resumed.status_code == 200
    messages = rt.service.snapshot(rt.ctx)["messages"]
    assert messages[-1]["text"] == "新的回复"
    assert "HOST_CONTROL" in str(calls[-1]["messages"])
    assert not any("Sorry" in message["text"] for message in messages)


async def wait_for_generation(rt, expected):
    """让请求进入插话屏障，不依赖机器快慢设置固定等待时间。"""
    async with asyncio.timeout(1):
        while rt.voice_generation < expected:
            await asyncio.sleep(0)


@pytest.mark.parametrize(
    "first_seconds", [2, 60], ids=["pause_and_continue", "continuous_60s_split"]
)
async def test_continued_speech_preserves_previous_asr_once_without_old_reply(
    speech_stack, first_seconds
):
    _, http, speech, rt, auth = speech_stack
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    first_text, second_text = "我在杭州，想了解注册条件。", "补充一下，我有三年驾龄，还没有车。"
    recording = io.BytesIO()
    with wave.open(recording, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 16000 * first_seconds)

    async def delayed_asr(audio, mime):
        entered.set()
        try:
            await release.wait()
            return first_text, None
        except asyncio.CancelledError:
            cancelled.set()
            raise

    speech.transcribe.side_effect = delayed_asr
    pending = asyncio.create_task(send_audio(http, rt, auth, recording.getvalue(), "audio/wav"))
    await entered.wait()
    interrupted = asyncio.create_task(
        http.post(f"/api/sessions/{rt.id}/voice/interrupt", headers=auth, json={})
    )
    await wait_for_generation(rt, 1)
    assert not interrupted.done() and not cancelled.is_set()
    assert not rt.agent.inputs
    release.set()
    assert (await interrupted).json() == {"generation": 1}
    assert (await pending).status_code == 409
    assert not cancelled.is_set() and rt.input_task is None
    assert [m["text"] for m in rt.service.snapshot(rt.ctx)["messages"]] == [first_text]
    assert first_text in str(rt.agent.state.context) and first_text in str(
        rt.framework_state().context
    )
    assert not rt.agent.inputs and not rt.reply_interrupted

    speech.transcribe.side_effect = None
    speech.transcribe.return_value = (second_text, None)
    response = await http.post(
        f"/api/sessions/{rt.id}/voice/turn?generation=1",
        headers={**auth, "Content-Type": "audio/wav"},
        content=b"next-audio",
    )
    assert response.status_code == 200 and len(rt.agent.inputs) == 1
    assert second_text in str(rt.agent.inputs[-1].content)
    customers = [
        m["text"]
        for m in rt.service.snapshot(rt.ctx)["messages"]
        if m["origin"] == "customer_transcript"
    ]
    assert customers == [first_text, second_text]
    assert speech.transcribe.await_count == 2


async def test_hangup_cancels_preserved_asr_without_waiting_for_interrupt_lock(speech_stack):
    host, http, speech, rt, auth = speech_stack
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked_asr(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    speech.transcribe.side_effect = blocked_asr
    pending = asyncio.create_task(send_audio(http, rt, auth))
    await entered.wait()
    interrupted = asyncio.create_task(
        http.post(f"/api/sessions/{rt.id}/voice/interrupt", headers=auth, json={})
    )
    await wait_for_generation(rt, 1)
    assert rt.interrupt_lock.locked()
    stopped = await asyncio.wait_for(
        http.post(f"/api/sessions/{rt.id}/stop", headers=auth, json={}), 1
    )
    assert stopped.status_code == 200 and cancelled.is_set()
    assert (await asyncio.wait_for(pending, 1)).status_code == 409
    await asyncio.wait_for(interrupted, 1)
    assert host.store.get(rt.id)["status"] == "closed"
    assert rt.input_task is None and not rt.interrupt_lock.locked()
    assert not rt.service.snapshot(rt.ctx)["messages"]


async def test_preserved_asr_failure_releases_barrier_for_next_speech(speech_stack):
    _, http, speech, rt, auth = speech_stack
    entered, release = asyncio.Event(), asyncio.Event()

    async def failed_asr(*args):
        entered.set()
        await release.wait()
        raise SpeechUnavailable("语音识别暂不可用。")

    speech.transcribe.side_effect = failed_asr
    pending = asyncio.create_task(send_audio(http, rt, auth))
    await entered.wait()
    interrupted = asyncio.create_task(
        http.post(f"/api/sessions/{rt.id}/voice/interrupt", headers=auth, json={})
    )
    await wait_for_generation(rt, 1)
    release.set()
    assert (await interrupted).json() == {"generation": 1}
    assert (await pending).status_code == 502
    speech.transcribe.side_effect = None
    resumed = await http.post(
        f"/api/sessions/{rt.id}/voice/turn?generation=1",
        headers={**auth, "Content-Type": "audio/wav"},
        content=b"next-audio",
    )
    assert resumed.status_code == 200 and len(rt.agent.inputs) == 1
    assert rt.input_task is None and not rt.interrupting


async def test_input_only_segments_are_saved_once_and_seen_by_next_native_agent(speech_stack):
    from agentscope.credential import OpenAICredential
    from agentscope.model import OpenAIChatModel
    from agentscope.tool import Toolkit
    from openai.types.chat import ChatCompletionChunk

    from backend.agent_harness.agent import create_agent

    _, http, speech, rt, auth = speech_stack
    texts = ["我在杭州，想了解注册条件。", "我有三年驾龄。", "还没有车，能先报名吗？"]
    speech.transcribe.side_effect = [(text, None) for text in texts]
    calls = []

    class CompletedStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def __aiter__(self):
            yield ChatCompletionChunk(
                id="mock",
                created=1,
                model="mock",
                object="chat.completion.chunk",
                choices=[
                    {
                        "index": 0,
                        "delta": {"content": "收到了，先核对驾驶员条件。"},
                        "finish_reason": "stop",
                    }
                ],
            )

    async def create(**kwargs):
        calls.append(kwargs)
        return CompletedStream()

    model = OpenAIChatModel(
        credential=OpenAICredential(api_key="test-only"),
        model="deepseek-chat",
        stream=True,
    )
    await model.client.close()
    model.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=AsyncMock(),
    )

    def start_agent():
        rt.model_client = model.client
        rt.agent = create_agent(
            model=model,
            prompt="测试连续客户输入",
            toolkit=Toolkit(),
            state=rt.framework_state(),
            max_iterations=2,
        )

    rt.agent = None
    rt.start_agent = start_agent
    for _ in range(2):
        response = await http.post(
            f"/api/sessions/{rt.id}/voice/turn?generation=0&input_only=true",
            headers={**auth, "Content-Type": "audio/wav"},
            content=b"segment",
        )
        assert response.status_code == 200 and response.json()["reply_message_id"] is None
        assert rt.agent is None and not calls
    assert len(rt.framework_state().context) == 2
    response = await http.post(
        f"/api/sessions/{rt.id}/voice/turn?generation=0",
        headers={**auth, "Content-Type": "audio/wav"},
        content=b"last-segment",
    )
    assert response.status_code == 200 and response.json()["reply_message_id"]
    assert len(calls) == 1
    model_input = json.dumps(calls[0]["messages"], ensure_ascii=False)
    assert all(model_input.count(text) == 1 for text in texts)
    customers = [
        m for m in rt.service.snapshot(rt.ctx)["messages"] if m["origin"] == "customer_transcript"
    ]
    assert [m["text"] for m in customers] == texts and len({m["id"] for m in customers}) == 3
