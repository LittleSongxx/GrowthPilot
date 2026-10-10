"""验证会话持久化和客户记忆，并使用限定范围的本地证据。"""

import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from psycopg import IntegrityError

from backend.agent_harness import BackendService
from backend.agent_harness.runtime.snapshots import digest
from backend.agent_harness.scenario import Scenario
from backend.database import connect
from tests.db import database_for


def make_scenario(root, *, scenario_id="memory_example", semantic_version="1"):
    data = {
        "scenario": {"id": scenario_id, "version": "1"},
        "customer_fields": [
            {
                "id": "city",
                "type": "string",
                "semantic_version": semantic_version,
                "ttl_seconds": 3600,
            },
            {"id": "ready", "type": "boolean", "semantic_version": "1"},
            {"id": "age", "type": "integer", "minimum": 18, "maximum": 80},
        ],
        "policies": {"session_timeout_seconds": 300},
        "tools": [{"id": "read_memory"}, {"id": "write_memory"}],
        "skills": [],
    }
    resources = {"process/SKILL.md": "客户自愿下载注册，先解答其疑问。"}
    return Scenario(data, root, digest({"config": data, "resources": resources}), resources)


@pytest.fixture
def scenario(tmp_path):
    return make_scenario(tmp_path)


@pytest.fixture
def service(tmp_path, scenario):
    service = BackendService(database_for(tmp_path / "app"), scenario, clock=lambda: 1791676800.0)
    yield service
    service.close()


@pytest.fixture
def ctx(service):
    return service.create_session("customer_a")


def message(service, ctx, text="我在北京", **kwargs):
    return service.add_message(ctx, text, **kwargs)["data"]["message_id"]


def test_fresh_database_and_snapshot_only_contain_conversation_evidence(service, ctx):
    message(service, ctx, "我注册好了")
    service.add_message(ctx, "您说已经注册好了，接下来可以了解使用方法。", origin="agent")
    snapshot = service.snapshot(ctx)
    assert set(snapshot) == {"session", "messages", "memory", "audit"}
    assert len(snapshot["messages"]) == 2
    assert snapshot["audit"][0]["kind"] == "session_started"
    assert ctx.workspace_id == "growth"
    assert snapshot["audit"][0]["payload"]["purpose"] == "session"
    with service._connection():
        tables = {
            row["table_name"] for row in service.db.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=current_schema()"
            )
        }
    assert tables == {
        "schema_migrations", "snapshots", "sessions", "messages", "memory", "audit",
        "profile_jobs", "runtime_sessions", "runtime_events", "rag_index_meta",
        "rag_corpora", "rag_chunks", "rag_chunk_terms",
    }
    for removed in (
        "query_progress",
        "prepare_guidance",
        "deliver_guidance",
        "query_operation",
        "stop_contact",
        "ingest_event",
    ):
        assert not hasattr(service, removed)


def test_memory_restart_correction_and_customer_scenario_scope(service, ctx, tmp_path, scenario):
    mid = message(service, ctx)
    assert service.write_memory(ctx, "city", "北京", mid, "北京")["status"] == "success"
    service.end_session(ctx)
    other = BackendService(database_for(tmp_path / "app"), scenario, clock=service.clock)
    try:
        fresh = other.create_session("customer_a")
        assert other.read_memory(fresh)["data"]["fields"][0]["value"] == "北京"
        correction = message(other, fresh, "我现在在上海")
        assert other.write_memory(fresh, "city", "上海", correction, "上海")["status"] == "conflict"
        assert (
            other.write_memory(fresh, "city", "上海", correction, "上海", expected_version=1)[
                "status"
            ]
            == "success"
        )
        history = other.read_memory(fresh, include_history=True)["data"]["fields"]
        assert len(history) == 2 and history[0]["supersedes"] == history[1]["id"]
        assert (
            other.write_memory(fresh, "city", "北京", mid, "北京", expected_version=2)["status"]
            == "denied"
        )
        b = other.create_session("customer_b")
        assert other.read_memory(b)["status"] == "no_data"
        other.end_session(fresh)
        other.end_session(b)
    finally:
        other.close()
    second = BackendService(database_for(tmp_path / "app"), make_scenario(tmp_path, scenario_id="other"))
    try:
        assert second.read_memory(second.create_session("customer_a"))["status"] == "no_data"
    finally:
        second.close()


@pytest.mark.parametrize(
    "origin, final", [("agent", True), ("host_control", True), ("customer_transcript", False)]
)
def test_memory_requires_current_final_customer_evidence(service, ctx, origin, final):
    mid = message(service, ctx, origin=origin, final=final)
    assert service.write_memory(ctx, "city", "北京", mid, "北京")["status"] == "denied"
    other = service.create_session("customer_b")
    foreign = message(service, other)
    assert service.write_memory(ctx, "city", "北京", foreign, "北京")["status"] == "denied"


def test_memory_final_transcript_provenance_expiry_and_idempotency(service, ctx):
    mid = message(service, ctx, origin="customer_transcript")
    first = service.write_memory(ctx, "city", "北京", mid, "北京", operation_id="same")
    assert first["status"] == "success"
    assert first["data"]["fields"][0]["source_kind"] == "customer_transcript"
    assert (
        service.write_memory(ctx, "city", "北京", mid, "北京", operation_id="same")["status"]
        == "success"
    )
    assert (
        service.write_memory(ctx, "city", "上海", mid, "北京", operation_id="same")["status"]
        == "conflict"
    )
    now = service.clock()
    service.clock = lambda: now + 3601
    assert service.read_memory(ctx)["data"]["fields"][0]["status"] == "expired"


@pytest.mark.parametrize("status", ["unknown", "declined", "not_asked"])
def test_unknown_memory_never_invents_a_value(service, ctx, status):
    mid = message(service, ctx, "我不想说")
    assert (
        service.write_memory(ctx, "city", "北京", mid, "不想说", status=status)["status"]
        == "denied"
    )
    saved = service.write_memory(ctx, "city", None, mid, "不想说", status=status)
    assert saved["status"] == "success"
    assert saved["data"]["fields"][0]["value"] is None
    assert saved["data"]["fields"][0]["status"] == status


def test_memory_candidate_type_and_exact_quote_are_enforced(service, ctx):
    mid = message(service, ctx, "我今年50岁，在上海，资料是否准备好还不确定")
    assert service.write_memory(ctx, "city", "北京", mid, "上海")["status"] == "denied"
    assert service.write_memory(ctx, "age", "50", mid, "50岁")["status"] == "denied"
    assert service.write_memory(ctx, "age", 50, mid, "50岁")["status"] == "success"
    assert (
        service.write_memory(ctx, "ready", True, mid, "资料是否准备好还不确定")["status"]
        == "denied"
    )
    yes = message(service, ctx, "是的")
    assert service.write_memory(ctx, "ready", "true", yes, "是的")["status"] == "denied"
    assert service.write_memory(ctx, "ready", True, yes, "是的")["status"] == "success"
    assert (
        service.write_memory(ctx, "city", "上海", mid, "上海", expected_version=True)["status"]
        == "denied"
    )


def test_memory_parallel_corrections_have_single_winner(service, ctx, tmp_path, scenario):
    mid = message(service, ctx)
    service.write_memory(ctx, "city", "北京", mid, "北京")
    correction = message(service, ctx, "我现在在上海")
    second = BackendService(database_for(tmp_path / "app"), scenario, clock=service.clock)
    try:

        def write(current):
            return current.write_memory(
                ctx, "city", "上海", correction, "上海", expected_version=1
            )["status"]

        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(write, [service, second])) == ["conflict", "success"]
    finally:
        second.close()


def test_memory_and_its_audit_commit_in_one_transaction(service, ctx):
    mid = message(service, ctx)
    with service._connection():
        service.db.execute("""
            CREATE FUNCTION fail_memory_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'audit unavailable' USING ERRCODE = '23514';
            END;
            $$;
            CREATE TRIGGER fail_memory_audit BEFORE INSERT ON audit
            FOR EACH ROW WHEN (NEW.kind='memory_written')
            EXECUTE FUNCTION fail_memory_audit();
        """, prepare=False)
    with pytest.raises(IntegrityError, match="audit unavailable"):
        service.write_memory(ctx, "city", "北京", mid, "北京")
    assert service.read_memory(ctx)["status"] == "no_data"
    with service._connection():
        service.db.execute("DROP TRIGGER fail_memory_audit ON audit")
    assert service.write_memory(ctx, "city", "北京", mid, "北京")["status"] == "success"
    assert service.trace(ctx)[-1]["kind"] == "memory_written"


def test_customer_single_executor_and_host_recovery(service, ctx):
    with pytest.raises(PermissionError, match="active executor"):
        service.create_session(ctx.customer_id)
    assert service.recover_host_sessions()["data"]["recovered_sessions"] == [ctx.id]
    assert service.session(ctx)["reason"] == "host_restart"
    assert service.create_session(ctx.customer_id).id != ctx.id


def test_frozen_scenario_is_self_contained_and_read_only(service, ctx, tmp_path):
    message(service, ctx)
    service.end_session(ctx)
    frozen = BackendService.saved_scenario(service.db_path, service.scenario.snapshot_hash)
    assert frozen.resources == service.scenario.resources
    reader = BackendService(service.db_path, frozen, read_only=True)
    try:
        assert reader.snapshot(reader.get_session(ctx.id))["messages"][0]["text"] == "我在北京"
        with pytest.raises(PermissionError, match="Read-only"):
            reader.create_session("new")
    finally:
        reader.close()


def test_changed_field_semantics_hide_old_value_but_keep_history(service, ctx, tmp_path):
    mid = message(service, ctx)
    service.write_memory(ctx, "city", "北京", mid, "北京")
    service.end_session(ctx)
    current = BackendService(
        service.db_path, make_scenario(tmp_path, semantic_version="2"), clock=service.clock
    )
    try:
        fresh = current.create_session(ctx.customer_id)
        assert current.read_memory(fresh, "city", include_history=True)["data"]["fields"] == []
        correction = message(current, fresh, "现在是上海")
        conflict = current.write_memory(fresh, "city", "上海", correction, "上海")
        assert conflict["data"]["actual_version"] == 1
        saved = current.write_memory(fresh, "city", "上海", correction, "上海", expected_version=1)
        assert saved["data"]["fields"][0]["semantic_version"] == "2"
        with current._connection():
            assert current.db.execute("SELECT COUNT(*) FROM memory").fetchone()["count"] == 2
    finally:
        current.close()


def test_field_tool_mappings_restrict_permissions(tmp_path, scenario):
    data = copy.deepcopy(scenario.data)
    for tool in data["tools"]:
        tool["fields"] = []
    restricted = Scenario(
        data,
        scenario.root,
        digest({"config": data, "resources": scenario.resources}),
        scenario.resources,
    )
    service = BackendService(database_for(tmp_path / "restricted"), restricted)
    try:
        ctx = service.create_session("restricted")
        mid = message(service, ctx)
        assert service.write_memory(ctx, "city", "北京", mid, "北京")["status"] == "denied"
        assert service.read_memory(ctx, "city")["status"] == "denied"
        assert service.read_memory(ctx)["status"] == "no_data"
    finally:
        service.close()


def test_unrelated_tables_and_additional_session_columns_survive_initialization(tmp_path, scenario):
    config = database_for(tmp_path / "existing")
    with connect(config) as db:
        db.execute("""
            ALTER TABLE sessions ADD COLUMN legacy_label TEXT;
            ALTER TABLE sessions ADD COLUMN legacy_sequence INTEGER;
            CREATE TABLE legacy_records(id TEXT PRIMARY KEY,payload TEXT);
            INSERT INTO legacy_records VALUES('prior-record','original-payload');
        """, prepare=False)
    service = BackendService(config, scenario)
    try:
        ctx = service.create_session("current-customer")
        service.recover_host_sessions()
        assert service.session(ctx)["state"] == "closed"
        with service._connection():
            assert service.db.execute("SELECT * FROM legacy_records").fetchone() == {
                "id": "prior-record", "payload": "original-payload",
            }
            columns = {
                row["column_name"] for row in service.db.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name='sessions'"
                )
            }
        assert {"legacy_label", "legacy_sequence"} <= columns
        assert set(service.snapshot(ctx)) == {"session", "messages", "memory", "audit"}
    finally:
        service.close()


def test_parallel_session_creation_reserves_one_customer_executor(service, scenario):
    second = BackendService(service.db_path, scenario, clock=service.clock)
    barrier = Barrier(2)

    def create(current):
        barrier.wait(timeout=5)
        try:
            return current.create_session("concurrent-customer")
        except PermissionError:
            return None

    try:
        with ThreadPoolExecutor(2) as pool:
            created = list(pool.map(create, [service, second]))
        assert sum(ctx is not None for ctx in created) == 1
        with service._connection():
            assert service.db.execute(
                "SELECT COUNT(*) AS count FROM sessions "
                "WHERE customer=%s AND state='active'", ("concurrent-customer",),
            ).fetchone()["count"] == 1
    finally:
        second.close()
