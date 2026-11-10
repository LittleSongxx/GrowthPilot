"""统一 PostgreSQL 连接池、版本迁移和测试数据库隔离。"""

from __future__ import annotations

import atexit
import hashlib
import os
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

DEFAULT_DATABASE_URL = "postgresql://growthpilot:growthpilot_local@127.0.0.1:55432/growthpilot"
MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_MIGRATION_NAME = re.compile(r"(\d{3,})_[a-z][a-z0-9_]*\.sql\Z")
# 同一数据库的所有 schema 共用锁，避免并行测试重复安装 pgvector 扩展。
_MIGRATION_LOCK = 0x47524F5754484442


@dataclass(frozen=True)
class DatabaseConfig:
    """数据库凭据不参与日志展示；schema 用于隔离运行和评测数据。"""

    dsn: str = field(repr=False)
    schema: str = "growthpilot"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema, str)
            or not _IDENTIFIER.fullmatch(self.schema)
            or self.schema.startswith("pg_")
        ):
            raise ValueError("DATABASE_SCHEMA 只能使用小写字母、数字和下划线，且不能以 pg_ 开头。")
        if self.schema == "information_schema":
            raise ValueError("DATABASE_SCHEMA 不能使用 PostgreSQL 系统 schema。")
        if not isinstance(self.dsn, str) or not self.dsn.strip():
            raise ValueError("DATABASE_URL 必须是有效的 PostgreSQL 连接配置。")
        try:
            conninfo_to_dict(self.dsn)
        except psycopg.ProgrammingError:
            # libpq 的解析错误可能包含原始输入，不能把连接密码带入错误信息。
            raise ValueError("DATABASE_URL 不是有效的 PostgreSQL 连接配置。") from None

    @classmethod
    def from_env(cls) -> DatabaseConfig:
        return cls(
            dsn=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
            schema=os.environ.get("DATABASE_SCHEMA", "growthpilot"),
        )

    @property
    def identity(self) -> str:
        """仅输出数据库定位信息，排除密码、连接参数和认证文件。"""
        values = conninfo_to_dict(self.dsn)
        host = quote(values.get("host", "local-socket"), safe=".:-,[]")
        port = quote(values.get("port", "5432"), safe=",")
        database = quote(values.get("dbname", "default"), safe="")
        return f"postgresql://{host}:{port}/{database}?schema={self.schema}"

    def __str__(self) -> str:
        return self.identity


_pools: dict[tuple[int, DatabaseConfig], ConnectionPool] = {}
_pools_lock = threading.Lock()


def _pool(config: DatabaseConfig) -> ConnectionPool:
    if not isinstance(config, DatabaseConfig):
        raise TypeError("数据库连接需要 DatabaseConfig 配置。")
    key = (os.getpid(), config)
    with _pools_lock:
        pool = _pools.get(key)
        if pool is None:

            def configure(connection: psycopg.Connection) -> None:
                connection.execute(
                    sql.SQL("SET search_path TO {}, public").format(sql.Identifier(config.schema))
                )
                connection.execute(
                    "SELECT set_config('growth.database_schema', %s, false)", (config.schema,)
                )

            pool = ConnectionPool(
                conninfo=config.dsn,
                kwargs={"autocommit": True, "row_factory": dict_row, "connect_timeout": 5},
                min_size=0,
                max_size=10,
                timeout=15,
                max_idle=120,
                num_workers=1,
                configure=configure,
                name=f"growth-{config.schema}",
                open=False,
            )
            pool.open()
            _pools[key] = pool
        return pool


@contextmanager
def connect(
    config: DatabaseConfig, *, read_only: bool = False
) -> Iterator[psycopg.Connection]:
    """借用连接；普通查询自动提交，多步写入必须显式使用 transaction()。"""
    try:
        with _pool(config).connection() as connection:
            if read_only:
                connection.execute("SET default_transaction_read_only TO on")
            try:
                yield connection
            finally:
                # 未正确关闭的显式事务不能泄漏给池中的下一位使用者。
                if not connection.closed:
                    if connection.info.transaction_status != TransactionStatus.IDLE:
                        connection.rollback()
                    if read_only:
                        connection.execute("SET default_transaction_read_only TO off")
    except PoolTimeout:
        raise psycopg.OperationalError(
            f"无法连接数据库 {config.identity}，请确认 PostgreSQL 已启动且连接配置正确。"
        ) from None


def close_database(config: DatabaseConfig) -> None:
    """清理一个配置对应的连接池，临时 schema 删除前先释放连接。"""
    with _pools_lock:
        pool = _pools.pop((os.getpid(), config), None)
    if pool is not None:
        pool.close()


def close_all_databases() -> None:
    """进程退出时关闭本进程创建的连接池。"""
    with _pools_lock:
        keys = [key for key in _pools if key[0] == os.getpid()]
        pools = [_pools.pop(key) for key in keys]
    for pool in pools:
        pool.close()


atexit.register(close_all_databases)


def initialize_database(config: DatabaseConfig) -> None:
    """在同一事务内校验历史版本并顺序执行未应用的 SQL 迁移。"""
    migrations = []
    versions = set()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"迁移文件命名不合法：{path.name}")
        version = int(match.group(1))
        if version in versions:
            raise ValueError(f"迁移版本重复：{version}")
        versions.add(version)
        content = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        migrations.append((version, path.name, checksum, content))
    if not migrations:
        raise RuntimeError("缺少数据库迁移文件，无法初始化数据库。")

    with connect(config) as connection, connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        connection.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(config.schema))
        )
        connection.execute(
            sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(config.schema))
        )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version BIGINT PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        applied = {
            row["version"]: row
            for row in connection.execute("SELECT version, name, checksum FROM schema_migrations")
        }
        if set(applied) - versions:
            raise RuntimeError("数据库含当前代码未知的迁移版本，请使用匹配的项目版本。")
        for version, name, checksum, content in sorted(migrations):
            previous = applied.get(version)
            if previous:
                if previous["name"] != name or previous["checksum"] != checksum:
                    raise RuntimeError(f"已应用迁移被修改：{name}；请新增迁移，不能改写历史迁移。")
                continue
            connection.execute(content, prepare=False)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                (version, name, checksum),
            )


def table_exists(connection: psycopg.Connection, name: str) -> bool:
    """只判断当前 schema 的表，不借用 public 中同名的业务表。"""
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError("表名只能使用小写字母、数字和下划线。")
    row = connection.execute(
        """SELECT EXISTS (
               SELECT 1 FROM information_schema.tables
               WHERE table_schema = current_setting('growth.database_schema') AND table_name = %s
           ) AS present""",
        (name,),
    ).fetchone()
    return bool(row["present"])


@contextmanager
def temporary_database(
    config: DatabaseConfig, prefix: str = "growthpilot_test"
) -> Iterator[DatabaseConfig]:
    """为一次测试或评测创建独立 schema，结束时仅删除本次实际创建的 schema。"""
    if (
        not isinstance(prefix, str)
        or not re.fullmatch(r"[a-z_][a-z0-9_]{0,29}", prefix)
        or prefix.startswith("pg_")
    ):
        raise ValueError("临时 schema 前缀应为最多 30 个小写字母、数字或下划线。")
    isolated = DatabaseConfig(config.dsn, f"{prefix}_{uuid.uuid4().hex}")
    created = False
    try:
        with connect(config) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(isolated.schema))
            )
            created = True
        initialize_database(isolated)
        yield isolated
    finally:
        close_database(isolated)
        if created:
            with connect(config) as connection:
                connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(isolated.schema))
                )
