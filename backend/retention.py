"""管理当前 PostgreSQL 命名空间的会话追踪保留期，并提供只读预览。"""

from __future__ import annotations

import math
import time
from contextlib import nullcontext
from types import SimpleNamespace

from backend.agent_harness.runtime.service import result
from backend.agent_harness.runtime.snapshots import digest
from backend.database import connect
from backend.maintenance import clear_session_traces, table_exists


def _validate_clock(retention_days, now):
    if type(retention_days) is not int or not 1 <= retention_days <= 30:
        raise ValueError("Trace retention must be an integer between 1 and 30 days")
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("Invalid retention clock")


def maintain_retention(settings, *, execute=False, retention_days=30, now=None):
    """只访问已配置的数据库，不加载场景、不调用模型，也不扫描其他 schema。"""
    now = time.time() if now is None else now
    _validate_clock(retention_days, now)
    output = {
        "mode": "execute" if execute else "dry_run",
        "retention_days": retention_days,
        "scopes": [],
        "status": "no_data",
    }
    with connect(settings.app_db, read_only=not execute) as db:
        if not table_exists(db, "sessions"):
            return output
        scopes = db.execute(
            "SELECT DISTINCT workspace,scenario FROM sessions ORDER BY workspace,scenario"
        ).fetchall()
        for row in scopes:
            # 清理历史记录无需迁移场景或加载快照。
            service = SimpleNamespace(
                db=db,
                workspace_id=row["workspace"],
                scenario=SimpleNamespace(id=row["scenario"]),
                clock=lambda: now,
                _tx=db.transaction,
                _connection=lambda: nullcontext(db),
            )
            if not execute:
                output["scopes"].append(
                    {"status": "planned", "plan": plan_retention(service, retention_days, now)}
                )
                continue
            with db.transaction():
                plan = _plan_retention(service, retention_days, now, lock=True)
                ids = [item["session_id"] for item in plan["sessions"]]
                counts = clear_session_traces(db, ids, reason="retention_expired")
            output["scopes"].append(
                {
                    "status": "success",
                    "plan": plan,
                    "result": result(
                        data={"expired_session_ids": ids, "removed_trace_rows": counts}
                    ),
                }
            )
    output["status"] = "success"
    output["memory_preserved"] = True
    return output


def plan_retention(service, retention_days=30, now=None):
    """只读计划不长期占用连接；删除前会在事务内重新查询并锁定。"""
    with service._connection():
        return _plan_retention(service, retention_days, now)


def _plan_retention(service, retention_days=30, now=None, *, lock=False):
    now = service.clock() if now is None else now
    _validate_clock(retention_days, now)
    cutoff = now - retention_days * 86400
    if lock:
        # 与会话写入及运行清理保持“先表锁、后行锁”的顺序，避免后续 UPDATE 升级死锁。
        service.db.execute("LOCK TABLE sessions IN ROW EXCLUSIVE MODE")
    rows = service.db.execute(
        "SELECT id,run_id,ended FROM sessions WHERE workspace=%s AND scenario=%s "
        "AND state IN ('closed','expiring') AND ended IS NOT NULL AND ended<=%s ORDER BY id"
        + (" FOR UPDATE" if lock else ""),
        (service.workspace_id, service.scenario.id, cutoff),
    ).fetchall()
    plan = {
        "workspace_id": service.workspace_id,
        "scenario_id": service.scenario.id,
        "retention_days": retention_days,
        "now": now,
        "cutoff": cutoff,
        "sessions": [
            {"session_id": row["id"], "run_id": row["run_id"], "ended_at": row["ended"]}
            for row in rows
        ],
    }
    plan["plan_hash"] = digest(plan)
    return plan


def begin_retention(service, plan_hash, retention_days=30, now=None):
    """删除追踪记录前，先以行锁核对同一份清理计划；排除活跃会话。"""
    with service._tx():
        plan = _plan_retention(service, retention_days, now, lock=True)
        if plan["plan_hash"] != plan_hash:
            return result("conflict", {"reason": "Retention plan changed; inspect and replan"})
        ids = [item["session_id"] for item in plan["sessions"]]
        if ids:
            marks = ",".join("%s" for _ in ids)
            service.db.execute(
                f"UPDATE sessions SET state='expiring' WHERE workspace=%s AND scenario=%s AND id IN ({marks})",
                (service.workspace_id, service.scenario.id, *ids),
            )
    return result(data=plan)


def cleanup_retention(service, plan_hash, retention_days=30, now=None):
    with service._tx():
        plan = _plan_retention(service, retention_days, now, lock=True)
        if plan["plan_hash"] != plan_hash:
            return result("conflict", {"reason": "Retention plan changed; no traces removed"})
        ids = [item["session_id"] for item in plan["sessions"]]
        if ids:
            marks = ",".join("%s" for _ in ids)
            if service.db.execute(
                f"SELECT 1 FROM sessions WHERE id IN ({marks}) AND state!='expiring'", ids
            ).fetchone():
                return result("conflict", {"reason": "Retention sessions were not claimed"})
        counts = clear_session_traces(service.db, ids, reason="retention_expired")
    return result(
        data={
            "expired_session_ids": ids,
            "removed_trace_rows": counts,
            "retention_days": retention_days,
            "memory_preserved": True,
        }
    )
