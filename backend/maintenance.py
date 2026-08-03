"""在配置的 PostgreSQL 命名空间内清理会话，保留客户记忆和无关记录。"""

from __future__ import annotations

import re

from backend.agent_harness.runtime.service import result
from backend.database import connect, table_exists


def protected_messages(db):
    """保留客户记忆的原始证据。"""
    if not table_exists(db, "memory"):
        return ""
    return " AND id NOT IN (SELECT message_id FROM memory WHERE message_id IS NOT NULL)"


def clear_session_traces(db, ids, *, reason):
    """清理已冻结的非活跃会话集合，不修改无关表。"""
    if not ids:
        return {}
    marks = ",".join("%s" for _ in ids)
    counts = {}
    if table_exists(db, "profile_jobs"):
        # 清理显式撤销尚未完成的提取；运行中的结果不能再写回已清理会话。
        db.execute(
            f"UPDATE profile_jobs SET status=CASE WHEN status IN "
            f"('collecting','pending','running','retrying') THEN 'cancelled' ELSE status END,"
            f"baseline_json='{{}}' WHERE session_id IN ({marks})", ids,
        )
    for table, column in (
        ("messages", "session"),
        ("audit", "session"),
        ("runtime_events", "session_id"),
    ):
        if not table_exists(db, table):
            continue
        suffix = protected_messages(db) if table == "messages" else ""
        counts[table] = db.execute(
            f"DELETE FROM {table} WHERE {column} IN ({marks})" + suffix, ids
        ).rowcount
    if table_exists(db, "runtime_sessions"):
        db.execute(
            f"UPDATE runtime_sessions SET state_json=NULL,metadata_json='{{}}' WHERE session_id IN ({marks})",
            ids,
        )
    # 保留最少必要的来源信息，确保留存的客户记忆仍可追溯。
    db.execute(
        f"UPDATE sessions SET state='expired',reason=%s WHERE id IN ({marks})",
        (reason, *ids),
    )
    return counts


def _validate_run(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", run_id):
        raise ValueError("实验名称只能由字母、数字、下划线和短横线组成")


def _read_sessions(db, run_id, *, lock=False):
    if not table_exists(db, "sessions"):
        return []
    rows = db.execute(
        "SELECT id,workspace,scenario,run_id,state FROM sessions "
        "WHERE run_id=%s ORDER BY id" + (" FOR UPDATE" if lock else ""),
        (run_id,),
    ).fetchall()
    if any(row["state"] in {"active", "expiring"} for row in rows):
        raise ValueError("请先结束此实验的全部活动会话再清理")
    return rows


def reset_run(settings, run_id, *, execute=False):
    """默认只预览；仅清理配置 schema 内指定运行，评测临时 schema 由评测器释放。"""
    _validate_run(run_id)
    plan = {"database": str(settings.app_db), "session_ids": []}
    with connect(settings.app_db, read_only=True) as db:
        rows = _read_sessions(db, run_id)
        plan["session_ids"] = [row["id"] for row in rows]
    if execute and plan["session_ids"]:
        with connect(settings.app_db) as db, db.transaction():
            # 显式管理操作短暂阻止新建或改变会话，避免状态检查后又插入同运行的活动会话。
            db.execute("LOCK TABLE sessions IN SHARE ROW EXCLUSIVE MODE")
            rows = _read_sessions(db, run_id, lock=True)
            plan["session_ids"] = [row["id"] for row in rows]
            plan["removed_trace_rows"] = clear_session_traces(
                db, plan["session_ids"], reason="run_reset"
            )
    return {
        "run_id": run_id,
        "mode": "execute" if execute else "dry_run",
        "status": "success" if plan["session_ids"] else "no_data",
        "scope": "application_run",
        "application": [plan],
        "memory_preserved": True,
        "exported_reports_preserved": True,
    }


def reset_application_run(service, run_id):
    """服务管理仅在本地执行，不向模型开放为工具。"""
    try:
        _validate_run(run_id)
    except ValueError:
        return service._denied("Invalid run ID")
    with service._tx():
        # 清理期间不允许新会话穿过“此运行没有活动会话”的检查。
        service.db.execute("LOCK TABLE sessions IN SHARE ROW EXCLUSIVE MODE")
        rows = service.db.execute(
            "SELECT id,state FROM sessions WHERE workspace=%s AND scenario=%s AND run_id=%s "
            "ORDER BY id FOR UPDATE",
            (service.workspace_id, service.scenario.id, run_id),
        ).fetchall()
        if not rows:
            return result("no_data", {"run_id": run_id})
        if any(row["state"] in {"active", "expiring"} for row in rows):
            return result("conflict", {"reason": "End active sessions before resetting their run"})
        ids = [row["id"] for row in rows]
        counts = clear_session_traces(service.db, ids, reason="run_reset")
    return result(
        data={
            "run_id": run_id,
            "reset_session_ids": ids,
            "removed_trace_rows": counts,
            "memory_preserved": True,
        }
    )
