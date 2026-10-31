"""使用本地时钟和真实业务工具，验证客户推理不占用 Agent 的会话预算。"""

import asyncio
import copy
import inspect
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent_harness import BackendService, load_scenario
from backend.agent_harness.conversation import Conversation
from backend.agent_harness.memory.worker import ProfileWorker
from backend.agent_harness.runtime.snapshots import digest
from backend.settings import Settings
from evaluation.common import trace as trace_module
from evaluation.dialogue.runtime import EvaluationClock, EvaluationSessionManager
from tests.db import database_for
from tests.paths import ROOT


def test_clock_excludes_nested_waits_and_resumes_after_errors():
    elapsed = [10.0]
    clock = EvaluationClock(epoch=1000.0, monotonic=lambda: elapsed[0])
    elapsed[0] += 2
    assert clock() == 1002

    with pytest.raises(ValueError), clock.pause():
        elapsed[0] += 100
        assert clock() == 1002
        with clock.pause():
            elapsed[0] += 50
            assert clock() == 1002
        elapsed[0] += 20
        raise ValueError("客户模型失败仍须恢复计时")

    elapsed[0] += 3
    assert clock() == 1005


@pytest.mark.parametrize("failed", [False, True])
async def test_agent_response_timing_excludes_trace_export_but_keeps_generation_and_tools(
    monkeypatch, failed,
):
    """响应完成即停止计时，不能把前后读取评测事件的耗时计入客户等待。"""
    elapsed = [0.0]
    monkeypatch.setattr(trace_module, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    tool = {"seq": 1, "type": "tool_result", "data": {"name": "search_policy"}}

    class Store:
        def events(self, session_id, after=0, limit=3000):
            elapsed[0] += 10
            return [tool] if elapsed[0] > 10 else []

    async def message(text):
        # 模型生成和工具执行都发生于 message 调用内部，合计七秒。
        elapsed[0] += 3
        conversation.events.first_text_at = elapsed[0]
        await asyncio.sleep(0)
        elapsed[0] += 4
        if failed:
            raise ValueError("本地模拟生成异常")
        return "资料说明不收取报名费。"

    turn = {"turn": 1, "origin": "customer", "user_message": "报名费是多少？"}
    manager = SimpleNamespace(store=Store())
    conversation = SimpleNamespace(
        id="timing-test", message=message, events=SimpleNamespace(first_text_at=None),
    )
    if failed:
        with pytest.raises(ValueError, match="本地模拟"):
            await trace_module.record_agent_turn(manager, conversation, turn)
    else:
        await trace_module.record_agent_turn(manager, conversation, turn)

    assert turn["elapsed_seconds"] == 7
    assert turn["first_token_seconds"] == (None if failed else 3)
    assert elapsed[0] == 27
    assert turn["tool_events"] == [tool]
    assert ("reply" in turn) is not failed


@pytest.fixture
async def runtime_factory(tmp_path):
    managers = []

    async def create(*, budget=0.3, clock=None):
        root = tmp_path / str(len(managers))
        settings = Settings(ROOT, root, database_for(root), session_max_seconds=budget)
        original = load_scenario(ROOT / "backend/scenarios/meituan_rider/scenario.yaml")
        data = copy.deepcopy(original.data)
        data["policies"]["session_timeout_seconds"] = budget
        scenario = replace(
            original, data=data,
            snapshot_hash=digest({"config": data, "resources": original.resources}),
        )
        clock = clock or EvaluationClock()
        service = BackendService(settings.app_db, scenario, clock=clock)
        manager = EvaluationSessionManager(settings, {scenario.id: service})
        manager.profile_worker = ProfileWorker(settings, extractor=AsyncMock(return_value={"updates": []}))
        manager._policy_index = SimpleNamespace(search=Mock(return_value={
            "status": "no_data", "data": {"reason": "local_test"}, "evidence": [],
        }))
        managers.append(manager)
        conversation = await manager.create(scenario.id, "clock-customer")
        return manager, conversation

    yield create
    for manager in managers:
        await manager.close()


async def test_customer_wait_preserves_budget_and_agent_time_still_expires(runtime_factory):
    manager, conversation = await runtime_factory()
    original_expiry = conversation.expiry_task

    async with manager.customer_wait(conversation):
        frozen_remaining = conversation.remaining_seconds
        await asyncio.sleep(0.4)
        assert original_expiry.cancelled()
        assert conversation.remaining_seconds == frozen_remaining
        assert conversation.service.session(conversation.ctx)["state"] == "active"
        assert conversation.tool_denial("estimate_work_budget") is None

    assert 0 < conversation.remaining_seconds <= frozen_remaining
    assert conversation.expiry_task is not original_expiry
    await asyncio.wait_for(conversation.stop_done.wait(), timeout=1)
    assert conversation.service.session(conversation.ctx)["state"] == "closed"
    assert conversation.service.session(conversation.ctx)["reason"] == "session_timeout"


async def test_evaluation_preserves_real_business_tool_interfaces(runtime_factory):
    manager, conversation = await runtime_factory(budget=5)
    baseline = Conversation(
        conversation.service, conversation.ctx, settings=conversation.settings,
        store=manager.store, search_policy=lambda query: {},
    )
    assert conversation.tools.keys() == baseline.tools.keys()
    for name in conversation.tools:
        assert inspect.signature(conversation.tools[name]) == inspect.signature(baseline.tools[name])

    result = await conversation.tools["estimate_work_budget"](
        work_days=10, monthly_fixed_cost=1000, daily_variable_cost=20, refundable_deposit=500,
    )
    assert result["status"] == "success"
    assert result["data"]["calculation"]["monthly_operating_cost_yuan"] == 1200
    assert result["data"]["calculation"]["first_month_cash_budget_yuan"] == 1700
    await conversation.tools["search_policy"](query="报名条件")
    manager.policy_index.search.assert_called_once_with("报名条件", "meituan_rider")


async def test_cancelled_customer_wait_restores_clock_and_expiry(runtime_factory):
    manager, conversation = await runtime_factory()
    entered = asyncio.Event()

    async def customer():
        async with manager.customer_wait(conversation):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(customer())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert conversation.expiry_task is not None
    await asyncio.wait_for(conversation.stop_done.wait(), timeout=1)
    assert conversation.service.session(conversation.ctx)["reason"] == "session_timeout"


async def test_evaluation_does_not_extend_the_three_hundred_second_cap(runtime_factory):
    elapsed = [0.0]
    clock = EvaluationClock(epoch=1000, monotonic=lambda: elapsed[0])
    manager, conversation = await runtime_factory(budget=1000, clock=clock)
    assert conversation.remaining_seconds == 300
    elapsed[0] = 250
    async with manager.customer_wait(conversation):
        elapsed[0] += 600
        assert conversation.remaining_seconds == 50
    assert conversation.remaining_seconds == 50
    elapsed[0] += 51
    assert conversation.tool_denial("estimate_work_budget") == "session_timeout"
    await asyncio.wait_for(conversation.stop_done.wait(), timeout=1)
