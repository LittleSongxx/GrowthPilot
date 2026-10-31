"""使用本地替身验证动态对话边界，不请求真实模型或业务服务。"""

import asyncio
import json
import subprocess
import sys
import textwrap
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from backend.settings import Settings
from evaluation.dialogue import customer, runner
from evaluation.judges.schema import JUDGE_VERSION, QUALITY_DIMENSIONS
from tests.db import database_for, schema_exists
from tests.paths import ROOT


def sample_case():
    return {
        "id": "rider-1", "scenario_id": "meituan_rider", "topic": "报名", "category": "拒绝",
        "customer": {
            "profile": "暂时不找工作", "initial_state": "未安装",
            "concerns": ["不想继续接听"], "acceptance_conditions": [],
            "refusal_conditions": ["明确拒绝本次介绍"], "disclosure_rules": ["开场后说不需要"],
        },
        "goal": {"type": "refusal", "description": "隐藏目标标记", "success_conditions": ["结束"]},
        "max_turns": 12, "source_case_ids": [],
    }


@pytest.fixture
def local_runtime(monkeypatch, tmp_path):
    state = {"managers": [], "customers": [], "customer_inputs": [], "messages": []}

    class Store:
        def __init__(self):
            self.rows = []

        def events(self, session_id, after=0, limit=3000):
            return [row for row in self.rows if row["seq"] > after][:limit]

        def save(self, session_id, **values):
            state.setdefault("saved", []).append(values)

        def emit(self, kind, data):
            self.rows.append({"seq": len(self.rows) + 1, "type": kind, "data": data})

    class Service:
        def __init__(self, app_db, scenario, workspace_id, clock):
            self.clock = clock
            self.profiles = SimpleNamespace(
                state=lambda context: {"status": "completed"}, baseline=lambda context: {},
            )

        def close(self):
            pass

    class Conversation:
        def __init__(self, manager):
            self.id = "session-" + str(len(state["managers"]))
            self.ctx = SimpleNamespace(snapshot_hash="snapshot-1")
            self.service = next(iter(manager.services.values()))
            self.manager = manager
            self.end_after_playback = False
            self.number = 0
            self.events = SimpleNamespace(first_text_at=None)

        async def message(self, text, origin="customer"):
            state["messages"].append((text, origin))
            self.number += 1
            self.manager.store.emit("framework", {"type": "MODEL_CALL_START"})
            if state.get("cancel_agent"):
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            if state.get("agent_error"):
                raise state["agent_error"]("本地异常")
            self.manager.store.emit("usage", {"input_tokens": 10, "output_tokens": 4})
            if self.number == state.get("end_after"):
                self.end_after_playback = True
            self.events.first_text_at = time.monotonic()
            return "业务人员的自然回复"

        async def stop(self, reason):
            state.setdefault("stop_reasons", []).append(reason)

    class Manager:
        def __init__(self, settings, services):
            self.settings, self.services = settings, services
            self.store = Store()
            state["managers"].append(self)

        async def create(self, scenario_id, customer_id, run_id):
            state.setdefault("customer_ids", []).append(customer_id)
            return Conversation(self)

        @asynccontextmanager
        async def customer_wait(self, conversation):
            yield

        async def close(self):
            state["closed_managers"] = state.get("closed_managers", 0) + 1

    class Customer:
        def __init__(self, settings, context, *, history=None, call_number=1):
            self.calls = []
            self.context = deepcopy(context)
            self.initial_history = deepcopy(history or [])
            self.messages = deepcopy(history or [])
            self.call_number = call_number
            state["customers"].append(self)

        async def respond(self, message, visible_events):
            self.messages.append({"agent_message": message, "visible_events": visible_events})
            state["customer_inputs"].append((message, visible_events))
            self.calls.append({"usage": {"prompt_tokens": 5, "completion_tokens": 2}})
            if state.get("customer_error"):
                raise ValueError("模拟无效 JSON")
            return {"text": "我不需要，谢谢", "done": state.get("customer_done", True)}

        def end_call(self, message=None, visible_events=None):
            self.messages.append({"agent_message": message, "visible_events": visible_events})

        def export_history(self):
            return deepcopy(self.messages)

        async def close(self):
            if state.get("customer_close_wait"):
                state["customer_close_started_count"] = state.get("customer_close_started_count", 0) + 1
                if state["customer_close_started_count"] == state.get("expected_closing_customers", 1):
                    state["customer_close_started"].set()
                await state["customer_close_wait"].wait()
            state["closed_customers"] = state.get("closed_customers", 0) + 1

    async def judge(settings, trace, case, diagnostics=None):
        diagnostics["usage"] = {"prompt_tokens": 20, "completion_tokens": 5}
        if state.get("judge_wait"):
            state["judge_started_count"] = state.get("judge_started_count", 0) + 1
            if state["judge_started_count"] == state.get("expected_judges", 1):
                state["judge_started"].set()
            await state["judge_wait"].wait()
        if state.get("judge_error"):
            raise ValueError("模拟无效评分")
        return {
            dimension: {
                "score": 5, "reason": "本地替身",
            }
            for dimension in QUALITY_DIMENSIONS
        }

    monkeypatch.setattr(runner, "BackendService", Service)
    monkeypatch.setattr(runner, "SessionManager", Manager)
    monkeypatch.setattr(runner, "load_scenario", lambda path: {
        "identity": {"role": "骑手报名服务助手", "app_name": "美团骑手 App"},
    })
    monkeypatch.setattr(runner, "PolicyIndex", lambda path, **kwargs: SimpleNamespace(runtime_dir=path, database=kwargs["database"]))
    monkeypatch.setattr(runner, "OpenAICompatibleCustomer", Customer)
    monkeypatch.setitem(sys.modules, "evaluation.judges.review", SimpleNamespace(review_trace=judge))
    settings = Settings(
        project_root=tmp_path, runtime_dir=tmp_path / "runtime", app_db=database_for(tmp_path),
        agent_api_key="local-test-key",
    )
    return settings, state


async def test_dynamic_runner_keeps_same_customer_across_calls_and_keeps_goals_private(
    local_runtime, tmp_path,
):
    settings, state = local_runtime
    summary = await runner.execute(settings, [sample_case()], tmp_path / "report")
    assert summary["status"] == "completed"
    assert summary["metrics"]["average_agent_tokens"] == 28
    assert len(state["managers"]) == len(state["customers"]) == 2
    assert len({manager.settings.app_db for manager in state["managers"]}) == 1
    assert len(set(state["customer_ids"])) == 1
    assert [actor.call_number for actor in state["customers"]] == [1, 2]
    assert state["customers"][0].initial_history == []
    assert state["customers"][1].initial_history == state["customers"][0].messages
    assert state["customers"][0].calls is not state["customers"][1].calls
    assert all(manager._policy_index.runtime_dir == settings.runtime_dir / "knowledge"
               for manager in state["managers"])
    assert [origin for _, origin in state["messages"]].count("host_control") == 2
    assert [origin for _, origin in state["messages"]].count("customer") == 2
    assert len({id(next(iter(manager.services.values())).clock)
                for manager in state["managers"]}) == 2
    assert "隐藏目标标记" not in json.dumps(state["messages"], ensure_ascii=False)
    assert "隐藏目标标记" not in json.dumps(
        [item.context for item in state["customers"]], ensure_ascii=False,
    )
    assert state["closed_managers"] == state["closed_customers"] == 2
    traces = json.loads((tmp_path / "report/traces.json").read_text())
    assert all([turn["origin"] for turn in trace["turns"]] == ["host_control", "customer"]
               for trace in traces)
    assert all(trace["end_reason"] == "customer_done" for trace in traces)
    assert all(trace["judge_usage"]["prompt_tokens"] == 20 for trace in traces)
    assert all(trace["memory_mode"] == "fresh_database_per_run" for trace in traces)
    assert all(trace["judge_version"] == JUDGE_VERSION for trace in traces)
    assert all(trace["business_context"]["app_name"] == "美团骑手 App" for trace in traces)
    assert all(trace["initial_profile"] == {} for trace in traces)
    assert not schema_exists(state["managers"][0].settings.app_db)


async def test_turns_persist_individual_cases_and_write_aggregate_only_once(
    local_runtime, tmp_path, monkeypatch,
):
    settings, _state = local_runtime
    saved_paths = []
    original_dump = runner.dump

    def recording_dump(path, value):
        saved_paths.append(path)
        original_dump(path, value)

    monkeypatch.setattr(runner, "dump", recording_dump)
    second = deepcopy(sample_case())
    second["id"] = "rider-2"
    output = tmp_path / "report"
    await runner.execute(settings, [sample_case(), second], output, repeats=2)

    assert saved_paths.count(output / "traces.json") == 1
    aggregate = json.loads((output / "traces.json").read_text())
    per_case = [json.loads(path.read_text()) for path in (output / "traces").glob("*.json")]
    def identity(trace):
        return trace["case_id"], trace["repeat"]

    assert len(per_case) == 4
    assert sorted(per_case, key=identity) == sorted(aggregate, key=identity)
    case_paths = {path for path in saved_paths if path.parent == output / "traces"}
    assert len(case_paths) == 4
    assert all(saved_paths.count(path) > 1 for path in case_paths)


@pytest.mark.parametrize("end_after, expected_customer_calls", [(1, 0), (2, 1)])
async def test_agent_end_call_stops_after_text_delivery(
    local_runtime, tmp_path, end_after, expected_customer_calls,
):
    settings, state = local_runtime
    state.update(end_after=end_after, customer_done=False)
    await runner.execute(settings, [sample_case()], tmp_path / "report", repeats=1)
    assert len(state["customer_inputs"]) == expected_customer_calls
    trace = json.loads((tmp_path / "report/traces.json").read_text())[0]
    assert trace["end_reason"] == "agent_end_call"


async def test_max_turns_limits_customer_messages_without_a_second_script_mode(
    local_runtime, tmp_path,
):
    settings, state = local_runtime
    state["customer_done"] = False
    case = sample_case()
    case["max_turns"] = 2
    await runner.execute(settings, [case], tmp_path / "report", repeats=1)
    assert len(state["customer_inputs"]) == 2
    assert len(state["messages"]) == 3


@pytest.mark.parametrize("failure, expected", [
    ("customer_error", "customer_error"), ("judge_error", "judge_error"),
    ("agent_error", "agent_error"), ("cancel_agent", "timeout"),
])
async def test_failure_categories_and_partial_trace_are_preserved(
    local_runtime, tmp_path, failure, expected,
):
    settings, state = local_runtime
    state[failure] = ValueError if failure == "agent_error" else True
    summary = await runner.execute(settings, [sample_case()], tmp_path / "report", repeats=1)
    trace = json.loads((tmp_path / "report/traces.json").read_text())[0]
    assert trace["status"] == expected
    assert trace["turns"][0]["events"]
    assert state["closed_managers"] == 1
    assert summary["quality"]["goal_completion"]["unassessable_runs"] == 1
    assert summary["metrics"]["goal_completion"] is None


def test_preview_does_not_construct_any_runtime_or_client(local_runtime, tmp_path):
    settings, state = local_runtime
    summary = runner.run(settings, tmp_path / "report", [sample_case()])
    assert summary["status"] == "preview"
    assert summary["model_requests"] == 0
    assert summary["concurrency"] == 16
    assert not state["managers"] and not state["customers"]
    assert not (tmp_path / "report").exists()


@pytest.mark.parametrize("concurrency", [0, -1, True, 1.5, "16", None])
async def test_invalid_concurrency_fails_before_creating_any_runtime(
    local_runtime, tmp_path, concurrency,
):
    settings, state = local_runtime
    with pytest.raises(ValueError, match="concurrency"):
        await runner.execute(settings, [sample_case()], tmp_path / "report", concurrency=concurrency)
    with pytest.raises(ValueError, match="concurrency"):
        runner.run(settings, tmp_path / "preview", [sample_case()], concurrency=concurrency)
    assert not state["managers"] and not state["customers"]
    assert not (tmp_path / "report").exists()


async def test_default_concurrency_limits_complete_trials_including_profile_finish(
    local_runtime, tmp_path, monkeypatch,
):
    settings, state = local_runtime
    blocked, release = asyncio.Event(), asyncio.Event()
    waiting_profiles = 0
    maximum_waiting = 0

    async def finish(conversation):
        nonlocal waiting_profiles, maximum_waiting
        waiting_profiles += 1
        maximum_waiting = max(maximum_waiting, waiting_profiles)
        if waiting_profiles == 16:
            blocked.set()
        try:
            await release.wait()
        finally:
            waiting_profiles -= 1
        return {"status": "completed"}

    monkeypatch.setattr(runner, "finish_conversation", finish)
    cases = [sample_case() | {"id": f"rider-{number}"} for number in range(18)]
    output = tmp_path / "report"
    task = asyncio.create_task(runner.execute(settings, cases, output, repeats=1))
    try:
        await asyncio.wait_for(blocked.wait(), timeout=2)
        assert len(state["managers"]) == len(state["customers"]) == 16
        assert state.get("closed_managers", 0) == 0
        assert not (output / "summary.json").exists()
    finally:
        release.set()
        summary = await asyncio.wait_for(task, timeout=2)

    assert maximum_waiting == 16
    assert summary["concurrency"] == 16
    assert summary["counts"]["expected_runs"] == summary["counts"]["recorded_runs"] == 18
    assert state["closed_managers"] == state["closed_customers"] == 18
    traces = json.loads((output / "traces.json").read_text())
    assert [(trace["case_id"], trace["repeat"]) for trace in traces] == [
        (case["id"], 1) for case in cases
    ]


async def test_same_customer_next_call_waits_for_profile_and_previous_runtime_close(
    local_runtime, tmp_path, monkeypatch,
):
    settings, state = local_runtime
    blocked, release = asyncio.Event(), asyncio.Event()
    finished = 0

    async def finish(conversation):
        nonlocal finished
        finished += 1
        if finished == 2:
            blocked.set()
        await release.wait()
        return {"status": "completed"}

    monkeypatch.setattr(runner, "finish_conversation", finish)
    cases = [sample_case() | {"id": f"rider-{number}"} for number in range(2)]
    output = tmp_path / "report"
    task = asyncio.create_task(runner.execute(settings, cases, output))
    try:
        await asyncio.wait_for(blocked.wait(), timeout=2)
        assert len(state["managers"]) == 2
        assert len(set(state["customer_ids"])) == 2
        assert [actor.call_number for actor in state["customers"]] == [1, 1]
        assert state.get("closed_managers", 0) == 0
    finally:
        release.set()
        summary = await asyncio.wait_for(task, timeout=2)

    assert summary["repeat_semantics"] == "sequential_customer_calls"
    assert summary["counts"]["recorded_runs"] == 4
    assert len({manager.settings.app_db for manager in state["managers"]}) == 1
    assert state["closed_managers"] == state["closed_customers"] == 4
    assert sorted(actor.call_number for actor in state["customers"]) == [1, 1, 2, 2]


async def test_concurrent_cancellation_waits_for_every_worker_and_keeps_planned_denominator(
    local_runtime, tmp_path,
):
    settings, state = local_runtime
    state.update(
        judge_wait=asyncio.Event(), judge_started=asyncio.Event(), expected_judges=3,
        customer_close_wait=asyncio.Event(), customer_close_started=asyncio.Event(),
        expected_closing_customers=3,
    )
    cases = [sample_case() | {"id": f"rider-{number}"} for number in range(5)]
    output = tmp_path / "report"
    task = asyncio.create_task(runner.execute(settings, cases, output, concurrency=3))
    try:
        await asyncio.wait_for(state["judge_started"].wait(), timeout=2)
        assert len(state["managers"]) == 3
        task.cancel()
        await asyncio.wait_for(state["customer_close_started"].wait(), timeout=2)
        # 再次中断也不能跳过正在释放的连接，或在其他工作者仍写入时提前汇总。
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not (output / "summary.json").exists()
        assert not (output / "traces.json").exists()
        assert len(state["managers"]) == 3
    finally:
        state["customer_close_wait"].set()
        state["judge_wait"].set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    assert state["closed_managers"] == state["closed_customers"] == 3
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == "incomplete" and summary["concurrency"] == 3
    assert summary["counts"]["expected_runs"] == 10
    assert summary["quality"]["goal_completion"]["unassessable_runs"] == 10
    assert summary["counts"]["recorded_runs"] == 3
    assert summary["errors"] == {"interrupted": 3, "missing_run": 7}
    traces = json.loads((output / "traces.json").read_text())
    assert all(trace["status"] == "interrupted" for trace in traces)
    for trace in traces:
        path = output / "traces" / (runner.digest([trace["case_id"], trace["repeat"]])[:20] + ".json")
        assert json.loads(path.read_text()) == trace


def test_repeated_real_sigint_finishes_cleanup_and_restores_handler():
    # 独立进程实际发送两次 SIGINT，防止信号测试干扰运行 pytest 的宿主。
    script = textwrap.dedent('''
        import asyncio
        import json
        import signal
        import tempfile
        from pathlib import Path
        from pytest import MonkeyPatch
        from evaluation.dialogue import runner
        from tests.evaluation.test_dynamic_dialogue import local_runtime, sample_case, schema_exists

        with tempfile.TemporaryDirectory() as directory, MonkeyPatch.context() as patch:
            root = Path(directory)
            settings, state = local_runtime.__wrapped__(patch, root)
            previous = signal.getsignal(signal.SIGINT)

            async def review(*args):
                asyncio.get_running_loop().call_soon(signal.raise_signal, signal.SIGINT)
                await asyncio.Event().wait()

            async def close(customer):
                signal.raise_signal(signal.SIGINT)
                await asyncio.sleep(0.02)
                state["closed_customers"] = state.get("closed_customers", 0) + 1

            patch.setattr(runner, "_review", review)
            patch.setattr(runner.OpenAICompatibleCustomer, "close", close)
            output = root / "report"
            try:
                runner.run(settings, output, [sample_case()], execute=True, repeats=1)
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("真实信号必须保持中断结果")
            assert signal.getsignal(signal.SIGINT) is previous
            assert state["closed_customers"] == state["closed_managers"] == 1
            assert not schema_exists(state["managers"][0].settings.app_db)
            trace = json.loads((output / "traces.json").read_text())[0]
            assert trace["status"] == "interrupted"
            summary = json.loads((output / "summary.json").read_text())
            assert summary["status"] == "incomplete"
            assert summary["counts"]["expected_runs"] == 1
            print("两次真实 SIGINT 后所有资源均已关闭")
    ''')
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "所有资源均已关闭" in result.stdout


@pytest.mark.parametrize("content", [
    "不是JSON", "[]", '{"text":"", "done":false}', '{"text":"再见", "done":"true"}',
    '{"text":"再见", "done":true,"score":1}',
])
def test_customer_protocol_errors_are_regular_parse_errors(content):
    with pytest.raises((ValueError, TypeError)):
        customer.parse_response(content)


def test_customer_context_and_sms_view_exclude_hidden_targets_and_tool_audits():
    context = sample_case()["customer"] | {"goal": "不该出现", "expected_answer": "不该出现"}
    assert set(customer.customer_context(context)) == set(customer.CUSTOMER_FIELDS)
    assert customer.parse_response('{"text":"我改主意了", "done":true}') == {
        "text": "我改主意了", "done": True,
    }
    turn = {"tool_events": [
        {"type": "tool_result", "data": {
            "name": "search_policy", "state": "success", "result": {"secret": "内部查询内容"},
        }},
        {"type": "tool_result", "data": {
            "name": "send_download_sms", "state": "success", "result": {
                "status": "success", "data": {
                    "delivery_status": "sent", "app_name": "美团众包", "message_id": "内部ID",
                }, "evidence": ["内部证据"],
            },
        }},
    ], "business_audit_events": [{"hidden": "内部审计"}]}
    assert customer.visible_sms_events(turn) == [{
        "type": "download_sms", "app_name": "美团众包", "delivery_status": "sent",
    }]
    turn["tool_events"][1]["data"]["result"]["status"] = "denied"
    assert customer.visible_sms_events(turn) == []


async def test_customer_sdk_context_is_independent_and_keeps_usage_on_parse_failure(monkeypatch):
    captured = []

    class Client:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def create(self, **kwargs):
            captured.append(deepcopy(kwargs))
            return SimpleNamespace(
                usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 7, "completion_tokens": 2}),
                choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content='{"text":"你好"}'),
                )],
            )

        async def close(self):
            pass

    monkeypatch.setattr(customer, "AsyncOpenAI", Client)
    settings = SimpleNamespace(customer_api_key="test", customer_base_url="https://example.com",
                               customer_model="customer-test", customer_temperature=0.7,
                               customer_max_tokens=700)
    actor = customer.OpenAICompatibleCustomer(settings, sample_case()["customer"])
    with pytest.raises(ValueError):
        await actor.respond("现在方便吗？")
    assert actor.calls[0]["status"] == "error"
    assert actor.calls[0]["usage"]["prompt_tokens"] == 7
    assert "隐藏目标标记" not in json.dumps(captured, ensure_ascii=False)
    assert captured[0]["model"] == settings.customer_model
    await actor.close()


async def test_profile_finish_waits_for_a_terminal_state_and_exposes_timeout():
    states = iter([{"status": "running"}, {"status": "completed"}])
    conversation = SimpleNamespace(
        ctx=SimpleNamespace(),
        service=SimpleNamespace(profiles=SimpleNamespace(state=lambda context: next(states))),
    )

    async def stop(reason):
        pass

    conversation.stop = stop
    assert (await runner.finish_conversation(conversation, timeout=1))["status"] == "completed"
    conversation.service.profiles.state = lambda context: {"status": "running"}
    assert await runner.finish_conversation(conversation, timeout=0) == {
        "status": "timeout", "last_state": {"status": "running"},
    }


async def test_external_cancellation_is_not_misreported_as_agent_timeout(monkeypatch):
    started = asyncio.Event()

    async def waiting_turn(manager, conversation, turn):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "record_agent_turn", waiting_turn)
    task = asyncio.create_task(runner._record_turn(None, None, {}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("phase", ["judge", "profile", "customer_close"])
async def test_cancellation_during_finishing_closes_resources_and_saves_partial_results(
    local_runtime, tmp_path, monkeypatch, phase,
):
    settings, state = local_runtime
    waiting, started = asyncio.Event(), asyncio.Event()
    if phase == "profile":
        async def finish(conversation):
            started.set()
            await waiting.wait()

        monkeypatch.setattr(runner, "finish_conversation", finish)
    else:
        state[f"{phase}_wait"] = waiting
        state[f"{phase}_started"] = started
    output = tmp_path / "report"
    task = asyncio.create_task(runner.execute(settings, [sample_case()], output, repeats=1))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        # 清理阶段自身受保护，释放本地替身后应继续关闭所有剩余资源。
        waiting.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    assert state["closed_managers"] == state["closed_customers"] == 1
    trace = json.loads((output / "traces.json").read_text())[0]
    assert trace["status"] == "interrupted"
    assert trace["turns"] and trace["profile_usage_events"] == []
    saved_case = json.loads(next((output / "traces").glob("*.json")).read_text())
    assert saved_case == trace
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == "incomplete"
    assert summary["counts"]["expected_runs"] == 1
    assert summary["quality"]["goal_completion"]["unassessable_runs"] == 1
    if phase == "judge":
        diagnostic = json.loads(next((output / "judge").glob("*.json")).read_text())
        assert diagnostic["status"] == "interrupted"
        assert diagnostic["usage"]["prompt_tokens"] == 20
