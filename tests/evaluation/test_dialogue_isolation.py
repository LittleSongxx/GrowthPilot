"""用真实会话和 PostgreSQL 验证评测记忆隔离；模型边界全部使用本地替身。"""

import asyncio
import json
import time
from copy import deepcopy

import pytest

from backend.agent_harness import BackendService, agent, load_scenario
from backend.settings import Settings
from evaluation.common.io import digest
from evaluation.dialogue import runner
from evaluation.dialogue.runtime import (
    EvaluationClock,
    EvaluationConversation,
    EvaluationSessionManager,
)
from evaluation.judges.schema import QUALITY_DIMENSIONS
from tests.db import database_for, schema_exists, snapshot_database
from tests.paths import ROOT


def _case(case_id="memory-isolation-rider", area="上海"):
    return {
        "id": case_id,
        "scenario_id": "meituan_rider",
        "category": "咨询",
        "goal": {"type": "consultation"},
        "customer": {"profile": f"我在{area}"},
        "max_turns": 1,
    }


def _area_patch(message_id, area):
    return {
        "updates": [
            {
                "field": "area",
                "value": area,
                "evidence": {"message_id": message_id, "quote": f"我在{area}"},
            }
        ]
    }


@pytest.fixture
def isolated_real_runtime(monkeypatch, tmp_path):
    settings = Settings(
        project_root=ROOT,
        runtime_dir=tmp_path / "production",
        app_db=database_for(tmp_path / "production"),
        agent_api_key="local-only",
    )
    scenario = load_scenario(ROOT / "backend/scenarios/meituan_rider/scenario.yaml")
    production = BackendService(settings.app_db, scenario, workspace_id="dialogue-evaluation")
    # 故意复用评测的租户和客户标识：测试不能仅依赖不同 ID 避开旧数据。
    for case_id in (_case()["id"], "memory-isolation-other"):
        customer_id = "eval-" + digest(case_id)[:20]
        previous = production.create_session(customer_id)
        message = production.add_message(previous, "我在北京")["data"]["message_id"]
        production.end_session(previous)
        with production._tx():
            production.profiles.apply(previous, _area_patch(message, "北京"))
        following = production.create_session(customer_id)
        assert production.profiles.baseline(following)["area"]["value"] == "北京"
        production.end_session(following)
    production.close()
    original = snapshot_database(settings.app_db)
    state = {
        "managers": [],
        "initial": [],
        "final_memory": [],
        "profile_payloads": [],
        "customer_waiting": asyncio.Event(),
        "profile_waiting": asyncio.Event(),
    }

    def prohibit_model(*_args, **_kwargs):
        raise AssertionError("隔离回归测试禁止创建真实模型")

    async def extract(_settings, payload):
        state["profile_payloads"].append(deepcopy(payload))
        if state.get("block") == "profile":
            state["profile_waiting"].set()
            await asyncio.Event().wait()
        message = next(item for item in payload["messages"] if item["origin"] == "customer")
        return _area_patch(message["id"], message["text"].removeprefix("我在"))

    class ObservedManager(EvaluationSessionManager):
        def __init__(self, isolated_settings, services):
            super().__init__(isolated_settings, services)
            self.profile_worker.extractor = extract
            state["managers"].append(self)

        async def close(self):
            for conversation in self.sessions.values():
                state["final_memory"].append(
                    conversation.service.read_memory(conversation.ctx)["data"]["fields"]
                )
            await super().close()

    async def local_message(conversation, text, origin="customer"):
        # 保留真实输入持久化、画像读取、事件和会话收尾，只替换付费模型生成。
        conversation.check_available()
        if origin == "host_control":
            state["initial"].append(
                {
                    "database": conversation.settings.app_db,
                    "run_id": conversation.ctx.run_id,
                    "customer_id": conversation.ctx.customer_id,
                    "profile": conversation.business_summary()["profile"],
                    "memory": conversation.service.read_memory(conversation.ctx)["data"]["fields"],
                    "context": list(conversation.framework_state().context),
                }
            )
        conversation.record_input(text, origin)
        conversation.emit("framework", {"type": "MODEL_CALL_START"})
        conversation.emit("usage", {"input_tokens": 10, "output_tokens": 3})
        reply = "您好，请问现在方便吗？" if origin == "host_control" else "收到，再见。"
        conversation.service.add_message(conversation.ctx, reply, origin="agent")
        conversation.events.first_text_at = time.monotonic()
        return reply

    class LocalCustomer:
        def __init__(self, _settings, context, *, history=None, call_number=1):
            self.calls = []
            self.text = context["profile"]
            self.history = deepcopy(history or [])

        async def respond(self, _message, _events):
            if state.get("block") == "customer":
                state["customer_waiting"].set()
                await asyncio.Event().wait()
            return {"text": self.text, "done": True}

        async def close(self):
            pass

        def end_call(self, *_args):
            pass

        def export_history(self):
            return self.history

    async def local_review(_settings, trace, _case, _output):
        trace["review"] = {
            dimension: {
                "score": 5, "reason": "按客户意愿结束对话。",
            }
            for dimension in QUALITY_DIMENSIONS
        }

    monkeypatch.setattr(agent, "create_model", prohibit_model)
    monkeypatch.setattr(runner, "SessionManager", ObservedManager)
    monkeypatch.setattr(EvaluationConversation, "message", local_message)
    monkeypatch.setattr(runner, "OpenAICompatibleCustomer", LocalCustomer)
    monkeypatch.setattr(runner, "_review", local_review)
    yield settings, state
    assert snapshot_database(settings.app_db) == original


async def test_customer_memory_survives_within_run_and_resets_between_runs(
    isolated_real_runtime,
    tmp_path,
):
    settings, state = isolated_real_runtime
    cases = [_case(), _case("memory-isolation-other", "杭州")]
    # 同一轮两位客户并行、每位客户的通话串行；新一轮沿用同一客户 ID 验证彻底隔离。
    for run in ("first", "second"):
        summary = await runner.execute(settings, cases, tmp_path / run, repeats=2, concurrency=16)
        assert summary["status"] == "completed"

    assert len(state["initial"]) == len(state["final_memory"]) == 8
    databases = {item["database"] for item in state["initial"]}
    assert len(databases) == 2
    for run in ("first", "second"):
        run_entries = [item for item in state["initial"] if item["run_id"] == run]
        assert len({item["database"] for item in run_entries}) == 1
        for case, area in zip(cases, ("上海", "杭州"), strict=True):
            customer_id = "eval-" + digest(case["id"])[:20]
            calls = [item for item in run_entries if item["customer_id"] == customer_id]
            assert len(calls) == 2
            assert calls[0]["profile"] == {} and calls[0]["memory"] == []
            assert calls[1]["profile"] == {"area": {"value": area, "status": "known"}}
            assert [field["value"] for field in calls[1]["memory"]] == [area]
            assert all(item["context"] == [] for item in calls)
    assert all(
        len(fields) == 1 and fields[0]["value"] in {"上海", "杭州"}
        for fields in state["final_memory"]
    )
    assert sum(payload["previous_profile"] == {} for payload in state["profile_payloads"]) == 4
    assert len(state["profile_payloads"]) == 8
    assert all(not schema_exists(database) for database in databases)
    assert all(manager.profile_worker.task is None for manager in state["managers"])


@pytest.mark.parametrize("stage", ["customer", "profile"])
async def test_cancellation_removes_isolated_database_after_background_tasks_stop(
    isolated_real_runtime,
    tmp_path,
    stage,
):
    settings, state = isolated_real_runtime
    state["block"] = stage
    output = tmp_path / "cancelled"
    task = asyncio.create_task(runner.execute(settings, [_case()], output, repeats=1))
    try:
        await asyncio.wait_for(state[f"{stage}_waiting"].wait(), timeout=5)
        assert all(schema_exists(manager.settings.app_db) for manager in state["managers"])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert all(not schema_exists(manager.settings.app_db) for manager in state["managers"])
    assert all(manager.profile_worker.task is None for manager in state["managers"])
    traces = json.loads((output / "traces.json").read_text())
    assert len(traces) == 1 and traces[0]["status"] == "interrupted"
    summary = json.loads((output / "summary.json").read_text())
    assert summary["counts"]["expected_runs"] == 1
    assert summary["quality"]["goal_completion"]["unassessable_runs"] == 1


async def test_closing_one_customer_does_not_claim_or_cancel_another_profile(tmp_path):
    """共享数据库后，案例 A 的画像 worker 不得领取或中断案例 B 的任务。"""
    settings = Settings(ROOT, tmp_path, database_for(tmp_path))
    scenario = load_scenario(ROOT / "backend/scenarios/meituan_rider/scenario.yaml")
    managers = []
    for _ in range(2):
        service = BackendService(
            settings.app_db, scenario, workspace_id="dialogue-evaluation", clock=EvaluationClock()
        )
        managers.append(EvaluationSessionManager(settings, {scenario.id: service}))
    first, second = managers
    started, released = asyncio.Event(), asyncio.Event()
    cancelled = []

    async def unexpected(_settings, _payload):
        raise AssertionError("没有客户消息的案例 A 不应领取案例 B 的画像任务")

    async def delayed(_settings, payload):
        started.set()
        try:
            await released.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        message = next(item for item in payload["messages"] if item["origin"] == "customer")
        return _area_patch(message["id"], "上海")

    first.profile_worker.extractor = unexpected
    second.profile_worker.extractor = delayed
    try:
        assert first.profile_worker.claim() is None
        a = await first.create(scenario.id, "customer-a")
        b = await second.create(scenario.id, "customer-b")
        assert first.profile_worker.session_ids == {a.id}
        assert second.profile_worker.session_ids == {b.id}
        b.service.add_message(b.ctx, "我在上海")
        await b.stop()
        await asyncio.wait_for(started.wait(), timeout=5)
        await first.close()
        assert not cancelled
        released.set()
        async with asyncio.timeout(5):
            while b.service.profiles.state(b.ctx)["status"] != "completed":
                await asyncio.sleep(0.01)
        assert [field["value"] for field in b.service.read_memory(b.ctx)["data"]["fields"]] == [
            "上海"
        ]
        assert not cancelled
    finally:
        released.set()
        await asyncio.gather(*(manager.close() for manager in managers))
