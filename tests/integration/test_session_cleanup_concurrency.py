"""验证迟到的会话写入与清理共享锁协议，不会复活已过期会话或残留追踪。"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from psycopg.pq import TransactionStatus

from backend.agent_harness.runtime.service import BackendService
from backend.database import connect
from tests.agent_harness.test_session_memory import make_scenario


@pytest.fixture
def services(database_config, tmp_path):
    scenario = make_scenario(tmp_path)
    writer = BackendService(database_config, scenario, clock=lambda: 100.0)
    cleaner = BackendService(database_config, scenario, clock=lambda: 100.0)
    context = writer.create_session("cleanup_customer", run_id="cleanup_race")
    writer.add_message(context, "历史客户消息")
    writer.end_session(context)
    yield writer, cleaner, context
    writer.close()
    cleaner.close()


def _cleanup(service, method):
    if method == "reset":
        return service.reset_run("cleanup_race")
    plan = service.plan_retention(retention_days=1, now=86500.0)
    claim = service.begin_retention(plan["plan_hash"], retention_days=1, now=86500.0)
    assert claim["status"] == "success"
    return service.cleanup_retention(plan["plan_hash"], retention_days=1, now=86500.0)


def _late_write(service, context, operation):
    if operation == "end":
        return service.end_session(context)
    return service.add_message(context, "清理期间迟到的消息")


def _observe_transaction(service, monkeypatch):
    ready = threading.Event()
    pids = []
    original = service._tx

    @contextmanager
    def observed(*args, **kwargs):
        with original(*args, **kwargs):
            pids.append(service.db.info.backend_pid)
            ready.set()
            yield

    monkeypatch.setattr(service, "_tx", observed)
    return ready, pids


def _assert_waiting_for_lock(config, pid, future):
    # 观察数据库锁等待，避免用固定睡眠猜测另一线程是否进入关键区。
    deadline = time.monotonic() + 5
    with connect(config, read_only=True) as db:
        while time.monotonic() < deadline and not future.done():
            row = db.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (pid,)
            ).fetchone()
            if row and row["wait_event_type"] == "Lock":
                return
            time.sleep(0.01)
    pytest.fail("竞争操作没有等待会话锁，或在持锁方释放前提前完成")


def _assert_clean(config, context):
    with connect(config, read_only=True) as db:
        assert db.execute("SELECT state FROM sessions WHERE id=%s", (context.id,)).fetchone() == {
            "state": "expired"
        }
        for table in ("messages", "audit"):
            assert (
                db.execute(
                    f"SELECT COUNT(*) AS count FROM {table} WHERE session=%s", (context.id,)
                ).fetchone()["count"]
                == 0
            )


@pytest.mark.parametrize("operation", ["end", "message"])
@pytest.mark.parametrize("method", ["reset", "retention"])
def test_cleanup_waits_for_checked_session_write(services, operation, method, monkeypatch):
    writer, cleaner, context = services
    checked, resume = threading.Event(), threading.Event()
    original_check = writer._check

    def paused_check(*args, **kwargs):
        row = original_check(*args, **kwargs)
        # add_message 的外层只读检查不是写入关键区，第二次事务内检查才暂停。
        current = writer._bound_db.get()
        if current is not None and current.info.transaction_status == TransactionStatus.INTRANS:
            checked.set()
            if not resume.wait(10):
                raise TimeoutError("会话写入等待测试释放超时")
        return row

    monkeypatch.setattr(writer, "_check", paused_check)
    cleanup_ready, cleanup_pids = _observe_transaction(cleaner, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(_late_write, writer, context, operation)
        try:
            assert checked.wait(5)
            cleaning = pool.submit(_cleanup, cleaner, method)
            assert cleanup_ready.wait(5)
            _assert_waiting_for_lock(writer.db_path, cleanup_pids[0], cleaning)
        finally:
            resume.set()
        assert writing.result(timeout=5)["status"] == "success"
        assert cleaning.result(timeout=5)["status"] == "success"
    _assert_clean(writer.db_path, context)


@pytest.mark.parametrize("operation", ["end", "message"])
@pytest.mark.parametrize("method", ["reset", "retention"])
def test_late_write_rechecks_session_after_cleanup(services, operation, method, monkeypatch):
    writer, cleaner, context = services
    cleaning_started, resume = threading.Event(), threading.Event()
    module = "backend.maintenance" if method == "reset" else "backend.retention"
    from importlib import import_module

    target = import_module(module)
    original_clear = target.clear_session_traces

    def paused_clear(*args, **kwargs):
        cleaning_started.set()
        if not resume.wait(10):
            raise TimeoutError("会话清理等待测试释放超时")
        return original_clear(*args, **kwargs)

    monkeypatch.setattr(target, "clear_session_traces", paused_clear)
    write_ready, write_pids = _observe_transaction(writer, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        cleaning = pool.submit(_cleanup, cleaner, method)
        try:
            assert cleaning_started.wait(5)
            writing = pool.submit(_late_write, writer, context, operation)
            # retention 先把会话标成 expiring；add_message 的外层检查可直接拒绝。
            if not writing.done():
                if operation == "message" and method == "retention":
                    with pytest.raises(ValueError, match="expired"):
                        writing.result(timeout=5)
                else:
                    assert write_ready.wait(5)
                    _assert_waiting_for_lock(writer.db_path, write_pids[0], writing)
        finally:
            resume.set()
        assert cleaning.result(timeout=5)["status"] == "success"
        with pytest.raises(ValueError, match="expired"):
            writing.result(timeout=5)
    _assert_clean(writer.db_path, context)


def test_reset_waits_for_retention_claim_without_lock_upgrade_deadlock(services, monkeypatch):
    from backend import retention

    writer, cleaner, context = services
    plan = writer.plan_retention(retention_days=1, now=86500.0)
    planned, resume = threading.Event(), threading.Event()
    original_plan = retention._plan_retention

    def paused_plan(*args, **kwargs):
        result = original_plan(*args, **kwargs)
        if kwargs.get("lock"):
            planned.set()
            if not resume.wait(10):
                raise TimeoutError("保留期认领等待测试释放超时")
        return result

    monkeypatch.setattr(retention, "_plan_retention", paused_plan)
    reset_ready, reset_pids = _observe_transaction(cleaner, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        retaining = pool.submit(writer.begin_retention, plan["plan_hash"], 1, 86500.0)
        try:
            assert planned.wait(5)
            resetting = pool.submit(cleaner.reset_run, "cleanup_race")
            assert reset_ready.wait(5)
            _assert_waiting_for_lock(writer.db_path, reset_pids[0], resetting)
        finally:
            resume.set()
        assert retaining.result(timeout=5)["status"] == "success"
        # 保留期已认领的 expiring 会话不能被另一项运行清理抢走。
        assert resetting.result(timeout=5)["status"] == "conflict"
    assert writer.cleanup_retention(plan["plan_hash"], 1, 86500.0)["status"] == "success"
    _assert_clean(writer.db_path, context)


def test_retention_rechecks_plan_after_waiting_for_reset(services, monkeypatch):
    from backend import maintenance

    writer, cleaner, context = services
    plan = writer.plan_retention(retention_days=1, now=86500.0)
    resetting_started, resume = threading.Event(), threading.Event()
    original_clear = maintenance.clear_session_traces

    def paused_clear(*args, **kwargs):
        resetting_started.set()
        if not resume.wait(10):
            raise TimeoutError("运行清理等待测试释放超时")
        return original_clear(*args, **kwargs)

    monkeypatch.setattr(maintenance, "clear_session_traces", paused_clear)
    retention_ready, retention_pids = _observe_transaction(writer, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        resetting = pool.submit(cleaner.reset_run, "cleanup_race")
        try:
            assert resetting_started.wait(5)
            retaining = pool.submit(writer.begin_retention, plan["plan_hash"], 1, 86500.0)
            assert retention_ready.wait(5)
            _assert_waiting_for_lock(writer.db_path, retention_pids[0], retaining)
        finally:
            resume.set()
        assert resetting.result(timeout=5)["status"] == "success"
        assert retaining.result(timeout=5)["status"] == "conflict"
    _assert_clean(writer.db_path, context)
