"""使用独立模型上下文评价整通对话，保存输入、修复和重试记录供人工核查。"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from json_repair import repair_json

from backend.llm import completion_parameters, judge_generation
from backend.settings import model_connection

from .evidence import judge_payload
from .schema import JUDGE_RESPONSE_DIMENSIONS, JUDGE_VERSION, NOT_APPLICABLE_DIMENSIONS

JUDGE_PROMPT_PATH = Path(__file__).with_name("prompt.md")
JUDGE_PROMPT = JUDGE_PROMPT_PATH.read_text(encoding="utf-8")
JUDGE_MAX_ATTEMPTS = 3
JUDGE_RETRY_DELAY_SECONDS = 1


def validate_review(review, trace):
    """提取必需字段并严格校验评分，不因多余字段丢弃有效评审。"""
    if not any(isinstance(turn.get("reply"), str) and turn["reply"].strip()
               for turn in trace.get("turns", [])):
        raise ValueError("轨迹没有实际回复，不能进行质量评审")
    if not isinstance(review, dict) or not set(JUDGE_RESPONSE_DIMENSIONS) <= set(review):
        raise ValueError("评审必须包含四个质量维度")
    result = {}
    for response_key, dimension in JUDGE_RESPONSE_DIMENSIONS.items():
        row = review[response_key]
        if not isinstance(row, dict):
            raise TypeError("每个质量维度必须是对象")
        if not {"score", "reason"} <= set(row):
            raise ValueError("评审项缺少评分或理由")
        keys = ["score", "reason"]
        if row["score"] is None:
            keys.append("null_reason")
            if row.get("null_reason") not in {"not_applicable", "unassessable"}:
                raise ValueError("空评分必须区分不适用与无法评审")
            if (row["null_reason"] == "not_applicable"
                    and dimension not in NOT_APPLICABLE_DIMENSIONS):
                raise ValueError("仅问题解决度和事实可靠性允许不适用")
        elif type(row["score"]) is not int or not 1 <= row["score"] <= 5:
            raise ValueError("评分必须是 1～5 的整数或 null")
        if not isinstance(row["reason"], str) or not row["reason"].strip():
            raise ValueError("评审必须说明理由")
        result[dimension] = {key: row[key] for key in keys}
    return result


def _check_original_scores(content):
    """修复只能补 JSON 语法，不能把评分表达式截断成看似合法的整数。"""
    # 引号中的理由作为完整 token 保留，避免把理由里提到的 score 误当成字段。
    pattern = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{}\[\],:]|[^\s{}\[\],:"\']+'
    tokens = re.findall(pattern + r"|[\"']", content)
    for index, token in enumerate(tokens[:-1]):
        if token not in {'"score"', "'score'", "score"} or tokens[index + 1] != ":":
            continue
        tail = tokens[index + 2:]
        if not tail or tail[0] not in {"1", "2", "3", "4", "5", "null", "None"}:
            raise ValueError("原始评分必须明确为 1～5 的整数或 null，不能通过修复补造或转换")
        # 下一字段前遗漏逗号可以修复，分数后附带另一数值或表达式则不能接收。
        if (len(tail) > 1 and tail[1] not in {",", "}"}
                and (len(tail) < 3 or tail[2] != ":")):
            raise ValueError("原始评分包含多余表达式，不能截断后评分")


def _parse_review(content, diagnostic):
    """先保留标准 JSON 的原值，仅在语法错误时修复，并留下修复结果。"""
    diagnostic["parsing_method"] = "json"
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        diagnostic["parsing_method"] = "json_repair"
        # 不使用 schema 修复，避免依赖替评审补默认分数、理由或转换分数类型。
        parsed, repair_log = repair_json(
            content, return_objects=True, skip_json_loads=True, logging=True,
        )
        diagnostic.update(repaired_output=parsed, repair_log=repair_log)
    ignored = []
    if isinstance(parsed, dict):
        ignored.extend(key for key in parsed if key not in JUDGE_RESPONSE_DIMENSIONS)
        for key in JUDGE_RESPONSE_DIMENSIONS:
            row = parsed.get(key)
            if isinstance(row, dict):
                required = {"score", "reason"}
                if row.get("score") is None:
                    required.add("null_reason")
                ignored.extend(f"{key}.{field}" for field in row if field not in required)
    diagnostic["ignored_fields"] = ignored
    return parsed


def _record_error(diagnostic, stage, error):
    """供应商异常可能包含密钥或请求信息，仅保留类别与本地校验消息。"""
    diagnostic.update(status="error", error_stage=stage, error_category=type(error).__name__)
    if stage in {"input", "completion", "validation"} and type(error) in {ValueError, TypeError}:
        diagnostic["error_message"] = str(error)
    elif stage == "json":
        diagnostic["error_message"] = "评审输出无法解析为 JSON"
    elif stage == "response":
        diagnostic["error_message"] = "评审响应缺少必要字段"


def _can_retry(stage, error):
    """只重试生成格式错误或临时服务故障，不重试鉴权和参数错误。"""
    from openai import APIConnectionError, APIStatusError

    if stage in {"completion", "json", "validation"}:
        return isinstance(error, (ValueError, TypeError))
    if stage == "response":
        return isinstance(error, (IndexError, AttributeError, TypeError))
    if stage == "request":
        if isinstance(error, (TimeoutError, ConnectionError, APIConnectionError)):
            return True
        if isinstance(error, APIStatusError):
            return error.status_code in {408, 409, 429} or error.status_code >= 500
    return False


def _add_usage(total, usage):
    """累加供应商返回的数值用量，保留输入、输出及推理 token 的层级。"""
    for key, value in usage.items():
        if isinstance(value, dict):
            child = total.setdefault(key, {})
            if isinstance(child, dict):
                _add_usage(child, value)
        elif type(value) in {int, float}:
            previous = total.get(key, 0)
            if type(previous) in {int, float}:
                total[key] = previous + value


def _publish_attempt(diagnostic, attempt):
    """旧读取方继续使用顶层最新结果，用量则包含所有已经发生的尝试。"""
    for key in ("content", "finish_reason", "reasoning_content", "parsing_method",
                "repaired_output", "repair_log", "ignored_fields", "error_stage",
                "error_category", "error_message", "retryable"):
        diagnostic.pop(key, None)
    diagnostic.update({key: value for key, value in attempt.items()
                       if key not in {"attempt", "usage"}})
    diagnostic["attempt_count"] = len(diagnostic["attempts"])
    diagnostic["retry_count"] = max(0, diagnostic["attempt_count"] - 1)
    total = {}
    for recorded in diagnostic["attempts"]:
        if isinstance(recorded.get("usage"), dict):
            _add_usage(total, recorded["usage"])
    diagnostic["usage"] = total or None
    diagnostic["usage_complete"] = all(
        isinstance(recorded.get("usage"), dict) for recorded in diagnostic["attempts"]
    )


async def review_trace(settings, trace, case, diagnostics=None):
    """独立评审最多请求三次；有效结果立即返回，失败与原始输出全部留档。"""
    from openai import AsyncOpenAI

    connection = model_connection(settings, "judge")
    diagnostic = diagnostics if diagnostics is not None else {}
    diagnostic.update(
        model=connection.model, **judge_generation(settings),
        judge_version=JUDGE_VERSION, status="preparing", attempts=[],
        max_attempts=JUDGE_MAX_ATTEMPTS, attempt_count=0, retry_count=0,
        usage=None, usage_complete=False,
    )
    stage, client = "input", None
    try:
        payload = judge_payload(trace, case)
        diagnostic["input"] = payload
        stage = "request"
        client = AsyncOpenAI(
            api_key=connection.api_key, base_url=connection.base_url,
            timeout=180, max_retries=0,
        )
        request = {
            "model": connection.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": JUDGE_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            **completion_parameters(settings, role="judge"),
        }
        for number in range(1, JUDGE_MAX_ATTEMPTS + 1):
            attempt = {"attempt": number, "status": "requesting", "usage": None}
            diagnostic["attempts"].append(attempt)
            stage = "request"
            _publish_attempt(diagnostic, attempt)
            try:
                # 每次仍以同一原始材料独立评分，不把失败答案或既有低分喂回裁判。
                response = await client.chat.completions.create(**request)
                stage = "response"
                usage = getattr(response, "usage", None)
                attempt["usage"] = usage.model_dump() if usage is not None else None
                choice = response.choices[0]
                attempt.update(
                    content=choice.message.content, finish_reason=choice.finish_reason,
                    reasoning_content=getattr(choice.message, "reasoning_content", None),
                    status="response_received",
                )
                stage = "completion"
                if choice.finish_reason != "stop":
                    raise ValueError("评审输出未完整结束")
                if not isinstance(choice.message.content, str) or not choice.message.content.strip():
                    raise ValueError("评审输出为空")
                stage = "json"
                parsed = _parse_review(choice.message.content, attempt)
                stage = "validation"
                if attempt["parsing_method"] == "json_repair":
                    _check_original_scores(choice.message.content)
                result = validate_review(parsed, trace)
                attempt["status"] = "valid"
            except asyncio.CancelledError:
                attempt.update(status="interrupted", error_category="CancelledError")
                _publish_attempt(diagnostic, attempt)
                raise
            except Exception as error:
                _record_error(attempt, stage, error)
                attempt["retryable"] = _can_retry(stage, error)
                _publish_attempt(diagnostic, attempt)
                if not attempt["retryable"] or number == JUDGE_MAX_ATTEMPTS:
                    raise
                stage = "retry_wait"
                await asyncio.sleep(JUDGE_RETRY_DELAY_SECONDS * number)
            else:
                _publish_attempt(diagnostic, attempt)
                return result
    except asyncio.CancelledError:
        diagnostic.update(status="interrupted", error_stage=stage, error_category="CancelledError")
        raise
    except Exception as error:
        if not diagnostic["attempts"]:
            _record_error(diagnostic, stage, error)
        raise
    finally:
        if client is not None:
            await client.close()
