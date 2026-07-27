"""持久化与宿主绑定的会话、客户记忆及其审计记录。

业务表由版本化迁移维护，数据库连接仅在一次读操作或事务期间占用。
下载短信在本地记录模拟发送，不连接外部业务系统。
"""

from __future__ import annotations

import json

import re

import time

import uuid

from contextlib import contextmanager

from contextvars import ContextVar

from dataclasses import dataclass

from pathlib import Path

from psycopg import IntegrityError

from psycopg.pq import TransactionStatus

from backend.channels.sms import download_message

from backend.database import DatabaseConfig, connect, initialize_database

from backend.paths import PROJECT_PREFIX, portable_path, resolve_project_path

from ..memory import MemoryStore

from ..memory.profiles import ProfileStore

from ..scenario import Scenario

from .snapshots import canonical, digest

def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex

def result(status="success", data=None, evidence=None, retryable=False):
    return {
        "status": status,
        "data": {} if data is None else data,
        "evidence": evidence or [],
        "retryable": retryable,
    }

def _snapshot_root(root: Path) -> str:
    """快照只保存项目内相对位置；外部场景的历史回放完全使用冻结资源。"""
    try:
        return root.resolve().relative_to(resolve_project_path(".")).as_posix()
    except ValueError:
        return "."

def _restore_snapshot_root(value: str) -> Path:
    """兼容旧绝对目录并按当前项目重定位，不修改快照正文及其内容哈希。"""
    reference = value.replace("\\", "/")
    if Path(reference).is_absolute() or re.match(r"^[A-Za-z]:/", reference):
        reference = portable_path(reference)
        if not (reference == PROJECT_PREFIX or reference.startswith(PROJECT_PREFIX + "/")):
            reference = "."
    return resolve_project_path(reference)

@dataclass(frozen=True)
class SessionContext:
    session_id: str

    workspace_id: str

    customer_id: str

    scenario_id: str

    snapshot_hash: str

    channel: str

    run_id: str

    @property
    def id(self):
        return self.session_id

    def as_dict(self):
        return dict(self.__dict__)


class BackendService:
    def __init__(
        self, db_path: DatabaseConfig, scenario, workspace_id="growth", clock=None, read_only=False
    ):
        self.scenario = scenario
        self.workspace_id = workspace_id
        self.clock = clock or time.time
        self.db_path = db_path
        self.read_only = read_only
        self._bound_db = ContextVar(f"growth_service_connection_{id(self)}", default=None)
        self._closed = False
        self.memory = MemoryStore(self, scenario, lambda: self.clock())
        self.profiles = ProfileStore(self.memory)
        if not read_only:
            initialize_database(db_path)
            with self._tx():
                self.db.execute(
                    "INSERT INTO snapshots(hash,scenario,version,root,config,resources,created) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (hash) DO NOTHING",
                    (
                        scenario.snapshot_hash,
                        scenario.id,
                        scenario.version,
                        _snapshot_root(scenario.root),
                        canonical(scenario.data),
                        canonical(scenario.resources),
                        self.clock(),
                    ),
                )

    @property
    def db(self):
        """只在显式连接作用域内暴露连接，避免会话长期占用连接池。"""
        db = self._bound_db.get()
        if db is None:
            raise RuntimeError("Database access requires a connection or transaction scope")
        return db

    @contextmanager
    def _connection(self):
        if self._closed:
            raise RuntimeError("Application service is closed")
        current = self._bound_db.get()
        if current is not None:
            yield current
            return
        with connect(self.db_path, read_only=self.read_only) as db:
            token = self._bound_db.set(db)
            try:
                yield db
            finally:
                self._bound_db.reset(token)

    def close(self):
        # 连接已随各操作归还连接池；关闭服务只阻止后续访问。
        self._closed = True

    @staticmethod
    def saved_scenario(db_path: DatabaseConfig, snapshot_hash):
        """读取已冻结的配置与资源包，不读取后来发生变更的源文件。"""
        with connect(db_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT root,config,resources FROM snapshots WHERE hash=%s", (snapshot_hash,)
            ).fetchone()
        if row is None:
            raise KeyError("Unknown frozen scenario")
        data, resources = json.loads(row["config"]), json.loads(row["resources"])
        if digest({"config": data, "resources": resources}) != snapshot_hash:
            raise ValueError("Frozen scenario content hash mismatch")
        return Scenario(data, _restore_snapshot_root(row["root"]), snapshot_hash, resources)

    @contextmanager
    def _tx(self, context=None):
        if self.read_only:
            raise PermissionError("Read-only application service cannot mutate records")
        with self._connection() as db, db.transaction():
            if isinstance(context, SessionContext):
                # 仅串行同一客户的写入，保护跨会话画像版本及短信幂等，不阻塞其他客户。
                db.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (canonical([context.workspace_id, context.customer_id]),),
                )
            yield

    def _scope(self, c):
        return (c.workspace_id, c.customer_id, c.scenario_id)

    def _check(self, c, active=False):
        with self._connection():
            if (
                not isinstance(c, SessionContext)
                or c.workspace_id != self.workspace_id
                or c.scenario_id != self.scenario.id
                or c.snapshot_hash != self.scenario.snapshot_hash
            ):
                raise ValueError("Invalid bound session context")
            lock = (
                not self.read_only and self.db.info.transaction_status == TransactionStatus.INTRANS
            )
            if lock:
                # 与清理入口统一使用“表锁 → 会话行锁 → 画像任务锁”的顺序。
                # 先取得写表锁，避免清理拿到表锁后等待本行，而本事务又等表锁升级。
                self.db.execute("LOCK TABLE sessions IN ROW EXCLUSIVE MODE")
            row = self.db.execute(
                "SELECT * FROM sessions WHERE id=%s AND workspace=%s AND customer=%s AND scenario=%s AND snapshot=%s"
                + (" FOR UPDATE" if lock else ""),
                (c.id, *self._scope(c), c.snapshot_hash),
            ).fetchone()
            if (
                not row
                or row["state"] in ("expiring", "expired")
                or (active and row["state"] != "active")
            ):
                raise ValueError("Session is missing, expired or no longer active")
            return row

    def _audit(self, c, kind, payload):
        self.db.execute(
            "INSERT INTO audit(workspace,customer,scenario,session,kind,payload,created) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (*self._scope(c), c.id, kind, canonical(payload), self.clock()),
        )

    def _denied(self, reason):
        return result("denied", {"reason": reason})

    def _active(self, c, tool=None):
        try:
            session = self._check(c, active=True)
        except ValueError as exc:
            return str(exc)
        if self.clock() - session["started"] > self.scenario["policies"]["session_timeout_seconds"]:
            return "Session time budget exceeded"
        tools = {item["id"]: item for item in self.scenario["tools"]}
        if tool and tool not in tools:
            return "Tool is not available in this scenario"
        return None

    def create_session(
        self,
        customer_id,
        channel="text",
        purpose="session",
        session_id=None,
        run_id=None,
    ):
        if not isinstance(customer_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,100}", customer_id
        ):
            raise ValueError("Valid customer_id required")
        if channel not in ("text", "voice"):
            raise ValueError("Unknown channel")
        c = SessionContext(
            session_id or uid("session"),
            self.workspace_id,
            customer_id,
            self.scenario.id,
            self.scenario.snapshot_hash,
            channel,
            run_id or uid("run"),
        )
        with self._tx(c):
            try:
                self.db.execute(
                    "INSERT INTO sessions(id,workspace,customer,scenario,snapshot,channel,run_id,state,started,ended,reason,purpose) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        c.id,
                        *self._scope(c),
                        c.snapshot_hash,
                        channel,
                        c.run_id,
                        "active",
                        self.clock(),
                        None,
                        None,
                        purpose,
                    ),
                )
            except IntegrityError as exc:
                raise PermissionError(
                    "Customer already has an active executor or session ID exists"
                ) from exc
            self._audit(
                c,
                "session_started",
                {"channel": channel, "snapshot_hash": c.snapshot_hash, "purpose": purpose},
            )
            self.profiles.begin(c)
        return c


