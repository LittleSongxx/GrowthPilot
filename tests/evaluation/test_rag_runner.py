"""用真实 PostgreSQL + pgvector 索引和确定性向量核验离线 RAG 评测链路。"""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import psycopg
import pytest

from backend.agent_harness.rag import index as index_module
from backend.agent_harness.rag.index import DIMENSIONS, PolicyIndex
from backend.database import connect
from evaluation.rag.runner import CorpusMismatchError, run
from tests.db import database_for


class DeterministicEmbedder:
    def embed(self, texts, **kwargs):
        for _ in texts:
            yield [1.0, *([0.0] * (DIMENSIONS - 1))]


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        runtime_dir=tmp_path / "runtime", project_root=tmp_path, app_db=database_for(tmp_path),
    )
    index = PolicyIndex(
        settings.runtime_dir / "knowledge", database=settings.app_db, embedder=DeterministicEmbedder(),
    )
    chunk = {
        "id": "rider-fees:0",
        "document_id": "rider-fees",
        "scenario_id": "meituan_rider",
        "title": "报名说明",
        "text": "骑手报名免费，装备费用需要单独核对。",
        "url": "https://example.org/fees",
        "fetched_at": "2026-10-01T00:00:00Z",
        "content_hash": "original-document-hash",
    }
    other = {
        **chunk,
        "id": "rider-safety:0",
        "document_id": "rider-safety",
        "text": "配送时请注意交通安全。",
    }
    version = index.build([chunk, other])["scenarios"][0]["version"]
    frozen = settings.project_root / "evaluation/datasets/corpus/meituan_rider.jsonl"
    frozen.parent.mkdir(parents=True)
    frozen.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in [chunk, other]) + "\n"
    )
    case = {
        "id": "rider-registration",
        "scenario_id": chunk["scenario_id"],
        "corpus_version": version,
        "question": "骑手报名要交钱吗？",
        "relevant_chunk_ids": [chunk["id"]],
        "evidence": [
            {
                "chunk_id": chunk["id"],
                "document_id": chunk["document_id"],
                "content_hash": chunk["content_hash"],
                "quote": "骑手报名免费",
            }
        ],
    }

    def policy_index(runtime, *, database, namespace="production"):
        if database == index.database and namespace == index.namespace:
            return index
        return PolicyIndex(
            runtime, database=database, namespace=namespace, embedder=DeterministicEmbedder(),
        )

    monkeypatch.setattr(index_module, "PolicyIndex", policy_index)
    return settings, index, case


def test_preview_needs_no_index_or_embedding(tmp_path, monkeypatch):
    monkeypatch.setattr(index_module, "PolicyIndex", lambda *_: pytest.fail("预览不应创建检索器"))
    monkeypatch.setattr(index_module, "_load_model", lambda *_: pytest.fail("预览不应加载模型"))
    case = {
        "id": "example",
        "scenario_id": "meituan_rider",
        "corpus_version": "frozen",
        "question": "报名费用？",
        "relevant_chunk_ids": ["c1"],
        "evidence": [
            {"chunk_id": "c1", "document_id": "d1", "content_hash": "h1", "quote": "免费"}
        ],
    }
    settings = SimpleNamespace(runtime_dir=tmp_path / "missing")
    summary = run(settings, tmp_path / "report", [case])
    assert summary["status"] == "preview" and summary["planned"] == 1
    assert summary["metrics"] is None
    assert not settings.runtime_dir.exists()


def test_execution_uses_one_default_search_and_preserves_raw_result(
    prepared, tmp_path, monkeypatch
):
    settings, index, case = prepared
    expected = index.search(case["question"], case["scenario_id"])
    original_search = index.search
    calls = []

    def traced_search(*args, **kwargs):
        calls.append((args, kwargs))
        return original_search(*args, **kwargs)

    monkeypatch.setattr(index, "search", traced_search)
    before = index.snapshot()
    output = tmp_path / "report"
    summary = run(settings, output, [case], execute=True)
    assert calls == [((case["question"], case["scenario_id"]), {})]
    assert summary["status"] == "completed"
    assert summary["index"]["source"] == "production_index"
    assert summary["metrics"]["recall_at_8"] == 1
    assert summary["metrics"]["precision_at_8"] == 1 / 8
    row = json.loads((output / "rag_results.json").read_text())["cases"][0]
    assert row["retrieval"] == expected
    assert before == index.snapshot()


@pytest.mark.parametrize("changed", ["version", "document", "hash", "quote", "id"])
def test_changed_gold_or_corpus_fails_before_search(prepared, tmp_path, monkeypatch, changed):
    settings, index, original = prepared
    case = deepcopy(original)
    if changed == "version":
        case["corpus_version"] = "old-version"
    elif changed == "document":
        case["evidence"][0]["document_id"] = "another-document"
    elif changed == "hash":
        case["evidence"][0]["content_hash"] = "another-hash"
    elif changed == "quote":
        case["evidence"][0]["quote"] = "报名必须缴费"
    else:
        case["relevant_chunk_ids"] = ["missing"]
        case["evidence"][0]["chunk_id"] = "missing"
    monkeypatch.setattr(index, "search", lambda *_: pytest.fail("标注不匹配时不应执行检索"))
    output = tmp_path / "report"
    with pytest.raises(CorpusMismatchError):
        run(settings, output, [case], execute=True)
    summary = json.loads((output / "rag_summary.json").read_text())
    assert summary["status"] == "preflight_failed"
    assert summary["metrics"] is None and summary["not_run"] == 1


def test_one_retrieval_error_remains_in_the_planned_denominator(prepared, tmp_path, monkeypatch):
    settings, index, case = prepared
    second = {**case, "id": "second", "question": "装备是否收费？"}
    original_search = index.search

    def sometimes_fails(query, scenario):
        if query == case["question"]:
            raise RuntimeError("本地向量模型执行失败")
        return original_search(query, scenario)

    monkeypatch.setattr(index, "search", sometimes_fails)
    summary = run(settings, tmp_path / "report", [case, second], execute=True)
    assert summary["status"] == "completed_with_errors"
    assert (summary["planned"], summary["scored"], summary["errors"]) == (2, 1, 1)
    assert summary["metrics"]["recall_at_8"] == 1 / 2
    rows = json.loads((tmp_path / "report/rag_results.json").read_text())["cases"]
    assert rows[0]["error"]["type"] == "RuntimeError"
    assert rows[1]["status"] == "scored"


def test_midrun_corpus_change_stops_without_publishing_comparable_scores(
    prepared, tmp_path, monkeypatch
):
    settings, index, case = prepared
    cases = [dict(case, id=f"case-{i}") for i in range(3)]
    original_search = index.search
    calls = []

    def changing_search(query, scenario):
        result = original_search(query, scenario)
        calls.append(query)
        if len(calls) == 2:
            result["data"]["corpus_version"] = "rebuilt-during-run"
        return result

    monkeypatch.setattr(index, "search", changing_search)
    with pytest.raises(CorpusMismatchError, match="执行期间"):
        run(settings, tmp_path / "report", cases, execute=True)
    summary = json.loads((tmp_path / "report/rag_summary.json").read_text())
    assert len(calls) == 2
    assert summary["status"] == "corpus_changed" and summary["metrics"] is None
    assert (summary["scored"], summary["errors"], summary["not_run"]) == (1, 1, 1)
    assert summary["by_scenario"]["meituan_rider"]["metrics"] is None


def test_empty_gold_is_rejected_even_in_preview(prepared, tmp_path):
    settings, _, case = prepared
    with pytest.raises(ValueError, match="相关片段 ID"):
        run(settings, tmp_path / "report", [dict(case, relevant_chunk_ids=[])])


def test_missing_production_index_builds_the_entire_frozen_corpus(prepared, tmp_path):
    original_settings, _, case = prepared
    settings = SimpleNamespace(
        runtime_dir=tmp_path / "fresh-runtime", project_root=original_settings.project_root,
        app_db=database_for(tmp_path / "fresh"),
    )
    summary = run(settings, tmp_path / "report", [case], execute=True)
    assert summary["index"]["source"] == "frozen_index_built"
    isolated_path = settings.runtime_dir / "evaluation/knowledge"
    assert summary["index"]["namespace"].startswith("eval_")
    assert summary["index"]["identity"].startswith(settings.app_db.identity)
    assert not (settings.runtime_dir / "knowledge").exists()
    assert PolicyIndex(
        isolated_path, database=settings.app_db, namespace=summary["index"]["namespace"],
    ).status()["scenarios"][0]["chunk_count"] == 2
    assert summary["metrics"]["recall_at_8"] == 1


def test_different_production_version_is_untouched_and_fallback_is_reusable(
    prepared, tmp_path, monkeypatch
):
    settings, production, case = prepared
    with connect(production.database) as db:
        db.execute("UPDATE rag_corpora SET version = 'newer-production-version'")
    before = production.snapshot()
    first = run(settings, tmp_path / "first", [case], execute=True)
    assert first["index"]["source"] == "frozen_index_built"
    assert production.snapshot() == before
    monkeypatch.setattr(PolicyIndex, "build", lambda *_: pytest.fail("同版本隔离索引无需重建"))
    second = run(settings, tmp_path / "second", [case], execute=True)
    assert second["index"]["source"] == "frozen_index_reused"
    assert first["metrics"] == second["metrics"]
    assert production.snapshot() == before


def test_snapshot_hash_is_verified_before_any_embedding(prepared, tmp_path, monkeypatch):
    original_settings, _, case = prepared
    settings = SimpleNamespace(
        runtime_dir=tmp_path / "fresh-runtime", project_root=original_settings.project_root,
        app_db=database_for(tmp_path / "fresh"),
    )
    frozen = settings.project_root / "evaluation/datasets/corpus/meituan_rider.jsonl"
    frozen.write_text(frozen.read_text().replace("报名免费", "报名收费"))
    monkeypatch.setattr(PolicyIndex, "build", lambda *_: pytest.fail("快照不匹配时不能向量化"))
    with pytest.raises(CorpusMismatchError, match="冻结语料 hash"):
        run(settings, tmp_path / "report", [case], execute=True)
    assert not settings.runtime_dir.exists()
    summary = json.loads((tmp_path / "report/rag_summary.json").read_text())
    assert summary["status"] == "preflight_failed" and summary["metrics"] is None


def test_built_index_must_still_match_frozen_version(prepared, tmp_path):
    original_settings, _, case = prepared
    settings = SimpleNamespace(
        runtime_dir=tmp_path / "fresh-runtime", project_root=original_settings.project_root,
        app_db=database_for(tmp_path / "fresh"),
    )
    frozen = settings.project_root / "evaluation/datasets/corpus/meituan_rider.jsonl"
    chunks = [json.loads(line) for line in frozen.read_text().splitlines()]
    chunks[0]["valid_until"] = "2000-01-01"
    payloads = [json.dumps(chunk, ensure_ascii=False, sort_keys=True) for chunk in chunks]
    frozen.write_text("\n".join(payloads) + "\n")
    changed = {
        **case,
        "corpus_version": hashlib.sha256("\n".join(sorted(payloads)).encode()).hexdigest(),
    }
    # 真实构建会过滤过期条款；即便文件哈希正确，也不能把新索引当作原语料评分。
    with pytest.raises(CorpusMismatchError, match="corpus_version"):
        run(settings, tmp_path / "report", [changed], execute=True)
    summary = json.loads((tmp_path / "report/rag_summary.json").read_text())
    assert summary["status"] == "preflight_failed" and summary["metrics"] is None


def test_connection_failure_report_does_not_serialize_credentials(tmp_path, monkeypatch):
    from evaluation.rag import runner

    case = {
        "id": "example", "scenario_id": "meituan_rider", "corpus_version": "frozen",
        "question": "报名费用？", "relevant_chunk_ids": ["c1"],
        "evidence": [
            {"chunk_id": "c1", "document_id": "d1", "content_hash": "h1", "quote": "免费"},
        ],
    }
    def unavailable(*_args, **_kwargs):
        raise psycopg.OperationalError("password=secret-cannot-appear")

    monkeypatch.setattr(runner, "_prepare_index", unavailable)
    output = tmp_path / "report"
    with pytest.raises(psycopg.OperationalError):
        run(SimpleNamespace(), output, [case], execute=True)
    report = json.loads((output / "rag_results.json").read_text())
    assert report["error"] == {"type": "OperationalError"}
    assert "secret-cannot-appear" not in (output / "rag_results.json").read_text()
    assert json.loads((output / "rag_summary.json").read_text())["status"] == "preflight_failed"
