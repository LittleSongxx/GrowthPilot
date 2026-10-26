"""从真实会话读取完整事件，保留失败轮次和供应商原始用量。"""

import asyncio
import time
from math import isfinite


def session_events(store, session_id, after=0):
    """分页读取事件，不因长对话超过单页上限而丢失工具或用量。"""
    events = []
    while True:
        page = store.events(session_id, after=after, limit=3000)
        events.extend(page)
        if len(page) < 3000:
            return events
        after = page[-1]["seq"]


async def record_agent_turn(manager, conversation, turn):
    """按事件序号隔离本轮；模型异常时也保存已经发生的调用。"""
    before = session_events(manager.store, conversation.id)
    after = max((event["seq"] for event in before), default=0)
    started = time.monotonic()
    try:
        async with asyncio.timeout(90):
            if turn.get("origin", "customer") == "customer":
                turn["reply"] = await conversation.message(turn["user_message"])
            else:
                turn["reply"] = await conversation.message(
                    turn["user_message"],
                    origin=turn["origin"],
                )
    finally:
        finished = time.monotonic()
        # 完整回复耗时保留用于诊断；主指标单独记录最终回复首个非空文本块的到达时间。
        turn["elapsed_seconds"] = round(finished - started, 6)
        first_text_at = getattr(getattr(conversation, "events", None), "first_text_at", None)
        reply = turn.get("reply")
        turn["first_token_seconds"] = (
            round(first_text_at - started, 6)
            if isinstance(reply, str) and reply.strip()
            and type(first_text_at) in {int, float}
            and isfinite(first_text_at)
            and started <= first_text_at <= finished
            else None
        )
        events = session_events(manager.store, conversation.id, after=after)
        turn["events"] = events
        turn["tool_events"] = [
            event for event in events if event["type"] in {"tool_start", "tool_result"}
        ]
        turn["business_audit_events"] = [
            event for event in events if event["type"] == "business_query_audit"
        ]
        turn["skill_events"] = [event for event in events if event["type"] == "skill_loaded"]
        turn["retrieval_events"] = [
            event for event in turn["tool_events"] if event["data"].get("name") == "search_policy"
        ]
        turn["usage_events"] = [event for event in events if event["type"] == "usage"]


# 兼容历史知识探针的分页辅助入口。
_session_events = session_events
