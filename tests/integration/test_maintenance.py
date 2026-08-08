"""清理保持显式执行，仅限指定运行，并保护客户记忆和无关数据。"""

import uuid
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from psycopg import sql

from backend.database import DatabaseConfig, close_database, connect
from backend.maintenance import reset_run
from backend.retention import begin_retention, cleanup_retention, maintain_retention, plan_retention
from backend.settings import Settings
from tests.db import database_for, snapshot_database


def seed_database(config, *, active=False):
    """写入正式迁移表结构，使清理测试覆盖真实约束和字段。"""
    with connect(config) as db, db.transaction():
        db.execute("""
            INSERT INTO sessions
                (id,workspace,customer,scenario,snapshot,channel,run_id,state,started,ended,reason)
            VALUES
                ('one','growth','customer-one','current_scenario','snapshot','text','target','closed',0,100,'normal'),
                ('two','growth','customer-two','current_scenario','snapshot','text','other','closed',0,100,'normal');
            INSERT INTO messages
                (id,workspace,customer,scenario,session,origin,text,final,created)
            VALUES
                ('plain','growth','customer-one','current_scenario','one','customer','ordinary conversation',1,1),
                ('memory-source','growth','customer-one','current_scenario','one','customer','customer preference',1,2),
                ('other-message','growth','customer-two','current_scenario','two','customer','unrelated run',1,3);
            INSERT INTO memory
                (id,workspace,customer,scenario,field,semantic_version,version,value,status,
                 source_kind,message_id,session,quote,quote_start,recorded,expires,operation_id,request_hash)
            VALUES
                ('memory','growth','customer-one','current_scenario','preference','1',1,'"preference"',
                 'active','customer_message','memory-source','one','customer preference',0,2,9999999,'operation','hash');
            CREATE TABLE unrelated_records(id TEXT PRIMARY KEY,result TEXT);
            INSERT INTO unrelated_records VALUES('unrelated-record','preserve receipt');
            INSERT INTO audit (workspace,customer,scenario,session,kind,payload,created)
            VALUES ('growth','customer-one','current_scenario','one','test','{}',1);
            INSERT INTO runtime_events (session_id,type,at,data_json)
            VALUES ('one','test',1,'{}');
            INSERT INTO runtime_sessions
                (session_id,scenario_id,snapshot_hash,channel,status,started_at,state_json,metadata_json)
            VALUES ('one','current_scenario','snapshot','text','closed',0,'{"history":[]}','{"source":"test"}');
        """)
        if active:
            db.execute("UPDATE sessions SET state='active',ended=NULL WHERE id='one'")


def settings_for(tmp_path):
    return Settings(tmp_path, tmp_path / ".run", database_for(tmp_path / "application"))


def test_reset_is_read_only_by_default_then_clears_only_target_run(tmp_path):
    settings = settings_for(tmp_path)
    seed_database(settings.app_db)
    settings.runtime_dir.mkdir(parents=True)
    unrelated = settings.runtime_dir / "accounting.data"
    unrelated.write_bytes(b"unrelated customer data")
    before = snapshot_database(settings.app_db)
    preview = reset_run(settings, "target")
    assert preview["mode"] == "dry_run"
    assert preview["application"][0]["session_ids"] == ["one"]
    assert snapshot_database(settings.app_db) == before
    completed = reset_run(settings, "target", execute=True)
    assert completed["memory_preserved"]
    assert unrelated.read_bytes() == b"unrelated customer data"
    with connect(settings.app_db, read_only=True) as db:
        assert db.execute("SELECT id FROM messages ORDER BY id").fetchall() == [
            {"id": "memory-source"},
            {"id": "other-message"},
        ]
        assert db.execute("SELECT id,state FROM sessions ORDER BY id").fetchall() == [
            {"id": "one", "state": "expired"},
            {"id": "two", "state": "closed"},
        ]
        assert (
            db.execute("SELECT COUNT(*) AS count FROM unrelated_records").fetchone()["count"] == 1
        )
        assert db.execute("SELECT state_json,metadata_json FROM runtime_sessions").fetchone() == {
            "state_json": None,
            "metadata_json": "{}",
        }
        assert db.execute("SELECT COUNT(*) AS count FROM memory").fetchone()["count"] == 1


def test_active_session_preflight_prevents_partial_cleanup(tmp_path):
    settings = settings_for(tmp_path)
    seed_database(settings.app_db)
    with connect(settings.app_db) as db:
        db.execute("UPDATE sessions SET run_id='target',state='active',ended=NULL WHERE id='two'")
    before = snapshot_database(settings.app_db)
    with pytest.raises(ValueError, match="全部活动会话"):
        reset_run(settings, "target", execute=True)
    assert snapshot_database(settings.app_db) == before


def test_cleanup_preserves_reports_unrelated_files_and_active_other_runs(tmp_path):
    settings = settings_for(tmp_path)
    seed_database(settings.app_db)
    with connect(settings.app_db) as db:
        db.execute("UPDATE sessions SET state='active',ended=NULL WHERE id='two'")
    directory = settings.runtime_dir / "evaluations/target/trace"
    directory.mkdir(parents=True)
    recording = directory / "recording.wav"
    recording.write_bytes(b"audio recording")
    report = tmp_path / "evaluation/reports/target/traces.json"
    report.parent.mkdir(parents=True)
    report.write_text("[]")
    result = reset_run(settings, "target", execute=True)
    assert result["scope"] == "application_run"
    assert result["application"][0]["session_ids"] == ["one"]
    assert report.read_text() == "[]" and recording.read_bytes() == b"audio recording"
    with connect(settings.app_db, read_only=True) as db:
        assert (
            db.execute("SELECT state FROM sessions WHERE id='two'").fetchone()["state"] == "active"
        )
        assert (
            db.execute("SELECT text FROM messages WHERE session='two'").fetchone()["text"]
            == "unrelated run"
        )


def test_cleanup_rejects_path_traversal_and_never_follows_evaluation_symlinks(tmp_path):
    settings = settings_for(tmp_path)
    seed_database(settings.app_db)
    outside = tmp_path / "outside"
    outside.mkdir()
    preserved = outside / "recording.wav"
    preserved.write_bytes(b"other experiment")
    target = settings.runtime_dir / "evaluations/linked"
    target.parent.mkdir(parents=True)
    target.symlink_to(outside, target_is_directory=True)
    before = snapshot_database(settings.app_db)
    with pytest.raises(ValueError):
        reset_run(settings, "../outside", execute=True)
    result = reset_run(settings, "linked", execute=True)
    assert result["status"] == "no_data"
    assert preserved.read_bytes() == b"other experiment"
    assert snapshot_database(settings.app_db) == before


def test_retention_needs_no_service_or_scenario_and_preserves_memory(tmp_path):
    settings = settings_for(tmp_path)
    seed_database(settings.app_db)
    with connect(settings.app_db) as db:
        db.execute("UPDATE sessions SET state='active',ended=NULL WHERE id='two'")
    before = snapshot_database(settings.app_db)
    now = 40 * 86400
    preview = maintain_retention(settings, now=now)
    assert preview["mode"] == "dry_run"
    assert preview["scopes"][0]["plan"]["sessions"][0]["session_id"] == "one"
    assert snapshot_database(settings.app_db) == before
    result = maintain_retention(settings, execute=True, now=now)
    assert result["status"] == "success"
    with connect(settings.app_db, read_only=True) as db:
        assert (
            db.execute("SELECT state FROM sessions WHERE id='two'").fetchone()["state"] == "active"
        )
        assert db.execute("SELECT COUNT(*) AS count FROM memory").fetchone()["count"] == 1
        assert (
            db.execute("SELECT COUNT(*) AS count FROM messages WHERE id='plain'").fetchone()[
                "count"
            ]
            == 0
        )
    assert (
        maintain_retention(settings, execute=True, now=now)["scopes"][0]["plan"]["sessions"] == []
    )


def test_retention_requires_same_plan_and_local_claim(tmp_path):
    config = database_for(tmp_path / "application")
    seed_database(config)
    with connect(config) as db:
        service = SimpleNamespace(
            db=db,
            workspace_id="growth",
            scenario=SimpleNamespace(id="current_scenario"),
            clock=lambda: 40 * 86400,
            _tx=db.transaction,
            _connection=lambda: nullcontext(db),
        )
        plan = plan_retention(service)
        assert (
            cleanup_retention(service, plan["plan_hash"], now=plan["now"])["status"] == "conflict"
        )
        assert begin_retention(service, "different-plan", now=plan["now"])["status"] == "conflict"
        assert begin_retention(service, plan["plan_hash"], now=plan["now"])["status"] == "success"
        assert cleanup_retention(service, plan["plan_hash"], now=plan["now"])["status"] == "success"


def test_missing_schema_cleanup_never_creates_it(tmp_path):
    base = DatabaseConfig.from_env()
    config = DatabaseConfig(base.dsn, "growth_missing_" + uuid.uuid4().hex)
    settings = Settings(tmp_path, tmp_path / ".run", config)
    try:
        assert reset_run(settings, "missing", execute=True)["status"] == "no_data"
        assert maintain_retention(settings, execute=True)["status"] == "no_data"
        with connect(base, read_only=True) as db:
            assert (
                db.execute(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name=%s",
                    (config.schema,),
                ).fetchone()
                is None
            )
    finally:
        close_database(config)
        with connect(base) as db:
            db.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(config.schema))
            )
