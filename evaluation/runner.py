"""统一入口：选择题集、冻结运行输入，再执行 RAG 或动态文本对话。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from backend.llm import completion_parameters, judge_generation
from backend.paths import portable_data, portable_path
from backend.settings import MODEL_ROLES, model_connection

from .common.datasets import load_dialogue_datasets, load_rag_datasets, select_cases
from .common.io import digest, dump
from .common.knowledge import corpus_versions, knowledge_snapshot
from .common.paths import create_run_dir, validate_run_label
from .common.report import write_report


def _source_hashes(root):
    """记录实际源码和业务资源，不读取环境文件、凭据或历史运行数据。"""
    hashes = {}
    for folder in ("backend", "evaluation"):
        for path in sorted((root / folder).rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".sql", ".yaml", ".yml", ".md", ".json"}:
                continue
            relative = path.relative_to(root)
            if any(
                part in {"__pycache__", "reports", "datasets", ".run", "runtime"}
                for part in relative.parts
            ):
                continue
            if path.suffix == ".json" and not (
                relative.parts[:2] == ("backend", "scenarios")
                or relative.parts[:2] == ("evaluation", "data")
                or relative.parts[:3] == ("evaluation", "checks", "data")
            ):
                continue
            hashes[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def make_manifest(settings, datasets, cases, suite, repeats=2, concurrency=16):
    """记录题目、模型与实际代码版本，密钥不参与序列化。"""
    from backend.agent_harness import load_scenario
    from backend.agent_harness.agent import FRAMEWORK_COMMIT
    from backend.agent_harness.prompts import PROMPT_VERSION

    agent_parameters = completion_parameters(settings, role="agent")
    customer_parameters = completion_parameters(settings, role="customer")
    profile_parameters = completion_parameters(settings, role="profile")
    scenarios = sorted({case["scenario_id"] for case in cases})
    manifest = {
        "schema_version": "evaluation-v1",
        "suite": suite,
        "created_at": datetime.now(UTC).isoformat(),
        "evaluation_date": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
        "dataset_sha256": digest(datasets),
        "selected_cases_sha256": digest(cases),
        "dataset_versions": {d["scenario_id"]: d["version"] for d in datasets},
        "corpus_versions": {
            d["scenario_id"]: d.get("corpus_version") for d in datasets if d.get("corpus_version")
        },
        "scenarios": scenarios,
        "scenario_snapshots": {
            sid: load_scenario(
                settings.project_root / f"backend/scenarios/{sid}/scenario.yaml"
            ).snapshot_hash
            for sid in scenarios
        },
        "framework_commit": FRAMEWORK_COMMIT,
        "prompt_version": PROMPT_VERSION,
        "model_config_hash": settings.model_config_hash,
        "models": {
            role: model_connection(settings, role).model for role in MODEL_ROLES
        },
        "generation": {
            "agent": {
                "temperature": agent_parameters.get("temperature"),
                "max_tokens": settings.agent_max_tokens,
                "max_iterations": settings.agent_max_iterations,
            },
            "customer": {
                "temperature": customer_parameters.get("temperature"),
                "max_tokens": settings.customer_max_tokens,
            },
            "profile": {
                "temperature": profile_parameters.get("temperature"),
                "max_tokens": settings.profile_max_tokens,
            },
            "judge": judge_generation(settings),
        },
        "source_hashes": _source_hashes(settings.project_root),
        "expected_cases": len(cases),
        "case_ids": [case["id"] for case in cases],
        "repeats": repeats if suite == "dialogue" else 1,
        "expected_trials": len(cases) * (repeats if suite == "dialogue" else 1),
        "customer_source": "dynamic_openai_compatible" if suite == "dialogue" else None,
        "external_conversion": "not_measured",
        "status": "preview",
    }
    if suite == "dialogue":
        from importlib.metadata import version

        from .dialogue.metrics import LATENCY_VERSION
        from .judges import JUDGE_PROMPT, JUDGE_PROMPT_PATH, JUDGE_VERSION
        from .judges.review import JUDGE_MAX_ATTEMPTS

        manifest["schema_version"] = "evaluation-v2"
        manifest["scoring"] = {
            "version": JUDGE_VERSION,
            "prompt_path": "evaluation/judges/" + JUDGE_PROMPT_PATH.name,
            "prompt_snapshot": "judge_prompt.md",
            "prompt_sha256": hashlib.sha256(JUDGE_PROMPT.encode()).hexdigest(),
        }
        manifest["judge_output_policy"] = {
            "parser": "json_then_json_repair",
            "json_repair_version": version("json-repair"),
            "extra_fields": "ignore_and_record",
            "score_validation": "strict_integer_1_to_5_or_explicit_null",
            "max_attempts": JUDGE_MAX_ATTEMPTS,
        }
        manifest["concurrency"] = concurrency
        manifest["latency_version"] = LATENCY_VERSION
        manifest["memory_mode"] = "fresh_database_per_run"
        manifest["repeat_semantics"] = "sequential_customer_calls"
        manifest["knowledge"] = knowledge_snapshot(
            settings.runtime_dir, scenarios, database=settings.app_db,
        )
        manifest["corpus_versions"] = corpus_versions(manifest["knowledge"])
    return manifest


def _finish_manifest(settings, manifest, execute):
    """记录结束时的资源身份；不混用执行期间发生变化的知识或代码。"""
    reasons = []
    manifest["sources_unchanged"] = manifest["source_hashes"] == _source_hashes(
        settings.project_root
    )
    if execute and not manifest["sources_unchanged"]:
        reasons.append("source_resources_changed")
    if execute and manifest["suite"] == "dialogue":
        before = manifest["knowledge"]
        after = knowledge_snapshot(
            settings.runtime_dir, manifest["scenarios"], database=settings.app_db,
        )
        manifest["knowledge_after"] = after
        manifest["corpus_versions_after"] = corpus_versions(after)
        if {"unreadable", "unavailable"} & {before["status"], after["status"]}:
            manifest["knowledge_unchanged"] = None
            reasons.append("knowledge_index_unverified")
        else:
            manifest["knowledge_unchanged"] = before == after
            if not manifest["knowledge_unchanged"]:
                reasons.append("knowledge_index_changed")
    return reasons


def _invalidate_summary(summary, reasons):
    """保留轨迹和计数用于诊断，变化后的混合版本不发布正式指标。"""
    summary["status"] = "invalidated"
    summary["invalidation_reasons"] = reasons
    metrics = summary.get("metrics")
    summary["metrics"] = dict.fromkeys(metrics) if isinstance(metrics, dict) else None
    for quality in summary.get("quality", {}).values():
        # 分布和原始低分证据用于诊断，正式均分与观察均分都必须撤回。
        quality["mean"] = None
        quality["observed_mean"] = None
    latency = summary.get("latency", {})
    for name in ("observed_p50_seconds", "observed_p95_seconds"):
        if name in latency:
            latency[name] = None
    for grouping in ("by_scenario", "by_goal_type"):
        for group in summary.get(grouping, {}).values():
            _invalidate_summary(group, reasons)


def _offline(suite, output):
    """保留原有工具契约命令，它们不参与模型质量指标。"""
    if suite == "contracts":
        from .checks.contracts import experiments

        with tempfile.TemporaryDirectory(prefix="growth-contracts-") as directory:
            report = asyncio.run(experiments(directory))
        dump(output / "report.json", report)
        summary = {"passed": report["passed"], "scenarios": sorted(report["scenarios"])}
    else:
        from .checks.business_coverage import run

        report = asyncio.run(run(output / "report.json"))
        summary = report["summary"]
    summary = {**summary, "suite": suite, "model_requests": 0, "quality_evaluated": False}
    dump(output / "summary.json", summary)
    print(json.dumps(portable_data({"path": portable_path(output), **summary}), ensure_ascii=False))
    return 0 if summary["passed"] else 1


def run_suite(
    suite,
    settings,
    execute=False,
    run_id=None,
    *,
    scenario=None,
    limit=None,
    repeats=2,
    case_ids=None,
    concurrency=16,
):
    """默认预览；显式执行时固定案例选择，不覆盖已有运行产物。"""
    if suite == "text":
        suite = "dialogue"
    offline = {"contracts", "business-coverage"}
    if suite not in {"rag", "dialogue"} | offline:
        raise ValueError("正式评测仅支持 rag 或 dialogue；独立 skills 评测已取消")
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats 必须是正整数")
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("concurrency 必须是正整数")
    if suite != "dialogue" and concurrency != 16:
        raise ValueError("--concurrency 只用于 dialogue；RAG 与工程检查不接受自定义对话并发数")
    validate_run_label(run_id)
    if suite in offline:
        if execute or scenario or limit or case_ids:
            raise ValueError("工具契约套件始终离线执行，不接受模型执行或题集筛选参数")
        output = create_run_dir(
            settings.project_root, suite, model=suite, execute=False, run_id=run_id,
        )
        return _offline(suite, output)
    if execute and suite == "dialogue" and not model_connection(settings).api_key:
        raise ValueError("真实对话评测需要 AGENT_API_KEY；省略 --execute 可离线预览")
    loader = load_rag_datasets if suite == "rag" else load_dialogue_datasets
    datasets = loader(settings.project_root, scenario)
    if portable_data(datasets, settings.project_root) != datasets:
        raise ValueError(
            "评测题集包含本机绝对路径；请改为相对路径或 /growthpilot/... 后再运行。"
        )
    cases = select_cases(datasets, limit=limit, case_ids=case_ids)
    manifest = make_manifest(settings, datasets, cases, suite, repeats, concurrency)
    if suite == "rag":
        from backend.agent_harness.rag.index import MODEL_NAME

        model = MODEL_NAME
    else:
        model = manifest["models"]["agent"]
    output = create_run_dir(
        settings.project_root, suite, model=model, execute=execute, run_id=run_id,
        created_at=manifest["created_at"],
    )
    manifest["run_id"] = output.name
    if run_id is not None:
        manifest["run_label"] = run_id
    if suite == "dialogue":
        from .judges import JUDGE_PROMPT

        # 冻结裁判实际使用的字符串，不重新读取可能已变更的源码文件。
        (output / manifest["scoring"]["prompt_snapshot"]).write_text(
            JUDGE_PROMPT, encoding="utf-8"
        )
    dump(output / "manifest.json", manifest)
    dump(output / "cases.json", cases)
    try:
        if suite == "rag":
            from .rag.runner import run

            summary = run(settings, output, cases, execute=execute)
        else:
            from .dialogue.runner import run

            summary = run(
                settings, output, cases, execute=execute, repeats=repeats,
                concurrency=concurrency,
            )
    except (asyncio.CancelledError, KeyboardInterrupt) as exc:
        manifest.update(status="interrupted", error_category=type(exc).__name__)
        _finish_manifest(settings, manifest, execute)
        dump(output / "manifest.json", manifest)
        raise
    except Exception as exc:
        manifest.update(status="error", error_category=type(exc).__name__)
        _finish_manifest(settings, manifest, execute)
        dump(output / "manifest.json", manifest)
        raise
    reasons = _finish_manifest(settings, manifest, execute)
    if reasons:
        _invalidate_summary(summary, reasons)
        manifest["invalidation_reasons"] = reasons
    manifest["status"] = summary.get("status", "unknown")
    dump(output / "manifest.json", manifest)
    dump(output / "summary.json", summary)
    write_report(output, suite, summary, manifest)
    print(json.dumps(
        portable_data({"path": portable_path(output, settings.project_root), **summary}),
        ensure_ascii=False, indent=2,
    ))
    if not execute:
        return 0
    return 0 if summary.get("status") == "completed" and manifest["sources_unchanged"] else 1
