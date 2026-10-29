"""复用已冻结的对话，只补跑缺失或无效的裁判结果。"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import shutil
from copy import deepcopy
from datetime import UTC, datetime

from backend.llm import judge_generation
from backend.paths import portable_data, portable_path, redact_local_paths
from backend.settings import model_connection
from evaluation.common.io import digest, dump, load
from evaluation.common.paths import create_run_dir, resolve_run_dir, validate_run_label
from evaluation.common.report import write_report
from evaluation.dialogue.metrics import LATENCY_VERSION, summarize
from evaluation.dialogue.runner import _run_interruptible
from evaluation.runner import _invalidate_summary, _source_hashes

from .review import JUDGE_PROMPT, review_trace, validate_review
from .schema import JUDGE_RESPONSE_DIMENSIONS, JUDGE_VERSION

MAX_SOURCE_FILE_BYTES = 100 * 1024 ** 2


def _read(path):
    return load(path)


def _hash_files(source, project_root):
    """只冻结直接来源的产物，前次补跑的 source 通过清单哈希追溯，避免递归复制。"""
    hashes = {}
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if relative.parts[0] == "source":
            continue
        if path.is_symlink():
            raise ValueError("源评测目录不能包含符号链接")
        if path.is_file():
            if path.stat().st_size > MAX_SOURCE_FILE_BYTES:
                raise ValueError(
                    "源评测包含超过 GitHub 单文件限制的产物；请先无损压缩来源，再执行裁判补跑。"
                )
            raw = path.read_bytes()
            # 检查解压后的内容，但冻结哈希与复制仍保留原始压缩字节。
            content = gzip.decompress(raw) if path.suffix == ".gz" else raw
            text = content.decode("utf-8", errors="replace")
            payload = text
            if path.suffix == ".json" or path.name.endswith(".json.gz"):
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    pass
            # 来源必须逐字冻结，不能一边脱敏一边继续沿用原有哈希和评分证据。
            # JSON 还要检查解码后的内容，防止转义字符隐藏本机路径。
            if (redact_local_paths(text, project_root) != text
                    or portable_data(payload, project_root) != payload):
                raise ValueError(
                    "源评测产物包含本机绝对路径，不能原样复制；请重新生成可分享的评测记录。"
                )
            hashes[str(relative)] = hashlib.sha256(raw).hexdigest()
    return hashes


def _valid_review(trace):
    """合法的低分和明确无法判定都保留，不能用补跑反复抽取更高分。"""
    review = trace.get("review")
    if not isinstance(review, dict):
        return False
    if set(review) != set(JUDGE_RESPONSE_DIMENSIONS.values()):
        return False
    try:
        validate_review({key: review[value] for key, value in JUDGE_RESPONSE_DIMENSIONS.items()}, trace)
    except (TypeError, ValueError, KeyError):
        return False
    return True


def _load_source(settings, source):
    """先校验已保存的计划、口径和证据，预览与执行使用完全相同的选择条件。"""
    if source.is_symlink() or not source.is_dir():
        raise ValueError("源评测目录不存在或是符号链接")
    hashes = _hash_files(source, settings.project_root)
    manifest, cases, traces = (_read(source / name) for name in (
        "manifest.json", "cases.json", "traces.json",
    ))
    if manifest.get("suite") != "dialogue" or manifest.get("status") == "preview":
        raise ValueError("仅支持已有真实对话记录的 dialogue 运行")
    if not isinstance(cases, list) or not isinstance(traces, list):
        raise TypeError("源案例和轨迹必须是列表")
    if manifest.get("selected_cases_sha256") != digest(cases):
        raise ValueError("源案例与冻结题集哈希不一致")
    repeats = manifest.get("repeats")
    summarize(traces, cases, repeats=repeats)
    if (manifest.get("case_ids") != [case["id"] for case in cases]
            or manifest.get("expected_cases") != len(cases)
            or manifest.get("expected_trials") != len(cases) * repeats):
        raise ValueError("源清单与冻结案例计划不一致")
    scoring = manifest.get("scoring", {})
    prompt_hash = hashlib.sha256(JUDGE_PROMPT.encode()).hexdigest()
    if (scoring.get("version") != JUDGE_VERSION
            or scoring.get("prompt_sha256") != prompt_hash
            or scoring.get("prompt_snapshot") != "judge_prompt.md"
            or hashes.get("judge_prompt.md") != prompt_hash):
        raise ValueError("源运行与当前评分口径不同，不能混合局部补跑结果")
    if manifest.get("latency_version") != LATENCY_VERSION:
        raise ValueError("源运行不属于当前首 token 时延口径，请重新运行完整对话评测")
    connection = model_connection(settings, "judge")
    if (manifest.get("models", {}).get("judge") != connection.model
            or manifest.get("generation", {}).get("judge") != judge_generation(settings)):
        raise ValueError("源运行与当前裁判模型或生成参数不同，不能混合局部补跑结果")
    cases_by_id = {case["id"]: case for case in cases}
    for trace in traces:
        if trace.get("judge_version") != JUDGE_VERSION:
            raise ValueError("源轨迹包含不同或缺失的评分版本")
        if trace.get("scenario_id", cases_by_id[trace["case_id"]]["scenario_id"]) != (
            cases_by_id[trace["case_id"]]["scenario_id"]
        ):
            raise ValueError("源轨迹与冻结案例的场景不一致")
        if (_valid_review(trace) and trace.get("status") == "completed"
                and trace.get("judge_error_category")):
            raise ValueError("源轨迹同时标记有效评分和裁判失败，请先核查原始记录")
    return manifest, cases, traces, hashes


def _selection(traces):
    """仅修复裁判缺测；对话自身失败、中断或没有回复的记录仍然缺测。"""
    selected, retained = [], 0
    for trace in traces:
        if trace.get("status") not in {"completed", "judge_error"}:
            continue
        if _valid_review(trace):
            if trace["status"] == "completed":
                retained += 1
            continue
        if any(isinstance(turn.get("reply"), str) and turn["reply"].strip()
               for turn in trace.get("turns", [])):
            selected.append(trace)
    return selected, retained


def _save_trace(output, trace):
    filename = digest([trace["case_id"], trace["repeat"]])[:20] + ".json"
    dump(output / "traces" / filename, trace)
    return filename


async def _execute(settings, output, cases, traces, selected, concurrency):
    """裁判请求并发执行，每条完成或取消后立即保存，不创建对话或业务数据库。"""
    cases_by_id = {case["id"]: case for case in cases}
    pending = iter(selected)

    async def worker():
        for trace in pending:
            diagnostics = {
                "case_id": trace["case_id"], "repeat": trace["repeat"],
                "judge_version": JUDGE_VERSION,
            }
            trace.pop("review", None)
            trace["judge_retry_status"] = "requesting"
            try:
                trace["review"] = await review_trace(
                    settings, trace, cases_by_id[trace["case_id"]], diagnostics=diagnostics,
                )
                trace.pop("judge_error_category", None)
                trace.update(status="completed", judge_retry_status="completed")
            except asyncio.CancelledError:
                trace["judge_retry_status"] = "interrupted"
                diagnostics.update(status="interrupted", error_category="CancelledError")
                raise
            except Exception as error:  # noqa: BLE001 - 不写可能包含凭据的供应商异常正文。
                trace.update(status="judge_error", judge_retry_status="failed",
                             judge_error_category=type(error).__name__)
                diagnostics.update(status="error")
                diagnostics.setdefault("error_category", type(error).__name__)
            finally:
                trace["judge_usage"] = diagnostics.get("usage")
                filename = _save_trace(output, trace)
                dump(output / "judge" / filename, diagnostics)

    try:
        workers = []
        async with asyncio.TaskGroup() as group:
            for number in range(min(concurrency, len(selected))):
                workers.append(group.create_task(worker(), name=f"judge-retry-{number + 1}"))
        if any(task.cancelled() for task in workers):
            raise asyncio.CancelledError
    finally:
        dump(output / "traces.json", traces)


def run_retry(settings, source_run, *, run_id=None, execute=False, concurrency=16):
    """生成新的完整报告，只替换失败评分；默认预览，不覆盖源运行或合法评分。"""
    validate_run_label(run_id)
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("concurrency 必须是正整数")
    source = resolve_run_dir(settings.project_root, source_run, category="agent")
    if source is None:
        raise ValueError("源评测目录不存在")
    original, cases, traces, hashes = _load_source(settings, source)
    if execute and not model_connection(settings, "judge").api_key:
        raise ValueError("执行裁判补跑需要 JUDGE_API_KEY 或沿用 AGENT_API_KEY")
    selected, retained = _selection(traces)
    for trace in selected:
        trace.pop("judge_retry_status", None)
    manifest = deepcopy(original)
    manifest.update(
        created_at=datetime.now(UTC).isoformat(), status="preview", operation="judge_retry",
        retry_source={
            "run": source_run, "status": original.get("status"), "files_sha256": hashes,
            "dataset_sha256": original.get("dataset_sha256"),
            "selected_cases_sha256": original["selected_cases_sha256"],
            "report_path": "source/report.md" if "report.md" in hashes else "source/manifest.json",
        },
        judge_retry={
            "source_run": source_run, "selected_trials": len(selected),
            "retained_valid_trials": retained,
            "unavailable_trials": original["expected_trials"] - retained - len(selected),
            "selected": [{"case_id": item["case_id"], "repeat": item["repeat"]}
                         for item in selected],
            "concurrency": concurrency, "executed": execute,
            "completed_trials": 0, "failed_trials": 0,
            "current_judge": {
                "model": model_connection(settings, "judge").model,
                "generation": judge_generation(settings), "version": JUDGE_VERSION,
                "prompt_sha256": original["scoring"]["prompt_sha256"],
            },
            "source_hashes": _source_hashes(settings.project_root),
        },
    )
    # 补跑只替换裁判结果，目录名仍使用原始对话的 Agent 模型。
    output = create_run_dir(
        settings.project_root, "dialogue", model=original["models"]["agent"],
        execute=execute, run_id=run_id, created_at=manifest["created_at"],
    )
    manifest["run_id"] = output.name
    manifest.pop("run_label", None)
    if run_id is not None:
        manifest["run_label"] = run_id
    for relative, expected_hash in hashes.items():
        destination = output / "source" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, destination)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != expected_hash:
            raise ValueError("冻结来源时原始产物发生变化，请使用新的运行名称重试")
    for name in ("judge", "traces"):
        if (output / "source" / name).is_dir():
            shutil.copytree(output / "source" / name, output / name)
    for trace in traces:
        _save_trace(output, trace)
    (output / "judge_prompt.md").write_text(JUDGE_PROMPT, encoding="utf-8")
    dump(output / "manifest.json", manifest)
    dump(output / "cases.json", cases)
    dump(output / "traces.json", traces)
    error_status = None
    try:
        if execute:
            _run_interruptible(_execute(settings, output, cases, traces, selected, concurrency))
    except (asyncio.CancelledError, KeyboardInterrupt):
        error_status = "interrupted"
        raise
    except Exception:
        error_status = "error"
        raise
    finally:
        retry = manifest["judge_retry"]
        retry["completed_trials"] = sum(
            item.get("judge_retry_status") == "completed" for item in selected
        )
        retry["failed_trials"] = sum(
            item.get("judge_retry_status") == "failed" for item in selected
        )
        retry["sources_unchanged"] = retry["source_hashes"] == _source_hashes(settings.project_root)
        summary = summarize(traces, cases, repeats=original["repeats"])
        summary.update(operation="judge_retry", judge_retry=deepcopy(retry))
        if not execute:
            summary.update(status="preview", quality_evaluated=False, model_requests=0)
        elif error_status:
            summary["status"] = error_status
        reasons = list(original.get("invalidation_reasons", []))
        if original.get("sources_unchanged") is False:
            reasons.append("source_resources_changed")
        if original.get("knowledge_unchanged") is False:
            reasons.append("knowledge_index_changed")
        if original.get("knowledge", {}).get("status") in {"unreadable", "unavailable"}:
            reasons.append("knowledge_index_unverified")
        if original.get("status") == "invalidated" and not reasons:
            reasons.append("source_run_invalidated")
        if execute and not retry["sources_unchanged"]:
            reasons.append("judge_retry_sources_changed")
        if reasons and execute:
            reasons = list(dict.fromkeys(reasons))
            _invalidate_summary(summary, reasons)
            manifest["invalidation_reasons"] = reasons
        manifest["status"] = summary["status"]
        dump(output / "traces.json", traces)
        dump(output / "manifest.json", manifest)
        dump(output / "summary.json", summary)
        write_report(output, "dialogue", summary, manifest)
    print(json.dumps(portable_data({
        "path": portable_path(output, settings.project_root), "status": summary["status"],
        "operation": "judge_retry", "source_run": source_run,
        **{name: retry[name] for name in (
            "selected_trials", "retained_valid_trials", "unavailable_trials",
            "completed_trials", "failed_trials",
        )},
    }), ensure_ascii=False, indent=2))
    return 0 if not execute or summary["status"] == "completed" else 1
