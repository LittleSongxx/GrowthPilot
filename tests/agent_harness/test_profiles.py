"""验证画像的异步更新、证据隔离、原子提交和跨通话顺序，不访问真实模型。"""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import AsyncMock

import pytest

from backend.agent_harness import BackendService, load_scenario
from backend.agent_harness.memory.worker import ProfileWorker
from backend.agent_harness.runtime.session_manager import SessionManager
from backend.settings import Settings
from tests.db import database_for
from tests.paths import ROOT


@pytest.fixture
def profile_stack(tmp_path):
    settings = Settings(ROOT, tmp_path, database_for(tmp_path / "app"))
    scenario = load_scenario(ROOT / "backend/scenarios/meituan_rider/scenario.yaml")
    service = BackendService(settings.app_db, scenario)
    worker = ProfileWorker(settings, AsyncMock(return_value={"updates": []}))
    yield settings, service, worker
    service.close()


def say(service, ctx, text, **kwargs):
    return service.add_message(ctx, text, **kwargs)["data"]["message_id"]


def update(field, value, mid, quote, **kwargs):
    return {
        "field": field,
        "value": value,
        "evidence": {"message_id": mid, "quote": quote},
        **kwargs,
    }


def prepare(service, text="我在上海", customer="customer"):
    ctx = service.create_session(customer)
    mid = say(service, ctx, text)
    service.end_session(ctx)
    return ctx, mid


async def test_profile_only_runs_after_stop_and_next_call_receives_it(profile_stack):
    settings, service, worker = profile_stack
    host = SessionManager(settings, {service.scenario.id: service})
    host.profile_worker = worker
    first = await host.create(service.scenario.id, "customer")
    await worker.close()
    source = say(service, first.ctx, "我在上海，资料已经全部准备好了，想做兼职")
    assert first.business_summary() == {"profile": {}}
    assert not await worker.run_once()
    worker.extractor.assert_not_awaited()
    assert not {"read_memory", "write_memory"} & first.tools.keys()
    worker.extractor.return_value = {
        "updates": [
            update("area", "上海", source, "我在上海"),
            update("materials_ready", True, source, "资料已经全部准备好了"),
            update("work_mode", "兼职", source, "想做兼职"),
        ]
    }
    await first.stop()
    await first.stop()
    assert service.profiles.state(first.ctx)["status"] == "pending"
    assert await worker.run_once()
    assert not await worker.run_once()
    worker.extractor.assert_awaited_once()
    second = await host.create(service.scenario.id, "customer")
    assert second.business_summary()["profile"] == {
        "area": {"value": "上海", "status": "known"},
        "materials_ready": {"value": True, "status": "known"},
        "work_mode": {"value": "兼职", "status": "known"},
    }
    assert len(service.read_memory(first.ctx, include_history=True)["data"]["fields"]) == 3
    await second.stop()
    await worker.close()


async def test_hangup_does_not_wait_for_extraction_and_cancellation_can_resume(profile_stack):
    settings, service, worker = profile_stack
    host = SessionManager(settings, {service.scenario.id: service})
    host.profile_worker = worker
    rt = await host.create(service.scenario.id, "customer")
    started = asyncio.Event()

    async def slow(_settings, _payload):
        started.set()
        await asyncio.Event().wait()

    worker.extractor = slow
    say(service, rt.ctx, "我在上海")
    await asyncio.wait_for(rt.stop(), timeout=0.5)
    assert service.session(rt.ctx)["state"] == "closed"
    await asyncio.wait_for(started.wait(), timeout=2)
    await worker.close()
    assert service.profiles.state(rt.ctx)["status"] == "pending"
    assert service.profiles.state(rt.ctx)["attempts"] == 0
    worker.extractor = AsyncMock(return_value={"updates": []})
    assert await worker.run_once()
    assert service.profiles.state(rt.ctx)["status"] == "completed"


async def test_payload_excludes_control_and_partial_transcripts_and_keeps_dialogue(profile_stack):
    _, service, worker = profile_stack
    ctx = service.create_session("customer")
    say(service, ctx, "请主动开场", origin="host_control")
    say(service, ctx, "资料都齐了吗？", origin="agent")
    mid = say(service, ctx, "是的", origin="customer_transcript")
    say(service, ctx, "暂不确定的中间转写", origin="customer_transcript", final=False)
    service.end_session(ctx)
    worker.extractor.return_value = {"updates": [update("materials_ready", True, mid, "是的")]}
    await worker.run_once()
    payload = worker.extractor.call_args.args[1]
    assert [m["text"] for m in payload["messages"]] == ["资料都齐了吗？", "是的"]
    assert {f["id"] for f in payload["fields"]} == service.scenario.fields.keys()
    assert service.read_memory(ctx)["data"]["fields"][0]["value"] is True


@pytest.mark.parametrize(
    "origin,final", [("agent", True), ("host_control", True), ("customer_transcript", False)]
)
async def test_without_final_customer_message_no_model_is_called(profile_stack, origin, final):
    _, service, worker = profile_stack
    ctx = service.create_session("customer")
    say(service, ctx, "我在上海", origin=origin, final=final)
    service.end_session(ctx)
    assert service.profiles.state(ctx)["status"] == "skipped"
    assert not await worker.run_once()
    worker.extractor.assert_not_awaited()


@pytest.mark.parametrize(
    "bad_kind", ["foreign", "agent", "quote", "field", "type", "status", "duplicate", "scope"]
)
async def test_invalid_patch_cannot_partially_update_profile(profile_stack, bad_kind):
    _, service, worker = profile_stack
    other = service.create_session("other")
    foreign = say(service, other, "我在北京")
    ctx = service.create_session("customer")
    mid = say(service, ctx, "我在上海，资料都齐了")
    assistant = say(service, ctx, "您可以考虑全职", origin="agent")
    service.end_session(ctx)
    first = update("area", "上海", mid, "上海")
    bad = {
        "foreign": update("concern", "北京", foreign, "北京"),
        "agent": update("work_mode", "全职", assistant, "全职"),
        "quote": update("work_mode", "全职", mid, "我想全职"),
        "field": update("invented", "上海", mid, "上海"),
        "type": update("materials_ready", "true", mid, "资料都齐了"),
        "status": update("materials_ready", True, mid, "资料都齐了", status="unknown"),
        "duplicate": first,
        "scope": update("materials_ready", True, mid, "资料都齐了", customer_id="other"),
    }[bad_kind]
    worker.extractor.return_value = {"updates": [first, bad]}
    await worker.run_once()
    assert service.profiles.state(ctx)["status"] == "retrying"
    assert service.read_memory(ctx)["data"]["fields"] == []
    assert not any(a["kind"] == "memory_written" for a in service.trace(ctx))


async def test_unmentioned_fields_survive_and_declined_removes_previous_value(profile_stack):
    _, service, worker = profile_stack
    _first, mid = prepare(service, "我在上海，想做兼职")
    worker.extractor.return_value = {
        "updates": [update("area", "上海", mid, "上海"), update("work_mode", "兼职", mid, "兼职")]
    }
    await worker.run_once()
    second, mid = prepare(service, "现在的工作意向不想说")
    worker.extractor.return_value = {
        "updates": [update("work_mode", None, mid, "不想说", status="declined")]
    }
    await worker.run_once()
    fields = {m["field"]: m for m in service.read_memory(second)["data"]["fields"]}
    assert fields["area"]["value"] == "上海" and fields["area"]["version"] == 1
    assert fields["work_mode"]["value"] is None and fields["work_mode"]["status"] == "declined"
    assert fields["work_mode"]["version"] == 2
    assert service.profiles.baseline(second)["work_mode"]["value"] == "兼职"


async def test_older_job_finishing_later_cannot_overwrite_new_customer_statement(profile_stack):
    _, service, worker = profile_stack
    first, earlier = prepare(service, "我在上海")
    second, later = prepare(service, "我现在在北京")
    with service._connection():
        service.db.execute(
            "UPDATE profile_jobs SET next_attempt=%s WHERE session_id=%s", (time.time() + 3600, first.id)
        )
    worker.extractor.return_value = {"updates": [update("area", "北京", later, "北京")]}
    await worker.run_once()
    with service._connection():
        service.db.execute("UPDATE profile_jobs SET next_attempt=0 WHERE session_id=%s", (first.id,))
    worker.extractor.return_value = {"updates": [update("area", "上海", earlier, "上海")]}
    await worker.run_once()
    assert service.read_memory(second)["data"]["fields"][0]["value"] == "北京"
    with service._connection():
        outcome = json.loads(
            service.db.execute(
                "SELECT result_json FROM profile_jobs WHERE session_id=%s", (first.id,)
            ).fetchone()["result_json"]
        )
    assert outcome == {"applied": [], "skipped_stale": ["area"]}


async def test_retry_is_bounded_and_does_not_block_other_customers(profile_stack):
    _, service, worker = profile_stack
    first, _ = prepare(service, customer="one")
    second, _ = prepare(service, customer="two")
    worker.extractor.side_effect = RuntimeError("供应商内部错误与客户资料")
    await worker.run_once()
    assert service.profiles.state(first)["error"] == "RuntimeError"
    worker.extractor.side_effect = None
    await worker.run_once()
    assert service.profiles.state(second)["status"] == "completed"
    worker.extractor.side_effect = ValueError("无效 JSON")
    for _ in range(2):
        with service._connection():
            service.db.execute("UPDATE profile_jobs SET next_attempt=0 WHERE session_id=%s", (first.id,))
        await worker.run_once()
    assert service.profiles.state(first) == {
        "status": "failed",
        "attempts": 3,
        "error": "ValueError",
    }
    assert not await worker.run_once()


async def test_host_recovery_and_expired_worker_lease_resume_jobs(profile_stack):
    _, service, worker = profile_stack
    ctx = service.create_session("customer")
    mid = say(service, ctx, "我在上海")
    service.recover_host_sessions()
    assert service.profiles.state(ctx)["status"] == "pending"
    worker.extractor.return_value = {"updates": [update("area", "上海", mid, "上海")]}
    with service._connection():
        service.db.execute("UPDATE profile_jobs SET status='running',attempts=1,lease_until=0")
    await worker.run_once()
    assert service.profiles.state(ctx)["status"] == "completed"
    assert service.profiles.state(ctx)["attempts"] == 2
    assert service.read_memory(ctx)["data"]["fields"][0]["value"] == "上海"


async def test_failed_transaction_retries_without_duplicate_versions(profile_stack):
    _, service, worker = profile_stack
    ctx, mid = prepare(service)
    worker.extractor.return_value = {"updates": [update("area", "上海", mid, "上海")]}
    with service._connection():
        service.db.execute("""
            CREATE FUNCTION fail_profile_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'audit unavailable' USING ERRCODE = '23514';
            END;
            $$;
            CREATE TRIGGER fail_profile_audit BEFORE INSERT ON audit
            FOR EACH ROW WHEN (NEW.kind='profile_updated')
            EXECUTE FUNCTION fail_profile_audit();
        """, prepare=False)
    await worker.run_once()
    assert service.read_memory(ctx)["status"] == "no_data"
    assert service.profiles.state(ctx)["status"] == "retrying"
    with service._connection():
        service.db.execute("DROP TRIGGER fail_profile_audit ON audit")
    with service._connection():
        service.db.execute("UPDATE profile_jobs SET next_attempt=0")
    await worker.run_once()
    assert service.read_memory(ctx)["data"]["fields"][0]["version"] == 1
    assert service.profiles.state(ctx)["status"] == "completed"


async def test_expiration_uses_customer_statement_time_and_is_not_injected(profile_stack):
    _, service, worker = profile_stack
    ctx, mid = prepare(service)
    source_time = time.time() - 31 * 86400
    with service._connection():
        service.db.execute("UPDATE messages SET created=%s WHERE id=%s", (source_time, mid))
    worker.extractor.return_value = {"updates": [update("area", "上海", mid, "上海")]}
    await worker.run_once()
    field = service.read_memory(ctx)["data"]["fields"][0]
    assert field["status"] == "expired"
    assert field["expires_at"] == source_time + 2592000
    fresh = service.create_session("customer")
    assert service.profiles.baseline(fresh) == {}


async def test_cleanup_cancels_inflight_writeback(profile_stack):
    _, service, worker = profile_stack
    ctx, mid = prepare(service)

    async def extracting(_settings, _payload):
        assert service.reset_run(ctx.run_id)["status"] == "success"
        return {"updates": [update("area", "上海", mid, "上海")]}

    worker.extractor = extracting
    await worker.run_once()
    assert service.profiles.state(ctx)["status"] == "cancelled"
    with service._connection():
        assert service.db.execute("SELECT COUNT(*) FROM memory").fetchone()["count"] == 0


async def test_provider_request_is_structured_without_business_tools(profile_stack, monkeypatch):
    import httpx
    from openai import AsyncOpenAI

    from backend.agent_harness.memory import extraction

    settings, _, _ = profile_stack
    from dataclasses import replace

    settings = replace(settings, agent_api_key="test-only")
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"updates":[]}'},
                    }
                ],
            },
        )

    monkeypatch.setattr(
        extraction,
        "AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        ),
    )
    assert await extraction.extract_profile(settings, {"messages": []}) == {"updates": []}
    assert len(requests) == 1
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert "tools" not in requests[0]


def test_parallel_workers_claim_each_profile_job_once(profile_stack):
    settings, service, first_worker = profile_stack
    first, _ = prepare(service, customer="first")
    second, _ = prepare(service, customer="second")
    second_worker = ProfileWorker(settings, AsyncMock(return_value={"updates": []}))
    barrier = Barrier(2)

    def claim(worker):
        barrier.wait(timeout=5)
        return worker.claim()

    with ThreadPoolExecutor(2) as pool:
        jobs = list(pool.map(claim, [first_worker, second_worker]))
    assert {job["session_id"] for job in jobs} == {first.id, second.id}
    assert [job["attempts"] for job in jobs] == [1, 1]
    assert first_worker.claim() is None
    assert second_worker.claim() is None


def test_worker_skips_job_locked_by_another_transaction(profile_stack):
    _, service, worker = profile_stack
    locked, _ = prepare(service, customer="locked")
    ready, _ = prepare(service, customer="ready")
    with ThreadPoolExecutor(1) as pool, service._tx():
        service.db.execute(
            "SELECT session_id FROM profile_jobs WHERE session_id=%s FOR UPDATE",
            (locked.id,),
        ).fetchone()
        job = pool.submit(worker.claim).result(timeout=5)
        assert job["session_id"] == ready.id
    assert worker.claim()["session_id"] == locked.id
