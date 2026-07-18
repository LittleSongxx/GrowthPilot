"""统一管理 PostgreSQL 测试隔离；数据库不可用时保留明确的连接错误。"""

import pytest

from tests.db import cleanup_databases, database_for


@pytest.fixture(autouse=True)
def _cleanup_test_databases():
    """未使用数据库的测试不会建立连接，实际创建的 schema 均在测试后回收。"""
    yield
    cleanup_databases()


@pytest.fixture
def database_config(tmp_path):
    """为需要真实 SQL、事务和并发语义的用例创建独立 PostgreSQL schema。"""
    return database_for(tmp_path / "database")
