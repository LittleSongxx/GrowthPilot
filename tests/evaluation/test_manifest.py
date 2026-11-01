"""验证评测运行清单记录真实语料，资源变化和用户中断不会留下完整成绩。"""

import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import psycopg
import pytest

from backend.database import DatabaseConfig, connect
from backend.settings import Settings
from evaluation.common.knowledge import corpus_versions, knowledge_snapshot
from evaluation.dialogue.metrics import LATENCY_VERSION
from evaluation.runner import _source_hashes, make_manifest, run_suite
from tests.db import database_for, schema_exists

SCENARIO = "meituan_rider"


def create_index(database, version="version-one", signature=None):
    from backend.agent_harness.rag.index import INDEX_SIGNATURE

    with connect(database) as connection, connection.transaction():
        connection.execute(
            "INSERT INTO rag_index_meta VALUES ('production', 'signature', %s)",
            (signature or INDEX_SIGNATURE,),
        )
        connection.execute(
            "INSERT INTO rag_corpora VALUES ('production', %s, %s, '2026-11-02T01:00:00Z', 15)",
            (SCENARIO, version),
        )
    return database


def test_snapshot_reads_model_and_chunk_metadata_without_loading_or_modifying_index(
    tmp_path, monkeypatch
):
    from backend.agent_harness.rag import index

    database = create_index(database_for(tmp_path))
    before = index.PolicyIndex(tmp_path, database=database).snapshot()

    def forbidden(*args, **kwargs):
        raise AssertionError("元数据读取不能加载模型或创建数据库")

    monkeypatch.setattr(index, "_load_model", forbidden)
    monkeypatch.setattr(index, "initialize_database", forbidden)
    snapshot = knowledge_snapshot(tmp_path, [SCENARIO, "didi_driver"], database=database)
    assert snapshot["status"] == "partial"
    assert snapshot["model"] == index.MODEL_NAME and snapshot["dimensions"] == index.DIMENSIONS
    assert snapshot["index_signature"] == index.INDEX_SIGNATURE
    assert snapshot["scenarios"][SCENARIO] == {
        "version": "version-one",
        "indexed_at": "2026-11-02T01:00:00Z",
        "chunk_count": 15,
    }
    assert corpus_versions(snapshot) == {SCENARIO: "version-one", "didi_driver": None}
    assert index.PolicyIndex(tmp_path, database=database).snapshot() == before
    assert database.dsn not in json.dumps(snapshot)


def test_absent_and_unreadable_index_are_distinct_and_neither_is_created(tmp_path, monkeypatch):
    from backend.agent_harness.rag.index import PolicyIndex

    database = replace(database_for(tmp_path), schema="missing_" + tmp_path.name.replace("-", "_")[-40:])
    runtime_dir = tmp_path / "not-created"
    absent = knowledge_snapshot(runtime_dir, [SCENARIO], database=database)
    assert absent["status"] == "no_data" and corpus_versions(absent) == {SCENARIO: None}
    assert not runtime_dir.exists() and not schema_exists(database)

    def corrupted(*_args, **_kwargs):
        raise psycopg.DatabaseError("不可序列化的连接凭据")

    monkeypatch.setattr(PolicyIndex, "snapshot", corrupted)
    failed = knowledge_snapshot(tmp_path, [SCENARIO], database=database)
    assert failed["status"] == "unreadable" and failed["error_category"] == "DatabaseError"
    assert "error_message" not in failed


def test_unavailable_database_is_distinct_from_missing_corpus(tmp_path, monkeypatch):
    from backend.agent_harness.rag.index import PolicyIndex

    database = database_for(tmp_path)

    def unavailable(*_args, **_kwargs):
        raise psycopg.OperationalError("不可序列化的连接凭据")

    monkeypatch.setattr(PolicyIndex, "snapshot", unavailable)
    snapshot = knowledge_snapshot(tmp_path, [SCENARIO], database=database)
    assert snapshot["status"] == "unavailable"
    assert snapshot["error_category"] == "OperationalError"
    assert "不可序列化" not in json.dumps(snapshot)


def test_source_hashes_include_business_json_and_exclude_credentials_and_artifacts(tmp_path):
    files = {
        "backend/scenarios/meituan_rider/catalog.json": "业务目录",
        "backend/scenarios/meituan_rider/samples.json": "场景示例",
        "backend/config/models.yaml": "模型配置",
        "backend/migrations/001_business.sql": "数据库迁移",
        "backend/.env": "不可读取的凭据",
        "backend/config/credentials.json": "不可读取的凭据",
        "backend/runtime/session.json": "运行中客户信息",
        "evaluation/datasets/dialogue/meituan_rider.json": "由题集哈希另行保存",
        "evaluation/reports/previous/report.md": "历史报告",
        "evaluation/judges/prompt.md": "正式评分量表",
    }
    for name, value in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    hashes = _source_hashes(tmp_path)
    assert set(hashes) == {
        "backend/scenarios/meituan_rider/catalog.json",
        "backend/scenarios/meituan_rider/samples.json",
        "backend/config/models.yaml",
        "backend/migrations/001_business.sql",
        "evaluation/judges/prompt.md",
    }
    path = tmp_path / "backend/scenarios/meituan_rider/catalog.json"
    path.write_text("更新后的目录")
    assert (
        _source_hashes(tmp_path)[str(path.relative_to(tmp_path))]
        != hashes[str(path.relative_to(tmp_path))]
    )


def setup_run(tmp_path, monkeypatch, run):
    import backend.agent_harness
    from evaluation import runner
    from evaluation.dialogue import runner as dialogue_runner

    datasets = [{"scenario_id": SCENARIO, "version": "dialogue-v3", "cases": [{"id": "case-one"}]}]
    monkeypatch.setattr(runner, "load_dialogue_datasets", lambda *args: datasets)
    monkeypatch.setattr(
        backend.agent_harness,
        "load_scenario",
        lambda path: SimpleNamespace(snapshot_hash="scenario-version"),
    )
    monkeypatch.setattr(dialogue_runner, "run", run)
    return Settings(
        tmp_path, tmp_path / "runtime", database_for(tmp_path), agent_api_key="unused"
    )


def run_output(tmp_path, *, execute=False, category="agent"):
    """从正式分类或本地预览目录取得本测试唯一的一轮产物。"""
    base = "evaluation/reports" if execute else ".run/evaluation-previews"
    outputs = list((tmp_path / base / category).iterdir())
    assert len(outputs) == 1
    return outputs[0]


def completed_summary():
    metrics = {
        "goal_completion": 2,
        "problem_resolution": 2,
        "factual_reliability": 2,
        "communication_quality": 2,
        "average_agent_tokens": 200,
        "agent_first_token_latency_p50_seconds": 0.75,
        "agent_first_token_latency_p95_seconds": 1.2,
    }
    quality = {
        name: {
            "mean": 2, "observed_mean": 2,
            "score_distribution": {"1": 0, "2": 2, "3": 0, "4": 0, "5": 0},
            "assessed_runs": 2, "not_applicable_runs": 0, "unassessable_runs": 0,
            "expected_runs": 2, "applicable_runs": 2, "coverage": 1,
            "low_score_cases": [{
                "case_id": "case-one", "repeat": 1, "score": 2,
                "reason": "关键问题未处理",
            }],
        }
        for name in ("goal_completion", "problem_resolution", "factual_reliability",
                     "communication_quality")
    }
    summary = {
        "status": "completed",
        "latency_version": LATENCY_VERSION,
        "metrics": metrics,
        "quality": quality,
        "counts": {"expected_runs": 2, "recorded_runs": 2},
        "latency": {"observed_p50_seconds": 0.75, "observed_p95_seconds": 1.2},
    }
    return {
        **summary,
        "by_scenario": {SCENARIO: deepcopy(summary)},
        "by_goal_type": {"progression": deepcopy(summary)},
    }


def test_preview_records_missing_knowledge_without_creating_runtime_or_model_clients(
    tmp_path, monkeypatch
):
    from evaluation import judges

    def preview(settings, output, cases, *, execute, repeats, concurrency):
        assert execute is False
        assert concurrency == 16
        return {"status": "preview", "metrics": None}

    settings = setup_run(tmp_path, monkeypatch, preview)
    changed_prompt = tmp_path / "prompt.md"
    changed_prompt.write_text("源码文件已被编辑，但裁判仍使用之前加载的字符串。")
    monkeypatch.setattr(judges, "JUDGE_PROMPT_PATH", changed_prompt)
    assert run_suite("dialogue", settings, run_id="preview") == 0
    output = run_output(tmp_path)
    manifest = json.loads((output / "manifest.json").read_text())
    from evaluation.judges import JUDGE_PROMPT, JUDGE_VERSION

    assert manifest["status"] == "preview"
    assert manifest["run_id"] == output.name
    assert manifest["run_label"] == "preview"
    assert not (tmp_path / "evaluation/reports").exists()
    assert manifest["schema_version"] == "evaluation-v2"
    assert manifest["latency_version"] == LATENCY_VERSION
    assert manifest["judge_output_policy"]["parser"] == "json_then_json_repair"
    assert manifest["judge_output_policy"]["json_repair_version"]
    assert manifest["judge_output_policy"]["max_attempts"] == 3
    assert manifest["generation"]["judge"] == {
        "thinking": False, "reasoning_effort": None, "temperature": 0.0,
        "configured_temperature": 0.0, "temperature_used": True, "max_tokens": 16384,
    }
    assert manifest["scoring"] == {
        "version": JUDGE_VERSION,
        "prompt_path": "evaluation/judges/prompt.md",
        "prompt_snapshot": "judge_prompt.md",
        "prompt_sha256": hashlib.sha256(JUDGE_PROMPT.encode()).hexdigest(),
    }
    snapshot = output / "judge_prompt.md"
    assert snapshot.read_text(encoding="utf-8") == JUDGE_PROMPT
    assert snapshot.read_text(encoding="utf-8") != changed_prompt.read_text()
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == manifest["scoring"]["prompt_sha256"]
    assert manifest["concurrency"] == 16
    assert manifest["memory_mode"] == "fresh_database_per_run"
    assert manifest["repeat_semantics"] == "sequential_customer_calls"
    assert manifest["knowledge"]["status"] == "no_data"
    assert manifest["corpus_versions"] == {SCENARIO: None}
    assert "knowledge_after" not in manifest and not settings.runtime_dir.exists()
    report = (output / "report.md").read_text()
    assert "客户并发上限：16" in report
    assert "客户案例：1；每位客户计划通话：2 次；计划通话：2 次" in report
    assert "同一客户后续通话保留本轮生成的记忆" in report
    assert "没有运行检索或调用对话模型，不产生质量成绩" in report
    assert f"评分版本：{JUDGE_VERSION}；" in report
    assert "[查看本次评分 prompt](judge_prompt.md)" in report
    assert "| 观察均分 |" not in report


def test_rag_manifest_preserves_original_schema_and_has_no_dialogue_scoring(tmp_path, monkeypatch):
    settings = setup_run(tmp_path, monkeypatch, lambda *_args: None)
    cases = [{"id": "case-one", "scenario_id": SCENARIO}]
    datasets = [{"scenario_id": SCENARIO, "version": "rag-v1", "cases": cases}]
    manifest = make_manifest(settings, datasets, cases, "rag")
    assert manifest["schema_version"] == "evaluation-v1"
    assert "scoring" not in manifest and "concurrency" not in manifest


def test_manifest_records_effective_generation_when_judge_thinking_is_disabled(tmp_path, monkeypatch):
    settings = replace(
        setup_run(tmp_path, monkeypatch, lambda *_args: None),
        judge_extra_body={"thinking": {"type": "disabled"}}, judge_temperature=0.3,
    )
    cases = [{"id": "case-one", "scenario_id": SCENARIO}]
    datasets = [{"scenario_id": SCENARIO, "version": "dialogue-v1", "cases": cases}]
    manifest = make_manifest(settings, datasets, cases, "dialogue")
    assert manifest["generation"]["judge"] == {
        "thinking": False, "reasoning_effort": None, "temperature": 0.3,
        "configured_temperature": 0.3, "temperature_used": True, "max_tokens": 16384,
    }


def test_manifest_records_models_and_generation_by_purpose(tmp_path, monkeypatch):
    settings = replace(
        setup_run(tmp_path, monkeypatch, lambda *_args: None),
        agent_model="agent-model", agent_temperature=None,
        customer_model="customer-model", customer_temperature=0.4, customer_max_tokens=900,
        profile_model="profile-model", profile_temperature=0.2, profile_max_tokens=1500,
        judge_model="judge-model", judge_temperature=None,
        judge_reasoning_effort="high", judge_max_tokens=5000,
    )
    cases = [{"id": "case-one", "scenario_id": SCENARIO}]
    datasets = [{"scenario_id": SCENARIO, "version": "dialogue-v1", "cases": cases}]

    manifest = make_manifest(settings, datasets, cases, "dialogue")

    assert manifest["models"] == {
        "agent": "agent-model", "customer": "customer-model",
        "profile": "profile-model", "judge": "judge-model",
    }
    assert manifest["generation"]["agent"]["temperature"] is None
    assert manifest["generation"]["customer"] == {"temperature": 0.4, "max_tokens": 900}
    assert manifest["generation"]["profile"] == {"temperature": 0.2, "max_tokens": 1500}
    assert manifest["generation"]["judge"]["temperature_used"] is False
    assert manifest["generation"]["judge"]["max_tokens"] == 5000
    assert manifest["customer_source"] == "dynamic_openai_compatible"


def test_manifest_resolves_inherited_models_after_replacing_agent(tmp_path, monkeypatch):
    settings = replace(
        setup_run(tmp_path, monkeypatch, lambda *_args: None), agent_model="updated-agent-model",
    )
    cases = [{"id": "case-one", "scenario_id": SCENARIO}]
    datasets = [{"scenario_id": SCENARIO, "version": "dialogue-v1", "cases": cases}]

    manifest = make_manifest(settings, datasets, cases, "dialogue")

    assert manifest["models"] == dict.fromkeys(
        ("agent", "customer", "profile", "judge"), "updated-agent-model",
    )
    assert settings.customer_model == settings.profile_model == settings.judge_model == ""


def test_custom_concurrency_is_forwarded_and_frozen_in_preview(tmp_path, monkeypatch):
    def preview(settings, output, cases, *, execute, repeats, concurrency):
        assert execute is False and concurrency == 4 and repeats == 3
        return {"status": "preview", "metrics": None, "concurrency": concurrency}

    settings = setup_run(tmp_path, monkeypatch, preview)
    assert run_suite("dialogue", settings, run_id="preview-four", repeats=3, concurrency=4) == 0
    manifest = json.loads((run_output(tmp_path) / "manifest.json").read_text())
    assert manifest["concurrency"] == 4 and manifest["expected_trials"] == 3


@pytest.mark.parametrize("execute", [False, True])
@pytest.mark.parametrize("suite", ["dialogue", "rag"])
def test_model_runs_without_label_use_model_and_manifest_creation_time(
    tmp_path, monkeypatch, execute, suite
):
    from backend.agent_harness.rag.index import MODEL_NAME
    from evaluation import runner
    from evaluation.rag import runner as rag_runner

    def evaluated(_settings, _output, _cases, **kwargs):
        assert kwargs["execute"] is execute
        return {"status": "completed" if execute else "preview", "metrics": None}

    settings = replace(setup_run(tmp_path, monkeypatch, evaluated), agent_model="sample-model")
    monkeypatch.setattr(runner, "load_rag_datasets", runner.load_dialogue_datasets)
    monkeypatch.setattr(rag_runner, "run", evaluated)
    assert run_suite(suite, settings, execute=execute) == 0
    output = run_output(tmp_path, execute=execute, category="rag" if suite == "rag" else "agent")
    manifest = json.loads((output / "manifest.json").read_text())
    stamp = datetime.fromisoformat(manifest["created_at"]).astimezone(
        ZoneInfo("Asia/Shanghai")
    ).strftime("%Y-%m-%d_%H-%M-%S")
    model_name = MODEL_NAME.replace("/", "-") if suite == "rag" else "sample-model"
    assert output.name == f"{model_name}_{stamp}"
    assert manifest["run_id"] == output.name and "run_label" not in manifest
    assert (output / "report.md").is_file()
    if not execute:
        assert not (tmp_path / "evaluation/reports").exists()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "16"])
def test_invalid_concurrency_is_rejected_before_creating_artifacts(tmp_path, value):
    settings = Settings(tmp_path, tmp_path / "runtime", DatabaseConfig.from_env())
    with pytest.raises(ValueError, match="concurrency 必须是正整数"):
        run_suite("dialogue", settings, run_id="invalid", concurrency=value)
    assert not (tmp_path / "evaluation").exists()


@pytest.mark.parametrize("suite", ["rag", "contracts", "business-coverage"])
def test_other_suites_reject_custom_dialogue_concurrency(tmp_path, suite):
    settings = Settings(tmp_path, tmp_path / "runtime", DatabaseConfig.from_env())
    with pytest.raises(ValueError, match="只用于 dialogue"):
        run_suite(suite, settings, run_id="invalid", concurrency=4)
    assert not (tmp_path / "evaluation").exists()


@pytest.mark.parametrize("change", ["none", "corpus", "business", "unreadable"])
def test_run_records_actual_versions_and_invalidates_changed_resources(
    tmp_path, monkeypatch, change
):
    def execute(settings, output, cases, **kwargs):
        if change == "corpus":
            with connect(index_database) as connection:
                connection.execute("UPDATE rag_corpora SET version = 'version-two'")
        elif change == "business":
            catalog.write_text('{"price": 20}')
        elif change == "unreadable":
            from backend.agent_harness.rag.index import PolicyIndex

            def corrupted(*_args, **_kwargs):
                raise psycopg.DatabaseError("不可序列化的连接凭据")

            monkeypatch.setattr(PolicyIndex, "snapshot", corrupted)
        return completed_summary()

    settings = setup_run(tmp_path, monkeypatch, execute)
    index_database = create_index(settings.app_db)
    catalog = tmp_path / f"backend/scenarios/{SCENARIO}/catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"price": 10}')
    code = run_suite("dialogue", settings, execute=True, run_id="live")
    folder = run_output(tmp_path, execute=True)
    manifest, summary = [
        json.loads((folder / name).read_text()) for name in ("manifest.json", "summary.json")
    ]
    assert manifest["corpus_versions"] == {SCENARIO: "version-one"}
    if change == "none":
        assert code == 0 and manifest["knowledge_unchanged"] is True
        assert summary["status"] == "completed" and summary["metrics"]["goal_completion"] == 2
        assert summary["quality"]["goal_completion"]["observed_mean"] == 2
    else:
        assert code == 1 and manifest["status"] == summary["status"] == "invalidated"
        assert all(value is None for value in summary["metrics"].values())
        assert all(value is None for value in summary["by_scenario"][SCENARIO]["metrics"].values())
        assert all(value is None for value in summary["by_goal_type"]["progression"]["metrics"].values())
        for group in (summary, summary["by_scenario"][SCENARIO],
                      summary["by_goal_type"]["progression"]):
            assert group["counts"]["expected_runs"] == 2
            for row in group["quality"].values():
                assert row["mean"] is row["observed_mean"] is None
                assert row["score_distribution"]["2"] == 2
                assert row["assessed_runs"] == 2 and row["coverage"] == 1
                assert row["low_score_cases"][0]["reason"] == "关键问题未处理"
                assert "evidence" not in row["low_score_cases"][0]
            assert group["latency"]["observed_p50_seconds"] is None
            assert group["latency"]["observed_p95_seconds"] is None
        report = (folder / "report.md").read_text()
        assert "正式均分与观察均分均已撤回" in report
        expected_reason = {
            "corpus": "knowledge_index_changed",
            "business": "source_resources_changed",
            "unreadable": "knowledge_index_unverified",
        }[change]
        assert expected_reason in summary["invalidation_reasons"]
    if change == "corpus":
        assert manifest["corpus_versions_after"] == {SCENARIO: "version-two"}
        assert manifest["knowledge_unchanged"] is False


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, asyncio.CancelledError])
def test_interruption_records_manifest_then_propagates(tmp_path, monkeypatch, interruption):
    def interrupted(*args, **kwargs):
        raise interruption()

    settings = setup_run(tmp_path, monkeypatch, interrupted)
    create_index(settings.app_db)
    with pytest.raises(interruption):
        run_suite("dialogue", settings, execute=True, run_id="interrupted")
    manifest = json.loads((run_output(tmp_path, execute=True) / "manifest.json").read_text())
    assert manifest["status"] == "interrupted"
    assert manifest["error_category"] == interruption.__name__
    assert manifest["knowledge_unchanged"] is True
