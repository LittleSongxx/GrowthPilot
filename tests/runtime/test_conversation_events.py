"""验证会话事件的来源和原生事件结构，不发起模型网络请求。"""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agentscope.event import (
    ModelCallEndEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultTextDeltaEvent,
)

from backend.agent_harness import BackendService, load_scenario
from backend.agent_harness.runtime import events as events_module
from backend.agent_harness.runtime.events import ConversationEvents
from tests.db import database_for


@pytest.fixture
def conversation(tmp_path):
    scenario = load_scenario(
        Path(__file__).resolve().parents[2] / "backend/scenarios/didi_driver/scenario.yaml"
    )
    service = BackendService(database_for(tmp_path / "events"), scenario)
    context = service.create_session("event-customer", channel="voice")
    records = []
    conversation = SimpleNamespace(
        id=context.session_id,
        service=service,
        ctx=context,
        channel="voice",
        end_after_playback=False,
        emit=lambda kind, data: records.append((kind, data)),
        records=records,
        checkpoint=Mock(),
        stop=AsyncMock(),
    )
    conversation.events = ConversationEvents(conversation)
    yield conversation
    service.close()


def reply(conversation, rid, role):
    return ReplyStartEvent(session_id=conversation.id, reply_id=rid, name=role, role=role)


@pytest.mark.asyncio
async def test_generated_text_usage_and_last_reply_do_not_infer_playback(conversation):
    conversation.channel = "text"
    events = conversation.events
    assert events.last_assistant_text == ""
    await events.process(reply(conversation, "assistant-1", "assistant"))
    await events.process(TextBlockDeltaEvent(reply_id="assistant-1", block_id="t", delta="查询中"))
    await events.process({"type": "MODEL_CALL_START", "reply_id": "assistant-1"})
    await events.process(
        TextBlockDeltaEvent(reply_id="assistant-1", block_id="t", delta="现有资料未确认。")
    )
    await events.process(
        ReplyEndEvent(
            session_id=conversation.id, reply_id="assistant-1", finished_reason="interrupted"
        )
    )
    assert events.last_assistant_text == "现有资料未确认。"
    outcome = next(data for kind, data in conversation.records if kind == "reply_outcome")
    assert outcome["finished_reason"] == "interrupted" and "playback_completed" not in outcome
    conversation.checkpoint.assert_called_once()
    await events.process(
        ModelCallEndEvent(
            reply_id="assistant-1", input_tokens=123, output_tokens=45, cache_input_tokens=6
        )
    )
    usage = next(data for kind, data in conversation.records if kind == "usage")
    assert usage["input_tokens"] == 123 and usage["output_tokens"] == 45
    assert usage["cache_input_tokens"] == 6 and usage["model_name"] is None
    await events.process(reply(conversation, "assistant-2", "assistant"))
    assert (
        events.last_assistant_text == ""
    )  # 新回复为空时，不能返回上一条回答。


@pytest.mark.asyncio
async def test_native_skill_trace_hashes_received_result_not_declared_resource(conversation):
    events = conversation.events
    skill = conversation.service.scenario.data["skills"][0]
    args = '{"skill":"' + skill["id"] + '"}'
    actual_body = "框架实际返回的 Skill 内容；与磁盘正文不同。"
    for event in [
        ToolCallStartEvent(reply_id="r", tool_call_id="call", tool_call_name="Skill"),
        ToolCallDeltaEvent(reply_id="r", tool_call_id="call", delta=args[:7]),
        ToolCallDeltaEvent(reply_id="r", tool_call_id="call", delta=args[7:]),
        ToolCallEndEvent(reply_id="r", tool_call_id="call"),
        ToolResultTextDeltaEvent(reply_id="r", tool_call_id="call", delta=actual_body),
        ToolResultEndEvent(reply_id="r", tool_call_id="call", state="success"),
    ]:
        await events.process(event)
    loaded = next(data for kind, data in conversation.records if kind == "skill_loaded")
    assert loaded["skill_id"] == skill["id"] and loaded["version"] == skill["version"]
    assert loaded["body_sha256"] == hashlib.sha256(actual_body.encode()).hexdigest()
    assert (
        loaded["body_sha256"]
        != hashlib.sha256(
            conversation.service.scenario.resources[skill["ref"]].encode()
        ).hexdigest()
    )
    result = next(data for kind, data in conversation.records if kind == "tool_result")
    assert result["result"] == {"text": actual_body} and result["state"] == "success"
    assert events.tool_calls == {}


@pytest.mark.asyncio
async def test_non_json_audit_fallback_does_not_hide_programming_errors_or_audio(conversation):
    events = conversation.events
    events.observe_tool_event(
        {"type": "TOOL_CALL_START", "tool_call_id": "bad", "tool_call_name": "search_policy"}
    )
    events.observe_tool_event(
        {"type": "TOOL_CALL_DELTA", "tool_call_id": "bad", "delta": "not-json"}
    )
    events.observe_tool_event({"type": "TOOL_CALL_END", "tool_call_id": "bad"})
    start = next(data for kind, data in conversation.records if kind == "tool_start")
    assert start["arguments"] == {"unparsed": "not-json"}
    with pytest.raises(TypeError):
        events.observe_tool_event(
            {"type": "TOOL_RESULT_TEXT_DELTA", "tool_call_id": "bad", "delta": None}
        )
    for kind in ["DATA_BLOCK_DELTA", "TOOL_RESULT_DATA_DELTA"]:
        await events.process(
            {"type": kind, "tool_call_id": "audio", "data": "private-audio-base64"}
        )
    assert "private-audio-base64" not in str(conversation.records)
    assert "private-provider-detail" not in str(conversation.records)


@pytest.mark.parametrize("local_goodbye", [False, True])
async def test_first_text_tracks_final_visible_content_across_model_iterations(
    conversation, monkeypatch, local_goodbye,
):
    """首 token 只取最终文本，忽略思考、工具内容、空块及被替换的中间说明。"""
    elapsed = [1.0]
    monkeypatch.setattr(events_module, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    events = conversation.events
    for kind in ("REPLY_START", "MODEL_CALL_START"):
        await events.process({"type": kind, "reply_id": "r"})
    for kind, delta in (
        ("THINKING_BLOCK_DELTA", "内部思考"),
        ("TOOL_CALL_DELTA", "工具参数"),
        ("TOOL_RESULT_TEXT_DELTA", "查询结果"),
        ("TEXT_BLOCK_DELTA", ""),
        ("TEXT_BLOCK_DELTA", " \n"),
    ):
        await events.process({"type": kind, "reply_id": "r", "delta": delta})
    assert events.first_text_at is None
    elapsed[0] = 2.0
    await events.process({"type": "TEXT_BLOCK_DELTA", "reply_id": "r", "delta": "我先查询"})
    assert events.first_text_at == 2.0

    elapsed[0] = 5.0
    conversation.end_after_playback = local_goodbye
    await events.process({"type": "MODEL_CALL_START", "reply_id": "r"})
    assert events.first_text_at is None
    elapsed[0] = 9.0
    await events.process({"type": "TEXT_BLOCK_DELTA", "reply_id": "r", "delta": "最终"})
    elapsed[0] = 12.0
    await events.process({"type": "TEXT_BLOCK_DELTA", "reply_id": "r", "delta": "回复"})
    await events.process({"type": "REPLY_END", "reply_id": "r", "finished_reason": "completed"})
    assert events.first_text_at == 9.0
    assert events.last_assistant_text == "最终回复"

    await events.process({"type": "REPLY_START", "reply_id": "next"})
    assert events.first_text_at is None
