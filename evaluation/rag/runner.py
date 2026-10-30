"""先核验冻结标注，再调用一次真实检索并保存可回放的结果。"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from backend.paths import portable_path, redact_local_paths
from evaluation.common.io import dump
from evaluation.rag.metrics import CUTOFFS, MAX_RESULTS, aggregate, score


class CorpusMismatchError(ValueError):
    """本地语料与冻结标注不一致，必须重新审阅标注后才能评价。"""


def _require_text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串。")
    return value


def _validate_cases(cases: list[dict]) -> None:
    if not cases:
        raise ValueError("至少选择一个 RAG 评测案例。")
    identities = set()
    versions = {}
    for case in cases:
        for field in ("id", "scenario_id", "question", "corpus_version"):
            _require_text(case.get(field), field)
        identity = (case["scenario_id"], case["id"])
        if identity in identities:
            raise ValueError(f"重复的 RAG 案例：{identity}")
        identities.add(identity)
        version = versions.setdefault(case["scenario_id"], case["corpus_version"])
        if version != case["corpus_version"]:
            raise ValueError(f"同场景案例不能混用不同语料版本：{case['scenario_id']}")
        gold = case.get("relevant_chunk_ids")
        if not isinstance(gold, list):
            raise TypeError(f"{case['id']} 的 relevant_chunk_ids 必须是列表。")
        score([], gold)
        evidence = case.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError(f"{case['id']} 缺少相关片段的原文证据。")
        annotated = set()
        for item in evidence:
            if not isinstance(item, dict):
                raise TypeError(f"{case['id']} 的证据必须是对象。")
            for field in ("chunk_id", "document_id", "content_hash", "quote"):
                _require_text(item.get(field), f"{case['id']}.evidence.{field}")
            annotated.add(item["chunk_id"])
        if annotated != set(gold):
            raise ValueError(f"{case['id']} 的相关片段集合与证据标注不一致。")


def _read_snapshot(index, cases: list[dict]) -> dict[str, dict]:
    """从同一只读事务获取语料版本及原文，不创建索引或加载向量模型。"""
    from backend.agent_harness.rag.index import INDEX_SIGNATURE

    expected = {case["scenario_id"]: case["corpus_version"] for case in cases}
    try:
        actual = index.snapshot(scenarios=list(expected))
    except (psycopg.Error, ValueError) as exc:
        raise CorpusMismatchError("知识索引无法读取；请检查 PostgreSQL 连接与索引结构。") from exc
    if actual["signature"] != INDEX_SIGNATURE:
        raise CorpusMismatchError("知识索引格式已改变；重建后请重新核验语料版本和标注。")
    versions = {row["scenario_id"]: row["version"] for row in actual["scenarios"]}
    for scenario, version in expected.items():
        if versions.get(scenario) != version:
            raise CorpusMismatchError(
                f"{scenario} 的 corpus_version 与题集不一致；请重新标注或使用对应语料，"
                "不能直接沿用旧分数。"
            )
    chunks = {}
    for chunk in actual["chunks"]:
        chunk_id, scenario = chunk.get("id"), chunk.get("scenario_id")
        if not chunk_id or scenario not in expected or chunk_id in chunks:
            raise CorpusMismatchError(f"索引内片段的身份或场景不一致：{chunk_id}")
        chunks[chunk_id] = chunk
    for case in cases:
        for evidence in case["evidence"]:
            chunk = chunks.get(evidence["chunk_id"])
            if not chunk or chunk.get("scenario_id") != case["scenario_id"]:
                raise CorpusMismatchError(f"{case['id']} 的相关片段不在当前场景索引中。")
            for field in ("document_id", "content_hash"):
                if chunk.get(field) != evidence[field]:
                    raise CorpusMismatchError(f"{case['id']} 的证据 {field} 与索引不一致。")
            if evidence["quote"] not in chunk.get("text", ""):
                raise CorpusMismatchError(f"{case['id']} 的证据引文不在当前片段原文中。")
    return chunks


def _matches_corpus(index, cases: list[dict]) -> bool:
    """仅检查索引身份；连接不可用直接失败，不能被当作缺失语料。"""
    from backend.agent_harness.rag.index import INDEX_SIGNATURE

    snapshot = index.snapshot(scenarios=sorted({case["scenario_id"] for case in cases}))
    if snapshot["signature"] != INDEX_SIGNATURE:
        return False
    versions = {row["scenario_id"]: row["version"] for row in snapshot["scenarios"]}
    return all(versions.get(case["scenario_id"]) == case["corpus_version"] for case in cases)


def _load_frozen_chunks(project_root: Path, cases: list[dict]) -> tuple[list[dict], list[str]]:
    """加载整个场景的冻结语料，不能只保留标准答案而减少检索干扰项。"""
    from backend.agent_harness.rag.index import _json

    expected = {case["scenario_id"]: case["corpus_version"] for case in cases}
    chunks, paths = [], []
    for scenario, version in sorted(expected.items()):
        if Path(scenario).name != scenario or scenario in {".", ".."}:
            raise CorpusMismatchError("题集的场景 ID 不能包含路径。")
        path = project_root / "evaluation" / "datasets" / "corpus" / f"{scenario}.jsonl"
        try:
            members = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        except (OSError, json.JSONDecodeError) as exc:
            raise CorpusMismatchError(
                f"无法读取冻结语料 {portable_path(path, project_root)}；请保留题集配套的完整快照。"
            ) from exc
        if not members or any(
            not isinstance(chunk, dict) or chunk.get("scenario_id") != scenario for chunk in members
        ):
            raise CorpusMismatchError(
                f"冻结语料为空或混入其他场景：{portable_path(path, project_root)}"
            )
        # 与 PolicyIndex.build 完全相同：规范化每条完整 payload，排序后计算哈希。
        actual = hashlib.sha256(
            "\n".join(sorted(_json(chunk) for chunk in members)).encode()
        ).hexdigest()
        if actual != version:
            raise CorpusMismatchError(
                f"{scenario} 的冻结语料 hash 与题集 corpus_version 不一致；请恢复配套快照或重新标注。"
            )
        chunks.extend(members)
        paths.append(portable_path(path, project_root))
    return chunks, paths


def _prepare_index(settings, cases: list[dict]):
    """复用同版本生产索引，否则在独立 namespace 中构建并复用冻结语料。"""
    from backend.agent_harness.rag.index import PolicyIndex

    production = PolicyIndex(settings.runtime_dir / "knowledge", database=settings.app_db)
    if _matches_corpus(production, cases):
        return (
            production,
            _read_snapshot(production, cases),
            {
                "source": "production_index",
                "identity": production.identity,
                "namespace": production.namespace,
                "reused": True,
            },
        )

    versions = sorted({(case["scenario_id"], case["corpus_version"]) for case in cases})
    namespace = "eval_" + hashlib.sha256(json.dumps(versions).encode()).hexdigest()[:24]
    isolated = PolicyIndex(
        settings.runtime_dir / "evaluation" / "knowledge",
        database=settings.app_db,
        namespace=namespace,
    )
    if _matches_corpus(isolated, cases):
        return (
            isolated,
            _read_snapshot(isolated, cases),
            {
                "source": "frozen_index_reused",
                "identity": isolated.identity,
                "namespace": isolated.namespace,
                "reused": True,
            },
        )

    chunks, paths = _load_frozen_chunks(settings.project_root, cases)
    # 沿用生产向量化和建库逻辑；首次运行可准备开源模型缓存，不访问政策网页或云模型。
    isolated.build(chunks)
    return (
        isolated,
        _read_snapshot(isolated, cases),
        {
            "source": "frozen_index_built",
            "identity": isolated.identity,
            "namespace": isolated.namespace,
            "reused": False,
            "snapshots": paths,
        },
    )


def _retrieved_ids(result: dict, case: dict, snapshot: dict[str, dict]) -> list[str]:
    if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
        raise TypeError("检索结果必须包含结构化 data。")
    data = result["data"]
    if data.get("corpus_version") != case["corpus_version"]:
        raise CorpusMismatchError("评测执行期间语料版本发生变化；本轮分数不可比较，请重新运行。")
    if data.get("scenario_id") != case["scenario_id"]:
        raise ValueError("检索结果的场景与当前题目不一致。")
    if result.get("status") not in {"success", "no_data"}:
        raise ValueError(f"检索器返回非预期状态：{result.get('status')}")
    clauses = data.get("clauses")
    if not isinstance(clauses, list) or len(clauses) > MAX_RESULTS:
        raise ValueError("默认检索必须返回至多八个原文片段。")
    if (result["status"] == "success") != bool(clauses):
        raise ValueError("检索状态与返回片段数量不一致。")
    ids = []
    for clause in clauses:
        if not isinstance(clause, dict):
            raise TypeError("检索片段必须是对象。")
        chunk_id = _require_text(clause.get("id"), "返回片段 ID")
        source = snapshot.get(chunk_id)
        if source is None:
            raise ValueError(f"检索器返回未收录于冻结语料的片段：{chunk_id}")
        for field in ("scenario_id", "document_id", "content_hash", "text"):
            if clause.get(field) != source.get(field):
                raise ValueError(f"检索器返回的片段 {chunk_id} 的 {field} 与冻结语料不一致。")
        ids.append(chunk_id)
    if result.get("evidence") != ids:
        raise ValueError("检索结果的 evidence 与实际返回片段顺序不一致。")
    return ids


def _summary(rows: list[dict], status: str) -> dict:
    total = aggregate(rows)
    scenarios = {}
    for scenario in sorted({row["scenario_id"] for row in rows}):
        scenarios[scenario] = aggregate([row for row in rows if row["scenario_id"] == scenario])
    if status not in {"completed", "completed_with_errors"}:
        total["metrics"] = None
        for group in scenarios.values():
            group["metrics"] = None
    return {
        "kind": "rag",
        "status": status,
        "cutoffs": list(CUTOFFS),
        "mrr_cutoff": MAX_RESULTS,
        "averaging": "macro_over_planned_cases",
        "error_policy": "执行异常保留计划分母、按零计分；语料不匹配则整轮不发布指标。",
        **total,
        "by_scenario": scenarios,
    }


def _error_record(error: Exception) -> dict:
    """数据库错误仅记录类型，避免连接配置和凭据进入可分享的评测报告。"""
    result = {"type": type(error).__name__}
    if not isinstance(error, psycopg.Error):
        result["message"] = redact_local_paths(str(error))
    return result


def run(settings, output: Path, cases: list[dict], *, execute: bool = False) -> dict:
    """默认只预览；执行时不调用云模型，也不修改生产索引。"""
    _validate_cases(cases)
    output = Path(output)
    rows = [
        {
            "id": case["id"],
            "scenario_id": case["scenario_id"],
            "question": case["question"],
            "corpus_version": case["corpus_version"],
            "relevant_chunk_ids": case["relevant_chunk_ids"],
            "status": "not_run",
            "metrics": None,
        }
        for case in cases
    ]
    report = {"created_at": datetime.now(UTC).isoformat(), "execute": execute, "cases": rows}

    def save(status: str) -> dict:
        summary = _summary(rows, status)
        if "index" in report:
            summary["index"] = report["index"]
        dump(output / "rag_results.json", report)
        dump(output / "rag_summary.json", summary)
        return summary

    if not execute:
        return save("preview")

    try:
        index, snapshot, report["index"] = _prepare_index(settings, cases)
    except Exception as exc:
        # 建库失败同样留存诊断，不生成评测分数；原始异常继续交给调用方处理。
        report["error"] = _error_record(exc)
        save("preflight_failed")
        raise

    for case, row in zip(cases, rows):
        started = time.monotonic()
        try:
            # 只调用一次默认检索；不同 k 的指标都截取同一份生产排序。
            result = index.search(case["question"], case["scenario_id"])
            row["retrieval"] = result
            retrieved = _retrieved_ids(result, case, snapshot)
            row["retrieved_chunk_ids"] = retrieved
            row["unique_retrieved_chunk_ids"] = list(dict.fromkeys(retrieved))
            row["metrics"] = score(retrieved, case["relevant_chunk_ids"])
            row["status"] = "scored"
        except CorpusMismatchError as exc:
            row["status"] = "error"
            row["error"] = _error_record(exc)
            row["elapsed_seconds"] = time.monotonic() - started
            report["error"] = row["error"]
            save("corpus_changed")
            raise
        except Exception as exc:  # noqa: BLE001 - 每题都保留异常，剩余案例仍继续运行。
            row["status"] = "error"
            row["error"] = _error_record(exc)
            row["metrics"] = score([], case["relevant_chunk_ids"])
        row["elapsed_seconds"] = time.monotonic() - started
        save("running")
    status = (
        "completed_with_errors" if any(row["status"] == "error" for row in rows) else "completed"
    )
    return save(status)
