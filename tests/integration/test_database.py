"""验证 PostgreSQL 基础设施的隔离、事务与迁移约束。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import psycopg
import pytest
from psycopg import sql
from psycopg.pq import TransactionStatus

import backend.database as database_module
from backend.database import (
    DatabaseConfig,
    close_database,
    connect,
    initialize_database,
    table_exists,
    temporary_database,
)


@pytest.mark.parametrize(
    "schema", [None, "", "Growth", "pg_catalog", "information_schema", "growth;drop", "a" * 64]
)
def test_schema_rejects_unsafe_or_reserved_names(schema):
    with pytest.raises(ValueError):
        DatabaseConfig("postgresql://localhost/growth", schema)


def test_database_identity_and_repr_never_include_credentials(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:do-not-expose@localhost:55432/growthpilot")
    monkeypatch.setenv("DATABASE_SCHEMA", "course")
    config = DatabaseConfig.from_env()
    assert config.schema == "course"
    assert config.identity == "postgresql://localhost:55432/growthpilot?schema=course"
    assert "do-not-expose" not in f"{config!r} {config}"
    with pytest.raises(ValueError) as error:
        DatabaseConfig("postgresql://owner:do-not-expose@localhost:invalid-port/growth?unknown=bad")
    assert "do-not-expose" not in str(error.value)


def _migration_copy(tmp_path, monkeypatch):
    """测试只修改临时副本，不能改写项目的历史迁移文件。"""
    directory = tmp_path / "migrations"
    directory.mkdir()
    for original in database_module.MIGRATIONS_DIR.glob("*.sql"):
        (directory / original.name).write_bytes(original.read_bytes())
    monkeypatch.setattr(database_module, "MIGRATIONS_DIR", directory)
    return directory


def test_read_only_rejects_writes_and_returns_clean_connection(database_config):
    with connect(database_config, read_only=True) as connection:
        assert connection.autocommit
        assert connection.execute("SELECT 1 AS value").fetchone() == {"value": 1}
        assert connection.info.transaction_status == TransactionStatus.IDLE
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute("CREATE TABLE forbidden_write (value INTEGER)")
    with connect(database_config) as connection:
        assert connection.execute("SHOW default_transaction_read_only").fetchone() == {
            "default_transaction_read_only": "off"
        }
        connection.execute("CREATE TABLE allowed_write (value INTEGER)")
        assert table_exists(connection, "allowed_write")


def test_explicit_transaction_rolls_back_all_writes(database_config):
    with connect(database_config) as connection:
        connection.execute("CREATE TABLE transaction_probe (value INTEGER)")
        with pytest.raises(RuntimeError), connection.transaction():
            connection.execute("INSERT INTO transaction_probe VALUES (1)")
            raise RuntimeError("模拟业务写入中途失败")
        assert connection.execute("SELECT COUNT(*) AS count FROM transaction_probe").fetchone()[
            "count"
        ] == 0


def test_table_exists_does_not_fall_back_to_another_schema(database_config):
    missing = replace(database_config, schema=f"missing_{database_config.schema[-32:]}")
    try:
        with connect(missing) as connection:
            # 模拟目标 schema 不存在时，search_path 回退到另一个含有业务表的 schema。
            connection.execute(
                sql.SQL("SET search_path TO {}, {}").format(
                    sql.Identifier(missing.schema), sql.Identifier(database_config.schema)
                )
            )
            assert connection.execute("SELECT current_schema() AS schema").fetchone()[
                "schema"
            ] == database_config.schema
            assert not table_exists(connection, "sessions")
    finally:
        close_database(missing)


def test_changed_migration_is_rejected_without_touching_data(
    database_config, tmp_path, monkeypatch
):
    directory = _migration_copy(tmp_path, monkeypatch)
    original = directory / "001_business.sql"
    original.write_text(original.read_text() + "\n-- 模拟错误地修改历史迁移。\n")
    with pytest.raises(RuntimeError, match="已应用迁移被修改"):
        initialize_database(database_config)
    with connect(database_config, read_only=True) as connection:
        assert table_exists(connection, "sessions")
        assert connection.execute("SELECT COUNT(*) AS count FROM schema_migrations").fetchone()[
            "count"
        ] == 2


def test_failed_migration_rolls_back_ddl_and_version(database_config, tmp_path, monkeypatch):
    directory = _migration_copy(tmp_path, monkeypatch)
    (directory / "003_failure.sql").write_text(
        "CREATE TABLE failed_migration (value INTEGER); SELECT 1 / 0;", encoding="utf-8"
    )
    with pytest.raises(psycopg.errors.DivisionByZero):
        initialize_database(database_config)
    with connect(database_config, read_only=True) as connection:
        assert not table_exists(connection, "failed_migration")
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM schema_migrations WHERE version = 3"
        ).fetchone()["count"] == 0


def test_parallel_initialization_applies_each_migration_once(
    database_config, tmp_path, monkeypatch
):
    directory = _migration_copy(tmp_path, monkeypatch)
    (directory / "003_concurrent.sql").write_text(
        "CREATE TABLE concurrent_migration (value INTEGER);", encoding="utf-8"
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(initialize_database, [database_config] * 4))
    with connect(database_config, read_only=True) as connection:
        assert table_exists(connection, "concurrent_migration")
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM schema_migrations WHERE version = 3"
        ).fetchone()["count"] == 1


def test_temporary_database_cleans_schema_after_exception(database_config):
    with (
        pytest.raises(RuntimeError, match="模拟评测取消"),
        temporary_database(database_config, prefix="growth_cleanup") as isolated,
    ):
        with connect(isolated, read_only=True) as connection:
            assert table_exists(connection, "sessions")
        raise RuntimeError("模拟评测取消")
    with connect(database_config, read_only=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM pg_namespace WHERE nspname = %s", (isolated.schema,)
        ).fetchone()["count"] == 0
        assert table_exists(connection, "sessions")


def test_temporary_database_cleans_schema_when_initialization_fails(database_config, monkeypatch):
    created = []

    def failing_initialize(config):
        created.append(config)
        raise RuntimeError("模拟迁移失败")

    monkeypatch.setattr(database_module, "initialize_database", failing_initialize)
    with pytest.raises(RuntimeError, match="模拟迁移失败"), temporary_database(database_config):
        pytest.fail("迁移失败时不应该进入评测代码")
    with connect(database_config, read_only=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM pg_namespace WHERE nspname = %s", (created[0].schema,)
        ).fetchone()["count"] == 0
