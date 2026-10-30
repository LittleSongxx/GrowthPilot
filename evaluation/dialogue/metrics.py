"""汇总四项对话质量评分、主对话用量和首 token 时延，缺测始终单独保留。"""

from __future__ import annotations

from collections import Counter
from math import isfinite

from evaluation.judges.schema import JUDGE_VERSION, QUALITY_DIMENSIONS

LATENCY_VERSION = "agent-first-token-v1"
METRIC_NAMES = (*QUALITY_DIMENSIONS, "average_agent_tokens",
                "agent_first_token_latency_p50_seconds", "agent_first_token_latency_p95_seconds")
PROFILE_STATES = (
    "completed", "skipped", "failed", "timeout", "error", "interrupted", "unavailable",
    "collecting", "pending", "running", "retrying", "cancelled", "missing", "unknown",
)
PROFILE_REASONS = {
    "failed": "画像提取或写入已失败",
    "timeout": "等待画像处理结束超时，未确认最终结果",
    "error": "读取或收尾画像处理时发生异常",
    "interrupted": "画像处理观测被中断",
    "unavailable": "未找到画像处理任务",
    "collecting": "通话信息仍在收集，画像尚未开始提取",
    "pending": "画像任务仍在等待执行",
    "running": "画像任务仍在执行",
    "retrying": "画像任务仍在等待重试",
    "cancelled": "画像任务已取消",
    "missing": "未采集画像处理状态，无法判断是否成功",
    "unknown": "画像处理状态无法识别，无法判断是否成功",
}
PROFILE_ERROR_CATEGORIES = {
    "TimeoutError", "CancelledError", "ValidationError", "JSONDecodeError", "ValueError",
    "TypeError", "RuntimeError", "APIConnectionError", "APITimeoutError", "APIStatusError",
    "RateLimitError", "AuthenticationError", "PermissionDeniedError", "BadRequestError",
    "NotFoundError", "ConflictError", "UnprocessableEntityError", "InternalServerError",
    "DatabaseError", "OperationalError", "InterfaceError", "IntegrityError", "ProgrammingError",
    "worker_interrupted",
}


def agent_tokens(trace):
    """汇总主对话全部 usage 事件，任何缺失用量都保留为未知。"""
    total = 0
    for turn in trace.get("turns", []):
        usage_events = turn.get("usage_events")
        if not isinstance(usage_events, list):
            return None
        model_starts = sum(
            event.get("type") == "framework"
            and event.get("data", {}).get("type") == "MODEL_CALL_START"
            and event.get("data", {}).get("source") != "end_call"
            for event in turn.get("events", [])
        )
        if model_starts > len(usage_events) or (turn.get("error_category") and not usage_events):
            return None
        for event in usage_events:
            data = event.get("data", event)
            values = [data.get("input_tokens"), data.get("output_tokens")]
            if any(type(value) is not int or value < 0 for value in values):
                return None
            total += sum(values)
    return total


def _quality_result(trace, dimension):
    """故障、中断及旧版二元评分不能作为正常通话的等级分。"""
    if (trace is None or trace.get("status") != "completed"
            or trace.get("judge_version") != JUDGE_VERSION):
        return "unassessable", {}
    review = trace.get("review")
    item = review.get(dimension) if isinstance(review, dict) else None
    if not isinstance(item, dict):
        return "unassessable", {}
    score = item.get("score")
    if type(score) is int and 1 <= score <= 5:
        return "assessed", item
    if (score is None and item.get("null_reason") == "not_applicable"
            and dimension in {"problem_resolution", "factual_reliability"}):
        return "not_applicable", item
    return "unassessable", item


def _quality_summary(by_key, expected, dimension):
    counts, scores, low_scores = Counter(), [], []
    for key in sorted(expected):
        status, item = _quality_result(by_key.get(key), dimension)
        counts[status] += 1
        if status == "assessed":
            score = item["score"]
            scores.append(score)
            if score <= 2:
                low_scores.append({
                    "case_id": key[0], "repeat": key[1], "score": score,
                    "reason": item.get("reason", ""),
                })
    observed = sum(scores) / len(scores) if scores else None
    applicable = len(expected) - counts["not_applicable"]
    return {
        "mean": None if counts["unassessable"] else observed,
        "observed_mean": observed,
        "score_distribution": {str(score): scores.count(score) for score in range(1, 6)},
        "assessed_runs": counts["assessed"],
        "not_applicable_runs": counts["not_applicable"],
        "unassessable_runs": counts["unassessable"],
        "expected_runs": len(expected), "applicable_runs": applicable,
        "coverage": counts["assessed"] / applicable if applicable else None,
        "low_score_cases": low_scores,
    }


def _percentile(values, probability):
    """按排序位置线性插值；单条观测的各分位数都等于自身。"""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _latency_summary(by_key, expected):
    """只计客户发言后成功回复的首 token 时延；失败和缺测单列。"""
    counts, values = Counter(), []
    for key in expected:
        trace = by_key.get(key)
        if trace is None:
            counts["unobserved_runs"] += 1
            continue
        for turn in trace.get("turns", []):
            if turn.get("origin") == "host_control":
                counts["excluded_opening_turns"] += 1
                continue
            if turn.get("origin") != "customer":
                # 旧记录缺少来源时无法可靠区分开场，不能默认为客户请求。
                counts["unknown_reply_turns"] += 1
                continue
            reply = turn.get("reply")
            if turn.get("error_category") or not isinstance(reply, str) or not reply.strip():
                counts["failed_reply_turns"] += 1
                continue
            counts["eligible_reply_turns"] += 1
            first_token = turn.get("first_token_seconds")
            if (type(first_token) not in {int, float}
                    or not isfinite(first_token) or first_token < 0):
                counts["unknown_reply_turns"] += 1
                continue
            elapsed = turn.get("elapsed_seconds")
            # 完整耗时只用于一致性校验，不能代替首 token 时延或使独立观测失效。
            if (type(elapsed) in {int, float} and isfinite(elapsed)
                    and elapsed >= 0 and first_token > elapsed):
                counts["unknown_reply_turns"] += 1
                continue
            values.append(first_token)
    counts["measured_reply_turns"] = len(values)
    eligible = counts["eligible_reply_turns"]
    return {
        **{name: counts[name] for name in (
            "eligible_reply_turns", "measured_reply_turns", "unknown_reply_turns",
            "failed_reply_turns", "excluded_opening_turns", "unobserved_runs",
        )},
        "coverage": len(values) / eligible if eligible else None,
        "observed_p50_seconds": _percentile(values, 0.5),
        "observed_p95_seconds": _percentile(values, 0.95),
    }


def _profile_summary(by_key, expected):
    """单列通话后画像的完整性，未知不冒充失败，也不改变已有对话质量分。"""
    counts, issues, affected = Counter(), [], set()
    for key in sorted(expected):
        trace = by_key.get(key)
        state = trace.get("profile_processing") if trace is not None else None
        if not isinstance(state, dict) or "status" not in state:
            status = "missing"
            state = {}
        else:
            status = state["status"]
            if not isinstance(status, str) or status not in PROFILE_STATES:
                status = "unknown"
        counts[status] += 1
        if status in {"completed", "skipped"}:
            continue
        # 同一客户后续通话可能缺少本通新画像；这是风险提示，不推断实际记忆错误。
        subsequent = sorted(repeat for case_id, repeat in expected if case_id == key[0] and repeat > key[1])
        affected.update((key[0], repeat) for repeat in subsequent)
        issue = {
            "case_id": key[0], "repeat": key[1], "status": status,
            "reason": PROFILE_REASONS[status], "trace_recorded": trace is not None,
            "subsequent_repeats": subsequent,
        }
        last_state = state.get("last_state")
        details = last_state if isinstance(last_state, dict) else state
        attempts = details.get("attempts")
        if type(attempts) is int and attempts >= 0:
            issue["attempts"] = attempts
        category = details.get("error_category") or details.get("error")
        if category:
            # 只发布已知异常类别，不复制供应商响应、请求信息或任意错误正文。
            issue["error_category"] = (
                category if isinstance(category, str) and category in PROFILE_ERROR_CATEGORIES
                else "unclassified_error"
            )
        if status == "timeout" and details.get("status") in PROFILE_STATES:
            issue["last_status"] = details["status"]
        issues.append(issue)
    unknown = counts["missing"] + counts["unknown"]
    successful = counts["completed"] + counts["skipped"]
    incomplete = len(expected) - successful - unknown
    return {
        "status": "incomplete" if incomplete else "unknown" if unknown else "completed",
        "expected_runs": len(expected),
        "state_counts": {status: counts[status] for status in PROFILE_STATES},
        "successful_runs": successful, "incomplete_runs": incomplete, "unknown_runs": unknown,
        "coverage": (len(expected) - unknown) / len(expected) if expected else None,
        "potentially_affected_subsequent_runs": len(affected),
        "issues": issues,
    }


def _summarize(traces, expected):
    by_key = {(trace["case_id"], trace["repeat"]): trace for trace in traces}
    errors = Counter()
    token_totals = []
    missing_tokens = 0
    for key in expected:
        trace = by_key.get(key)
        if trace is None:
            errors["missing_run"] += 1
            missing_tokens += 1
            continue
        if trace.get("status") != "completed":
            errors[trace.get("status", "missing_status")] += 1
        if trace.get("judge_error_category") and trace.get("status") != "judge_error":
            errors["judge_error"] += 1
        tokens = agent_tokens(trace)
        if tokens is None:
            missing_tokens += 1
        else:
            token_totals.append(tokens)
    quality = {dimension: _quality_summary(by_key, expected, dimension)
               for dimension in QUALITY_DIMENSIONS}
    latency = _latency_summary(by_key, expected)
    quality_complete = not any(item["unassessable_runs"] for item in quality.values())
    latency_complete = not (latency["unknown_reply_turns"] or latency["unobserved_runs"])
    tokens_complete = not missing_tokens
    completed = quality_complete and latency_complete and tokens_complete
    metrics = dict.fromkeys(METRIC_NAMES)
    # 每项指标独立判断缺测；已经取得的分布和局部观测另行保留，不冒充完整结果。
    metrics.update({dimension: item["mean"] for dimension, item in quality.items()})
    if tokens_complete and expected:
        metrics["average_agent_tokens"] = sum(token_totals) / len(expected)
    if latency_complete:
        metrics["agent_first_token_latency_p50_seconds"] = latency["observed_p50_seconds"]
        metrics["agent_first_token_latency_p95_seconds"] = latency["observed_p95_seconds"]
    return {
        "status": "completed" if completed else "incomplete",
        "latency_version": LATENCY_VERSION,
        "metrics": metrics, "quality": quality, "latency": latency,
        "profile_processing": _profile_summary(by_key, expected),
        "counts": {
            "expected_runs": len(expected), "recorded_runs": len(traces),
            "completed_runs": sum(trace.get("status") == "completed" for trace in traces),
            "agent_token_known_runs": len(token_totals), "agent_token_unknown_runs": missing_tokens,
        },
        "errors": dict(sorted(errors.items())),
        "known_agent_tokens": sum(token_totals),
    }


def summarize(traces, cases, *, repeats=2):
    """以运行前冻结的案例及重复次数作分母，不把故障样本悄悄排除。"""
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats 必须是正整数")
    ids = [case["id"] for case in cases]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("评测案例必须非空且 ID 唯一")
    expected = {(case_id, repeat) for case_id in ids for repeat in range(1, repeats + 1)}
    seen = set()
    for trace in traces:
        key = (trace["case_id"], trace["repeat"])
        if key not in expected or key in seen:
            raise ValueError("追踪记录包含重复运行或计划外案例")
        seen.add(key)
    summary = _summarize(traces, expected)
    summary.update(
        suite="dialogue", repeats=repeats, expected_cases=len(cases),
        judge_version=JUDGE_VERSION,
        external_conversion="not_measured",
        token_scope="仅主对话模型调用；不含客户、裁判和通话后画像提取",
        latency_scope=(
            "客户文本提交给 Agent 至最终面向客户回复的首个非空文本块；"
            "包含此前工具和多轮模型调用耗时，内部思考、工具参数、工具结果、"
            "被丢弃的中间文本均不作为首 token；结束工具的告别文本按可用时间计；"
            "排除主动开场、模拟客户、裁判和通话后画像等待"
        ),
        latency_percentile_method="排序位置 (n-1)×p 线性插值，仅统计成功交付回复",
        by_scenario={}, by_goal_type={},
    )
    for scenario_id in sorted({case["scenario_id"] for case in cases}):
        scenario_ids = {case["id"] for case in cases if case["scenario_id"] == scenario_id}
        selected = [trace for trace in traces if trace["case_id"] in scenario_ids]
        summary["by_scenario"][scenario_id] = _summarize(
            selected, {key for key in expected if key[0] in scenario_ids},
        )
    for goal_type in sorted({case.get("goal", {}).get("type", "unspecified") for case in cases}):
        goal_ids = {
            case["id"] for case in cases if case.get("goal", {}).get("type", "unspecified") == goal_type
        }
        selected = [trace for trace in traces if trace["case_id"] in goal_ids]
        summary["by_goal_type"][goal_type] = _summarize(
            selected, {key for key in expected if key[0] in goal_ids},
        )
    return summary
