"""只读记录运行时知识索引身份，不加载向量模型，也不创建或更新索引。"""

from __future__ import annotations

import psycopg


def knowledge_snapshot(runtime_dir, scenarios, *, database):
    """读取 PostgreSQL 元数据，区分语料尚未准备与数据库暂时不可连接。"""
    from backend.agent_harness.rag.index import (
        DIMENSIONS,
        INDEX_SIGNATURE,
        MODEL_NAME,
        PolicyIndex,
    )

    index = PolicyIndex(runtime_dir / "knowledge", database=database)
    snapshot = {
        "identity": index.identity,
        "namespace": index.namespace,
        "status": "no_data",
        "index_signature": None,
        "expected_index_signature": INDEX_SIGNATURE,
        "model": None,
        "dimensions": None,
        "scenarios": dict.fromkeys(sorted(set(scenarios))),
    }
    try:
        # 索引快照使用一个只读事务，避免语料重建时混入不同版本的元数据。
        actual = index.snapshot(scenarios=list(snapshot["scenarios"]))
        snapshot["index_signature"] = actual["signature"]
        for row in actual["scenarios"]:
            if row["scenario_id"] in snapshot["scenarios"]:
                snapshot["scenarios"][row["scenario_id"]] = {
                    key: row[key] for key in ("version", "indexed_at", "chunk_count")
                }
        if actual["status"] == "no_data":
            return snapshot
        if snapshot["index_signature"] != INDEX_SIGNATURE:
            snapshot["status"] = "rebuild_required"
        else:
            snapshot.update(model=MODEL_NAME, dimensions=DIMENSIONS)
            available = sum(row is not None for row in snapshot["scenarios"].values())
            snapshot["status"] = (
                "ready"
                if available == len(snapshot["scenarios"])
                else "partial"
                if available
                else "no_data"
            )
    except psycopg.OperationalError as error:
        snapshot.update(status="unavailable", error_category=type(error).__name__)
    except (psycopg.Error, OSError, ValueError) as error:
        # 数据库异常正文可能含连接信息，清单仅保留类别。
        snapshot.update(status="unreadable", error_category=type(error).__name__)
    return snapshot


def corpus_versions(snapshot):
    """未建立的场景显式记为 null，不能伪装成题集版本或当前源码版本。"""
    return {
        scenario: row["version"] if row is not None else None
        for scenario, row in snapshot["scenarios"].items()
    }
