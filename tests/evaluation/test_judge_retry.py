"""裁判补跑只修复评分缺测，保留完整证据、原计划和所有对话测量。"""

import asyncio
import gzip
import hashlib
import json
from copy import deepcopy

import pytest

from backend.database import DatabaseConfig
from backend.llm import judge_generation
from backend.settings import Settings
from evaluation.common import io
from evaluation.common.io import digest, dump
from evaluation.dialogue.metrics import LATENCY_VERSION
from evaluation.judges import JUDGE_PROMPT, JUDGE_VERSION, retry
from tests.evaluation.test_dialogue_metrics import case, trace


def load(path):
    return io.load(path)


def source_run(tmp_path, *, traces=None, repeats=2, status="incomplete", legacy=False):
    settings = Settings(
        tmp_path, tmp_path / "runtime", DatabaseConfig("postgresql://unused@localhost/unused"),
        judge_api_key="unused-judge-key",
    )
    source = tmp_path / "evaluation/reports" / ("original" if legacy else "agent/original")
    source.mkdir(parents=True)
    cases = [case()]
    if traces is None:
        invalid = trace(repeat=2)
        invalid.pop("review")
        invalid.update(status="judge_error", judge_error_category="ValueError")
        traces = [trace(score=1), invalid]
    manifest = {
        "suite": "dialogue", "status": status, "repeats": repeats,
        "expected_cases": 1, "expected_trials": repeats, "case_ids": ["case-1"],
        "selected_cases_sha256": digest(cases), "dataset_sha256": "original-dataset-hash",
        "models": {"agent": "original-agent", "judge": settings.agent_model},
        "generation": {"judge": judge_generation(settings)},
        "source_hashes": {"backend/original.py": "original-source-hash"},
        "knowledge": {"status": "ready", "original": True},
        "scoring": {
            "version": JUDGE_VERSION, "prompt_snapshot": "judge_prompt.md",
            "prompt_sha256": hashlib.sha256(JUDGE_PROMPT.encode()).hexdigest(),
        },
        "latency_version": LATENCY_VERSION, "concurrency": 16,
    }
    dump(source / "manifest.json", manifest)
    dump(source / "cases.json", cases)
    dump(source / "traces.json", traces)
    (source / "judge_prompt.md").write_text(JUDGE_PROMPT, encoding="utf-8")
    (source / "report.md").write_text("原始评测报告", encoding="utf-8")
    dump(source / "judge" / "original.json", {"content": "原始非法响应"})
    return settings, source


def retry_output(project_root, *, execute=False):
    """按实际输出分类查找本次补跑，不把用户标签当作目录名。"""
    parent = project_root / (
        "evaluation/reports/agent" if execute else ".run/evaluation-previews/agent"
    )
    outputs = [path for path in parent.iterdir()
               if path.is_dir() and path.name != "original"]
    assert len(outputs) == 1
    return outputs[0]


def forbid_execution(*_args, **_kwargs):
    raise AssertionError("裁判补跑不能执行 Agent、客户或创建业务数据库")


@pytest.mark.parametrize("legacy", [False, True])
def test_preview_preserves_sources_and_only_selects_missing_judge(tmp_path, monkeypatch, legacy):
    settings, source = source_run(tmp_path, legacy=legacy)
    monkeypatch.setattr(retry, "review_trace", forbid_execution)
    original = {str(path.relative_to(source)): path.read_bytes()
                for path in source.rglob("*") if path.is_file()}
    assert retry.run_retry(settings, "original", run_id="preview") == 0
    output = retry_output(tmp_path)
    manifest = load(output / "manifest.json")
    assert manifest["status"] == "preview"
    assert manifest["run_id"] == output.name
    assert manifest["run_label"] == "preview"
    assert output.name.startswith("original-agent_")
    plan = manifest["judge_retry"]
    assert plan["selected"] == [{"case_id": "case-1", "repeat": 2}]
    assert plan["retained_valid_trials"] == plan["selected_trials"] == 1
    assert plan["unavailable_trials"] == plan["completed_trials"] == plan["failed_trials"] == 0
    assert plan["executed"] is False
    for name, raw in original.items():
        assert (source / name).read_bytes() == (output / "source" / name).read_bytes() == raw
    assert load(output / "traces.json") == load(source / "traces.json")
    assert load(output / "summary.json")["model_requests"] == 0
    assert not settings.runtime_dir.exists()


def test_execution_preserves_low_score_measurements_and_original_configuration(tmp_path, monkeypatch):
    settings, source = source_run(tmp_path)
    calls = []

    async def review(_settings, item, selected_case, diagnostics):
        calls.append((item["case_id"], item["repeat"]))
        assert selected_case["id"] == item["case_id"]
        diagnostics.update(status="valid", usage={"total_tokens": 80}, attempts=[{"number": 1}])
        return trace(score=3)["review"]

    monkeypatch.setattr(retry, "review_trace", review)
    monkeypatch.setattr("evaluation.dialogue.runner.execute", forbid_execution)
    monkeypatch.setattr("evaluation.dialogue.runner.temporary_database", forbid_execution)
    monkeypatch.setattr("backend.database.connect", forbid_execution)
    assert retry.run_retry(
        settings, "agent/original", run_id="repaired", execute=True, concurrency=2,
    ) == 0
    output = retry_output(tmp_path, execute=True)
    assert output.name.startswith("original-agent_")
    assert not output.name.startswith(settings.agent_model + "_")
    assert calls == [("case-1", 2)]
    results = load(output / "traces.json")
    originals = load(source / "traces.json")
    assert results[0] == originals[0]
    assert results[1]["status"] == "completed" and "judge_error_category" not in results[1]
    assert results[1]["turns"] == originals[1]["turns"]
    assert results[1]["judge_usage"] == {"total_tokens": 80}
    manifest = load(output / "manifest.json")
    old = load(source / "manifest.json")
    for name in ("models", "generation", "knowledge", "source_hashes", "selected_cases_sha256"):
        assert manifest[name] == old[name]
    assert manifest["judge_retry"]["completed_trials"] == 1
    assert manifest["retry_source"]["files_sha256"]["traces.json"] == hashlib.sha256(
        (source / "traces.json").read_bytes()
    ).hexdigest()
    summary = load(output / "summary.json")
    assert summary["metrics"]["goal_completion"] == 2
    assert summary["metrics"]["agent_first_token_latency_p50_seconds"] == 1
    report = (output / "report.md").read_text(encoding="utf-8")
    assert "补跑" in report and "source/report.md" in report
    assert "新建独立数据库" not in report


def test_compressed_source_preserves_frozen_bytes_and_hashes(tmp_path, monkeypatch):
    settings, source = source_run(tmp_path)
    original_traces = load(source / "traces.json")
    stored_limit = max(path.stat().st_size for path in source.rglob("*") if path.is_file()) + 1
    compressed = source / "traces.json.gz"
    compressed.write_bytes(gzip.compress((source / "traces.json").read_bytes(), mtime=0))
    (source / "traces.json").unlink()
    original_bytes = compressed.read_bytes()
    # 文件限制只看磁盘上的压缩字节，不拒绝解压后较大的合法证据。
    extra = json.dumps({"evidence": "x" * stored_limit * 2}).encode()
    (source / "extra.json.gz").write_bytes(gzip.compress(extra, mtime=0))
    monkeypatch.setattr(retry, "MAX_SOURCE_FILE_BYTES", stored_limit)
    monkeypatch.setattr(io, "JSON_COMPRESSION_THRESHOLD", 1)

    async def review(*_args, **_kwargs):
        return trace(score=3)["review"]

    monkeypatch.setattr(retry, "review_trace", review)
    assert retry.run_retry(settings, "agent/original", execute=True) == 0
    output = retry_output(tmp_path, execute=True)
    manifest = load(output / "manifest.json")
    hashes = manifest["retry_source"]["files_sha256"]
    assert hashes["traces.json.gz"] == hashlib.sha256(original_bytes).hexdigest()
    assert "traces.json" not in hashes
    assert (output / "source/traces.json.gz").read_bytes() == original_bytes
    assert compressed.read_bytes() == original_bytes
    assert (output / "source/extra.json.gz").read_bytes() == (source / "extra.json.gz").read_bytes()
    assert not (output / "traces.json").exists()
    assert (output / "traces.json.gz").is_file()
    repaired = load(output / "traces.json")
    assert repaired[0] == original_traces[0]
    assert repaired[1]["status"] == "completed"


def test_oversized_source_is_rejected_before_creating_output(tmp_path, monkeypatch):
    settings, source = source_run(tmp_path)
    stored_limit = max(path.stat().st_size for path in source.rglob("*") if path.is_file()) + 1
    oversized = source / "oversized.json"
    original = b" " * (stored_limit + 1)
    oversized.write_bytes(original)
    monkeypatch.setattr(retry, "MAX_SOURCE_FILE_BYTES", stored_limit)
    monkeypatch.setattr(retry, "review_trace", forbid_execution)
    with pytest.raises(ValueError, match="先无损压缩来源"):
        retry.run_retry(settings, "agent/original", execute=True)
    assert oversized.read_bytes() == original
    assert list(source.parent.iterdir()) == [source]
    assert not (tmp_path / ".run").exists()


def test_failure_retains_raw_diagnostics_and_returns_incomplete(tmp_path, monkeypatch):
    settings, _source = source_run(tmp_path)

    async def review(_settings, _trace, _case, diagnostics):
        diagnostics.update(content="错误 JSON", usage={"total_tokens": 120},
                           attempts=[{"status": "error"}] * 3)
        raise ValueError("供应商可能含敏感内容，不写异常正文")

    monkeypatch.setattr(retry, "review_trace", review)
    assert retry.run_retry(settings, "original", run_id="failed", execute=True) == 1
    output = retry_output(tmp_path, execute=True)
    summary = load(output / "summary.json")
    assert summary["status"] == "incomplete"
    assert summary["judge_retry"]["failed_trials"] == 1
    name = digest(["case-1", 2])[:20] + ".json"
    diagnostics = load(output / "judge" / name)
    assert diagnostics["content"] == "错误 JSON" and len(diagnostics["attempts"]) == 3
    assert "敏感" not in json.dumps(diagnostics, ensure_ascii=False)
    assert load(output / "traces.json")[1]["judge_usage"] == {"total_tokens": 120}


def test_interruption_saves_traces_diagnostics_and_partial_summary(tmp_path, monkeypatch):
    settings, _source = source_run(tmp_path)

    async def review(_settings, _trace, _case, diagnostics):
        diagnostics.update(content="已收到的输出", usage={"total_tokens": 20})
        asyncio.current_task().cancel()
        await asyncio.sleep(0)

    monkeypatch.setattr(retry, "review_trace", review)
    with pytest.raises(asyncio.CancelledError):
        retry.run_retry(settings, "original", run_id="interrupted", execute=True)
    output = retry_output(tmp_path, execute=True)
    assert load(output / "manifest.json")["status"] == "interrupted"
    assert load(output / "summary.json")["status"] == "interrupted"
    assert load(output / "traces.json")[1]["judge_retry_status"] == "interrupted"
    name = digest(["case-1", 2])[:20] + ".json"
    assert load(output / "judge" / name)["usage"] == {"total_tokens": 20}


def test_missing_failed_dialogues_and_valid_null_reviews_are_not_retried(tmp_path, monkeypatch):
    valid_null = trace()
    valid_null["review"]["goal_completion"].update(score=None, null_reason="unassessable")
    broken = trace(repeat=2)
    broken.update(status="agent_error", error_category="TimeoutError")
    settings, _source = source_run(tmp_path, traces=[valid_null, broken], repeats=3)
    monkeypatch.setattr(retry, "review_trace", forbid_execution)
    assert retry.run_retry(settings, "original", run_id="unchanged", execute=True) == 1
    plan = load(retry_output(tmp_path, execute=True) / "manifest.json")["judge_retry"]
    assert plan["selected_trials"] == 0 and plan["retained_valid_trials"] == 1
    assert plan["unavailable_trials"] == 2


@pytest.mark.parametrize("change", ["prompt", "version", "latency", "model", "generation",
                                    "hash", "plan", "empty", "duplicate", "unplanned"])
def test_rejects_inconsistent_source_before_creating_output(tmp_path, change):
    settings, source = source_run(tmp_path)
    manifest = load(source / "manifest.json")
    traces = load(source / "traces.json")
    if change == "prompt":
        (source / "judge_prompt.md").write_text("不同评分标准", encoding="utf-8")
    elif change == "version":
        manifest["scoring"]["version"] = "old"
    elif change == "latency":
        manifest.pop("latency_version")
    elif change == "model":
        manifest["models"]["judge"] = "different"
    elif change == "generation":
        manifest["generation"]["judge"]["max_tokens"] = 10
    elif change == "hash":
        manifest["selected_cases_sha256"] = "changed"
    elif change == "plan":
        manifest["expected_trials"] = 800
    elif change == "empty":
        dump(source / "cases.json", [])
        manifest["selected_cases_sha256"] = digest([])
    elif change == "duplicate":
        traces.append(deepcopy(traces[0]))
    elif change == "unplanned":
        traces[0]["case_id"] = "unknown"
    dump(source / "manifest.json", manifest)
    dump(source / "traces.json", traces)
    with pytest.raises(ValueError):
        retry.run_retry(settings, "original", run_id="rejected")
    assert not (tmp_path / ".run").exists()


@pytest.mark.parametrize("run_name", ["../outside", "/absolute", "a/b", ""])
def test_rejects_source_and_output_path_traversal(tmp_path, run_name):
    settings, _source = source_run(tmp_path)
    with pytest.raises(ValueError):
        retry.run_retry(settings, run_name, run_id="new")
    with pytest.raises(ValueError):
        retry.run_retry(settings, "original", run_id=run_name)


def test_rejects_symlinks_before_creating_output(tmp_path):
    settings, source = source_run(tmp_path)
    (source / "outside.json").symlink_to(tmp_path / "missing.json")
    with pytest.raises(ValueError, match="符号链接"):
        retry.run_retry(settings, "original", run_id="rejected")
    assert not (tmp_path / ".run").exists()


def test_missing_source_does_not_create_output(tmp_path):
    settings, _source = source_run(tmp_path)
    for reference in (None, "agent/missing"):
        with pytest.raises(ValueError):
            retry.run_retry(settings, reference)
    assert not (tmp_path / ".run").exists()


def test_optional_label_does_not_inherit_source_label(tmp_path, monkeypatch):
    settings, source = source_run(tmp_path)
    original = load(source / "manifest.json")
    original["run_label"] = "earlier-label"
    dump(source / "manifest.json", original)
    monkeypatch.setattr(retry, "review_trace", forbid_execution)
    assert retry.run_retry(settings, "original") == 0
    output = retry_output(tmp_path)
    manifest = load(output / "manifest.json")
    assert manifest["run_id"] == output.name
    assert "run_label" not in manifest


@pytest.mark.parametrize("execute", [False, True])
def test_repeated_label_creates_separate_runs_without_overwriting_sources(
    tmp_path, monkeypatch, execute,
):
    settings, source = source_run(
        tmp_path, traces=[trace(), trace(repeat=2)], status="completed",
    )
    original = (source / "manifest.json").read_bytes()
    monkeypatch.setattr(retry, "review_trace", forbid_execution)
    assert retry.run_retry(settings, "original", run_id="original", execute=execute) == 0
    first = retry_output(tmp_path, execute=execute)
    first_manifest = (first / "manifest.json").read_bytes()
    assert retry.run_retry(settings, "original", run_id="original", execute=execute) == 0
    outputs = [path for path in first.parent.iterdir() if path != source]
    assert len(outputs) == 2
    assert all(path.name.startswith("original-agent_") for path in outputs)
    assert (first / "manifest.json").read_bytes() == first_manifest
    assert (source / "manifest.json").read_bytes() == original


def test_invalidated_source_stays_invalidated_after_judge_repair(tmp_path, monkeypatch):
    settings, _source = source_run(tmp_path, status="invalidated")

    async def review(*_args, **_kwargs):
        return trace()["review"]

    monkeypatch.setattr(retry, "review_trace", review)
    assert retry.run_retry(settings, "original", run_id="still-invalid", execute=True) == 1
    summary = load(retry_output(tmp_path, execute=True) / "summary.json")
    assert summary["status"] == "invalidated"
    assert summary["metrics"]["goal_completion"] is None
    assert summary["quality"]["goal_completion"]["observed_mean"] is None
    assert summary["judge_retry"]["completed_trials"] == 1


@pytest.mark.parametrize("field, reason", [
    ("sources_unchanged", "source_resources_changed"),
    ("knowledge_unchanged", "knowledge_index_changed"),
])
def test_interrupted_source_with_changed_resources_stays_invalidated(
    tmp_path, monkeypatch, field, reason,
):
    settings, source = source_run(tmp_path, status="interrupted")
    manifest = load(source / "manifest.json")
    manifest[field] = False
    dump(source / "manifest.json", manifest)
    (source / "report.md").unlink()

    async def review(*_args, **_kwargs):
        return trace()["review"]

    monkeypatch.setattr(retry, "review_trace", review)
    assert retry.run_retry(settings, "original", run_id="still-invalid", execute=True) == 1
    output = retry_output(tmp_path, execute=True)
    summary = load(output / "summary.json")
    assert summary["status"] == "invalidated"
    assert reason in summary["invalidation_reasons"]
    assert load(output / "manifest.json")["retry_source"]["report_path"] == "source/manifest.json"


def test_valid_low_score_with_conflicting_error_is_not_retried(tmp_path, monkeypatch):
    conflicting = trace(score=1)
    conflicting["judge_error_category"] = "ValueError"
    settings, _source = source_run(tmp_path, traces=[conflicting], repeats=1)
    monkeypatch.setattr(retry, "review_trace", forbid_execution)
    with pytest.raises(ValueError, match="同时标记"):
        retry.run_retry(settings, "original", run_id="rejected", execute=True)


def test_multiple_retries_preserve_direct_source_without_recursive_archive(tmp_path, monkeypatch):
    settings, source = source_run(tmp_path)
    dump(source / "source" / "older.json", {"previous": "archive"})
    monkeypatch.setattr(retry, "review_trace", forbid_execution)
    assert retry.run_retry(settings, "original", run_id="preview") == 0
    output = retry_output(tmp_path)
    assert (output / "source/manifest.json").is_file()
    assert not (output / "source/source").exists()


def test_source_code_change_during_judging_invalidates_result(tmp_path, monkeypatch):
    settings, _source = source_run(tmp_path)

    async def review(*_args, **_kwargs):
        path = tmp_path / "backend/changed.py"
        path.parent.mkdir()
        path.write_text("changed = True", encoding="utf-8")
        return trace()["review"]

    monkeypatch.setattr(retry, "review_trace", review)
    assert retry.run_retry(settings, "original", run_id="changed", execute=True) == 1
    summary = load(retry_output(tmp_path, execute=True) / "summary.json")
    assert summary["invalidation_reasons"] == ["judge_retry_sources_changed"]


def test_retry_limits_concurrency_without_skipping_selected_calls(tmp_path, monkeypatch):
    pending = []
    for number in range(1, 5):
        item = trace(repeat=number)
        item.pop("review")
        item["status"] = "judge_error"
        pending.append(item)
    settings, _source = source_run(tmp_path, traces=pending, repeats=4)
    active, peak, calls = 0, 0, []

    async def review(_settings, item, _case, diagnostics):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(item["repeat"])
        await asyncio.sleep(0.01)
        active -= 1
        return trace()["review"]

    monkeypatch.setattr(retry, "review_trace", review)
    assert retry.run_retry(settings, "original", run_id="bounded", execute=True, concurrency=2) == 0
    assert peak == 2 and sorted(calls) == [1, 2, 3, 4]
    assert load(retry_output(tmp_path, execute=True) / "manifest.json")["judge_retry"]["completed_trials"] == 4
