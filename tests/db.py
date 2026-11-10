"""为真实 PostgreSQL 回归测试提供独立 schema，并在用例结束后统一回收。"""

import atexit
import json
from contextlib import ExitStack
from pathlib import Path

from psycopg import sql

from backend.database import DatabaseConfig, connect, temporary_database

_databases: dict[Path, DatabaseConfig] = {}
_contexts = ExitStack()


def database_for(path: Path) -> DatabaseConfig:
    """用测试路径标识隔离数据库；同一用例中重复打开相同标识可验证持久化。"""
    key = Path(path).resolve()
    if key not in _databases:
        _databases[key] = _contexts.enter_context(
            temporary_database(DatabaseConfig.from_env(), prefix="growthpilot_test")
        )
    return _databases[key]


def cleanup_databases() -> None:
    """仅删除本进程为当前测试创建的 schema，不清理开发者的业务库。"""
    try:
        _contexts.close()
    finally:
        _databases.clear()


def snapshot_database(config: DatabaseConfig) -> dict[str, list[str]]:
    """读取当前 schema 的完整逻辑快照，验证预览和重放不会修改已存数据。"""
    with connect(config, read_only=True) as db:
        tables = db.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name",
            (config.schema,),
        ).fetchall()
        return {
            table["table_name"]: sorted(
                json.dumps(row["data"], ensure_ascii=False, sort_keys=True, default=str)
                for row in db.execute(
                    sql.SQL("SELECT row_to_json(record) AS data FROM {}.{} AS record").format(
                        sql.Identifier(config.schema), sql.Identifier(table["table_name"])
                    )
                ).fetchall()
            )
            for table in tables
        }


def schema_exists(config: DatabaseConfig) -> bool:
    """核实临时 schema 是否仍存在，不为已删除的 schema 重新建立专用连接池。"""
    with connect(DatabaseConfig(config.dsn, schema="public"), read_only=True) as db:
        return db.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s) AS present",
            (config.schema,),
        ).fetchone()["present"]


# 两个名字用于强调快照只比较数据内容，不依赖 PostgreSQL 的物理存储格式。
database_snapshot = snapshot_database


# 子进程直接调用测试夹具时也必须回收自己创建的 schema。
atexit.register(cleanup_databases)
