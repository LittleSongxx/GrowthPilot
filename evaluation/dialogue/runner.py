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

