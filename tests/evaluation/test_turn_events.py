"""验证长会话的评测证据分页完整，且回答异常时仍保留本轮事件。"""

from types import SimpleNamespace

import pytest

from evaluation.checks.knowledge_probe import record_agent_turn
from evaluation.common import trace as trace_module


class EventStore:
    """复现运行时按序号升序分页的事件读取契约。"""

    def __init__(self, events):
        self.rows = events

    def events(self, session_id, after=0, limit=3000):
        return [event for event in self.rows if event["seq"] > after][:limit]


def tool_event(seq):
    return {
        "seq": seq,
        "type": "tool_result",
        "data": {"name": "search_policy", "state": "success"},
    }


async def test_more_than_one_page_of_old_events_stays_outside_the_new_turn():
    store = EventStore([tool_event(seq) for seq in range(1, 3002)])

    class Conversation:
        id = "long-session"

        async def message(self, message):
            store.rows.append(tool_event(3002))
            return "本轮回复"

    turn = {"turn": 2, "user_message": "继续核对"}
    await record_agent_turn(SimpleNamespace(store=store), Conversation(), turn)

    assert turn["reply"] == "本轮回复"
    assert [event["seq"] for event in turn["tool_events"]] == [3002]
    assert turn["retrieval_events"] == turn["tool_events"]


@pytest.mark.parametrize("fails", [False, True])
async def test_large_turn_keeps_all_event_pages_even_when_reply_fails(fails):
    store = EventStore([tool_event(1)])

    class Conversation:
        id = "large-turn"

        async def message(self, message):
            store.rows.extend(tool_event(seq) for seq in range(2, 3004))
            store.rows.extend(
                [
                    {
                        "seq": 3004,
                        "type": "business_query_audit",
                        "data": {"name": "query_delivery_pay"},
                    },
                    {
                        "seq": 3005,
                        "type": "skill_loaded",
                        "data": {"skill_id": "meituan-rider-work-fit"},
                    },
                ]
            )
            if fails:
                raise TimeoutError("模拟回答中断")
            return "本轮回复"

    turn = {"turn": 2, "user_message": "核对这轮结果"}
    if fails:
        with pytest.raises(TimeoutError, match="模拟回答中断"):
            await record_agent_turn(SimpleNamespace(store=store), Conversation(), turn)
        assert "reply" not in turn
    else:
        await record_agent_turn(SimpleNamespace(store=store), Conversation(), turn)
        assert turn["reply"] == "本轮回复"

    assert [event["seq"] for event in turn["tool_events"]] == list(range(2, 3004))
    assert turn["retrieval_events"] == turn["tool_events"]
    assert [event["seq"] for event in turn["business_audit_events"]] == [3004]
    assert [event["seq"] for event in turn["skill_events"]] == [3005]


@pytest.mark.parametrize("first_text_at", [None, 9.0, 18.0, True, float("nan")])
async def test_first_token_missing_or_outside_this_turn_stays_unknown(monkeypatch, first_text_at):
    """旧记录或上一轮时刻不能用完整回复耗时补成首 token 指标。"""
    elapsed = [10.0]
    monkeypatch.setattr(trace_module, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))

    async def message(text):
        elapsed[0] = 17.0
        return "当前回复"

    conversation = SimpleNamespace(
        id="timing", message=message, events=SimpleNamespace(first_text_at=first_text_at),
    )
    turn = {"turn": 1, "origin": "customer", "user_message": "请介绍报名条件"}
    await record_agent_turn(SimpleNamespace(store=EventStore([])), conversation, turn)
    assert turn["elapsed_seconds"] == 7.0
    assert turn["first_token_seconds"] is None
