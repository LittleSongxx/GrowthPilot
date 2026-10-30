"""通过真实文本 Conversation 运行动态客户案例，每次完整评测使用独立数据库。"""

from __future__ import annotations

import asyncio
import inspect
import secrets
import signal
import tempfile
import threading
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from backend.agent_harness import BackendService, load_scenario
from backend.agent_harness.rag.index import PolicyIndex
from backend.database import temporary_database
from backend.settings import model_connection
from evaluation.common.io import digest, dump
from evaluation.common.trace import record_agent_turn, session_events
from evaluation.judges.schema import JUDGE_VERSION

from .customer import OpenAICompatibleCustomer, visible_sms_events
from .metrics import summarize
from .runtime import EvaluationClock
from .runtime import EvaluationSessionManager as SessionManager

OPENING = (
    "[HOST_CONTROL: 主动开场，非客户话语/非同意/不可作为画像证据] "
    "请简短说明身份与本次联系背景，询问现在是否方便。"
)
PROFILE_FINISH_TIMEOUT_SECONDS = 210


def _business_context(scenario, conversation):
    """只提供已注入业务 Agent 的身份和工具能力，不把能力声明当作执行回执。"""
    functions = dict(getattr(conversation, "tools", {}))
    return {
        "app_name": scenario["identity"]["app_name"],
        "agent_role": scenario["identity"]["role"],
        "tool_capabilities": [
            {"name": name, "description": (inspect.getdoc(function) or "").split("\n\nArgs:")[0]}
            for name, function in functions.items()
        ],
    }


def _save_trace(output, trace):
    """逐轮只保存当前案例，避免重复序列化已经完成的全部对话。"""
    filename = digest([trace["case_id"], trace["repeat"]])[:20] + ".json"
    dump(output / "traces" / filename, trace)


async def finish_conversation(conversation, *, timeout=PROFILE_FINISH_TIMEOUT_SECONDS):
    """关闭主对话后等画像任务明确结束，避免删除临时库时取消仍在执行的提取。"""
    await conversation.stop("evaluation_complete")
    deadline = time.monotonic() + timeout
    while True:
        state = conversation.service.profiles.state(conversation.ctx)
        if state["status"] in {"completed", "failed", "skipped", "unavailable"}:
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"status": "timeout", "last_state": state}
        await asyncio.sleep(min(0.25, remaining))


async def _review(settings, trace, case, output):
    """评审只在对话结束后读取隐藏目标，评审失败与业务失败分别保留。"""
    diagnostics = {
        "case_id": case["id"], "repeat": trace["repeat"], "judge_version": JUDGE_VERSION,
    }
    filename = digest([case["id"], trace["repeat"]])[:20] + ".json"
    if not any(turn.get("reply") for turn in trace["turns"]):
        # 没有实际回复时保留缺测，不把运行故障自动转成任何质量分数。
        trace["judge_usage"] = None
        diagnostics.update(status="not_requested", reason="没有实际回复")
        dump(output / "judge" / filename, diagnostics)
        return
    try:
        from evaluation.judges.review import review_trace

        trace["review"] = await review_trace(settings, trace, case, diagnostics=diagnostics)
    except asyncio.CancelledError:
        diagnostics.update(status="interrupted", error_category="CancelledError")
        raise
    except Exception as error:  # noqa: BLE001 - 无效评分保留为未判定，不写供应商异常正文。
        trace["judge_error_category"] = type(error).__name__
        if trace["status"] == "completed":
            trace["status"] = "judge_error"
        diagnostics.update(status="error")
        diagnostics.setdefault("error_category", type(error).__name__)
    finally:
        trace["judge_usage"] = diagnostics.get("usage")
        dump(output / "judge" / filename, diagnostics)


async def _record_turn(manager, conversation, turn):
    """独立任务承接前台生成，避免会话超时取消整个案例运行器。"""
    try:
        await asyncio.create_task(record_agent_turn(manager, conversation, turn))
    except asyncio.CancelledError:
        # 宿主超时会取消当前生成；外部取消评测时则继续向上传播，不能吞掉用户中断。
        if not asyncio.current_task().cancelling():
            raise TimeoutError("会话在生成过程中终止") from None
        raise
    if not isinstance(turn.get("reply"), str) or not turn["reply"].strip():
        raise ValueError("Agent 没有交付非空文本回复")


async def _release_resources(manager, conversation, customer, service, trace):
    """关闭客户端与后台任务后再读取用量，任何单项清理错误都不能跳过其余资源。"""
    try:
        if customer is not None:
            try:
                await customer.close()
            except Exception as error:  # noqa: BLE001 - 释放连接失败不覆盖已保存的客户输出。
                trace["customer_close_error_category"] = type(error).__name__
    finally:
        try:
            if manager is not None:
                await manager.close()
            elif service is not None:
                service.close()
        except Exception as error:  # noqa: BLE001 - 清理错误单独保存，不丢弃其余案例。
            trace["runtime_close_error_category"] = type(error).__name__
        finally:
            if conversation is not None:
                try:
                    events = session_events(manager.store, conversation.id)
                    trace["profile_usage_events"] = [
                        event for event in events if event["type"] == "profile_usage"
                    ]
                except Exception as error:  # noqa: BLE001 - 导出错误不覆盖已有主对话证据。
                    trace["profile_usage_error_category"] = type(error).__name__


async def _cleanup_case(manager, conversation, customer, service, trace, output, started):
    """中断只取消评测工作，资源释放必须在整次评测的临时数据库删除之前完成。"""
    cleanup = asyncio.create_task(_release_resources(manager, conversation, customer, service, trace))
    cancelled = False
    try:
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                if cleanup.cancelled():
                    raise
                cancelled = True
                trace.update(status="interrupted", error_category="CancelledError")
        cleanup.result()
    finally:
        trace["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _save_trace(output, trace)
    if cancelled:
        raise asyncio.CancelledError


async def _execute_case(settings, case, output, repeat, traces, isolated, continuity):
    customer_id = "eval-" + digest(case["id"])[:20]
    trace = {
        "case_id": case["id"], "scenario_id": case["scenario_id"], "repeat": repeat,
        "category": case.get("category"), "goal_type": case["goal"]["type"],
        "status": "running", "turns": [], "customer_calls": [],
        "external_conversion": "not_measured", "customer_id": customer_id,
        "memory_mode": "fresh_database_per_run",
        "judge_version": JUDGE_VERSION,
        "customer_history_before_call": deepcopy(continuity.get("history", [])),
    }
    traces.append(trace)
    started, stage = time.monotonic(), "infrastructure"
    manager = conversation = customer = service = None
    observed_turn = -1
    try:
        scenario = load_scenario(
            settings.project_root / "backend/scenarios" / case["scenario_id"] / "scenario.yaml"
        )
        service = BackendService(
            isolated.app_db, scenario, workspace_id="dialogue-evaluation",
            clock=EvaluationClock(),
        )
        manager = SessionManager(isolated, {case["scenario_id"]: service})
        # 检索读取已有索引；同次评测共享新库，同一客户的后续通话沿用已沉淀画像。
        manager._policy_index = PolicyIndex(settings.runtime_dir / "knowledge", database=settings.app_db)
        conversation = await manager.create(
            case["scenario_id"], customer_id,
            run_id=output.name,
        )
        trace.update(
            session_id=conversation.id, scenario_snapshot=conversation.ctx.snapshot_hash,
            initial_profile=conversation.service.profiles.baseline(conversation.ctx),
            business_context=_business_context(scenario, conversation),
        )
        customer = OpenAICompatibleCustomer(
            settings, case["customer"], history=continuity.get("history"), call_number=repeat,
        )
        trace["customer_calls"] = customer.calls
        stage = "agent"
        turn = {"turn": 0, "user_message": OPENING, "origin": "host_control"}
        trace["turns"].append(turn)
        await _record_turn(manager, conversation, turn)
        manager.store.save(conversation.id, opening_sent=1)
        _save_trace(output, trace)
        trace["end_reason"] = "max_turns"
        if conversation.end_after_playback:
            trace["end_reason"] = "agent_end_call"
        else:
            for number in range(1, case.get("max_turns", 12) + 1):
                stage = "customer"
                async with manager.customer_wait(conversation):
                    observed_turn = turn["turn"]
                    response = await customer.respond(turn["reply"], visible_sms_events(turn))
                turn = {
                    "turn": number, "user_message": response["text"],
                    "origin": "customer", "customer_done": response["done"],
                }
                trace["turns"].append(turn)
                stage = "agent"
                await _record_turn(manager, conversation, turn)
                _save_trace(output, trace)
                # 文本已完整交付，等价于产品中的播放完成；结束后不再续喂客户消息。
                if conversation.end_after_playback:
                    trace["end_reason"] = "agent_end_call"
                    break
                if response["done"]:
                    trace["end_reason"] = "customer_done"
                    break
        trace["status"] = "completed"
    except asyncio.CancelledError:
        trace.update(status="interrupted", error_category="CancelledError")
        raise
    except Exception as error:  # noqa: BLE001 - 单案错误不能丢失其他案例和已完成的调用。
        if stage == "agent":
            timed_out = isinstance(error, TimeoutError) or (
                isinstance(error, RuntimeError) and str(error) == "session_expired"
            )
            trace["status"] = "timeout" if timed_out else "agent_error"
            trace["turns"][-1]["error_category"] = type(error).__name__
        else:
            trace["status"] = "customer_error" if stage == "customer" else "infrastructure_error"
        trace["error_category"] = type(error).__name__
    finally:
        try:
            if customer is not None:
                # 结束后不再让客户模型作答，仅保留其已听到的末句和短信供下次通话使用。
                delivered = [turn for turn in trace["turns"] if turn.get("reply")]
                last_turn = delivered[-1] if delivered else None
                if last_turn is not None and last_turn["turn"] > observed_turn:
                    customer.end_call(last_turn["reply"], visible_sms_events(last_turn))
                else:
                    customer.end_call()
                continuity["history"] = customer.export_history()
            if conversation is not None:
                try:
                    # 先关会话释放前台计时器；画像任务在独立后台继续收尾。
                    await conversation.stop(trace.get("end_reason", "evaluation_error"))
                except Exception as error:  # noqa: BLE001 - 收尾失败仍保留主对话记录。
                    trace["stop_error_category"] = type(error).__name__
            if trace["status"] in {"completed", "agent_error", "timeout"}:
                await _review(settings, trace, case, output)
            if conversation is not None:
                try:
                    if trace["status"] == "interrupted":
                        trace["profile_processing"] = {"status": "interrupted"}
                    else:
                        trace["profile_processing"] = await finish_conversation(conversation)
                except Exception as error:  # noqa: BLE001 - 画像收尾不是单独的业务效果评分。
                    trace["profile_processing"] = {
                        "status": "error", "error_category": type(error).__name__,
                    }
        except asyncio.CancelledError:
            trace.update(status="interrupted", error_category="CancelledError")
            trace.setdefault("profile_processing", {"status": "interrupted"})
            raise
        finally:
            await _cleanup_case(
                manager, conversation, customer, service, trace, output, started,
            )


def _validate_concurrency(concurrency):
    """并发上限必须是正整数，避免无工作者时生成看似执行过的摘要。"""
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("concurrency 必须是正整数")


async def execute(settings, cases: list, output: Path, *, repeats=2, concurrency=16):
    """不同客户并发，同一客户连续通话；完整评测之间隔离记忆。"""
    # 执行前完成本地计划校验，防止已经付费运行后才发现重复 ID 或无效分母。
    _validate_concurrency(concurrency)
    summarize([], cases, repeats=repeats)
    if not model_connection(settings).api_key:
        raise ValueError("真实对话评测需要 AGENT_API_KEY")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    traces = []
    plan = [(case, repeat) for case in cases for repeat in range(1, repeats + 1)]
    pending = iter(cases)
    order = {(case["id"], repeat): number for number, (case, repeat) in enumerate(plan)}

    async def worker(isolated):
        # 取下一项到开始运行之间没有 await；工作者不会重复领取同一个案例。
        for case in pending:
            continuity = {}
            for repeat in range(1, repeats + 1):
                # 同一客户先完成画像和资源收尾，再开始下一通；整个序列占一个并发名额。
                await _execute_case(settings, case, output, repeat, traces, isolated, continuity)

    try:
        with (
            tempfile.TemporaryDirectory(prefix="growth-dialogue-run-") as directory,
            temporary_database(settings.app_db, prefix="dialogue_eval") as database,
        ):
            root = Path(directory)
            isolated = replace(
                settings, runtime_dir=root, app_db=database,
                session_secret=secrets.token_urlsafe(24),
            )
            # TaskGroup 在中断或异常时取消并等待全部工作者，之后才删除本次评测数据库。
            async with asyncio.TaskGroup() as group:
                for number in range(min(concurrency, len(cases))):
                    group.create_task(worker(isolated), name=f"dialogue-evaluation-{number + 1}")
    finally:
        # 中断也保留原计划分母和未判定状态，已执行的付费调用不因缺少最终摘要而丢失。
        traces.sort(key=lambda trace: order[(trace["case_id"], trace["repeat"])])
        dump(output / "traces.json", traces)
        summary = summarize(traces, cases, repeats=repeats)
        summary.update(concurrency=concurrency, memory_mode="fresh_database_per_run",
                       repeat_semantics="sequential_customer_calls")
        dump(output / "summary.json", summary)
    return summary


def _run_interruptible(awaitable):
    """把连续 Ctrl-C 合并为一次协作式取消，清理结束后恢复原有信号处理器。"""
    if threading.current_thread() is not threading.main_thread():
        return asyncio.run(awaitable)
    task = None
    interrupted = False
    previous = signal.getsignal(signal.SIGINT)

    def interrupt(_number, _frame):
        nonlocal interrupted
        if interrupted:
            return
        interrupted = True
        if task is not None:
            task.get_loop().call_soon_threadsafe(task.cancel)

    async def main():
        nonlocal task
        task = asyncio.current_task()
        if interrupted:
            task.cancel()
        return await awaitable

    signal.signal(signal.SIGINT, interrupt)
    try:
        return asyncio.run(main())
    except asyncio.CancelledError:
        if interrupted:
            raise KeyboardInterrupt from None
        raise
    finally:
        signal.signal(signal.SIGINT, previous)


def run(settings, output, cases, *, execute=False, repeats=2, concurrency=16):
    """默认仅返回计划摘要；显式 execute 才创建模型客户端并运行对话。"""
    _validate_concurrency(concurrency)
    if execute:
        return _run_interruptible(globals()["execute"](
            settings, cases, Path(output), repeats=repeats, concurrency=concurrency,
        ))
    summary = summarize([], cases, repeats=repeats)
    summary.update(status="preview", quality_evaluated=False, model_requests=0,
                   concurrency=concurrency, memory_mode="fresh_database_per_run",
                   repeat_semantics="sequential_customer_calls")
    return summary
