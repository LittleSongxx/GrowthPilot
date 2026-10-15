"""使用真实 PostgreSQL 和 HTTP 路由验证会话及业务查询的边界。

这些测试不会调用服务供应商模型、语音服务或外部业务 API。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import Mock

import httpx
import pytest

from backend.agent_harness import BackendService, load_scenario
from backend.agent_harness.agent import create_toolkit
from backend.agent_harness.prompts import system_prompt
from backend.agent_harness.runtime.session_manager import SessionManager
from backend.api.app import create_app
from backend.settings import Settings
from tests.db import database_for
from tests.paths import ROOT


@pytest.fixture
async def stack(tmp_path):
    settings = Settings(
        project_root=ROOT,
        runtime_dir=tmp_path,
        app_db=database_for(tmp_path / "application"),
        session_secret="test-session-secret",
    )
    services = {}
    for path in (ROOT / "backend/scenarios").glob("*/scenario.yaml"):
        scenario = load_scenario(path)
        services[scenario.id] = BackendService(settings.app_db, scenario)
    host = SessionManager(settings, services)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, host)),
        base_url="http://127.0.0.1:8787",
    ) as ui:
        yield host, ui
    await host.close()


async def create(stack, sid="didi_driver", customer_id="test-security", run_id=None):
    host, ui = stack
    response = await ui.post(
        "/api/sessions",
        json={"scenario_id": sid, "customer_id": customer_id, "run_id": run_id},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    return host.get(data["session_id"]), {"Authorization": "Bearer " + data["token"]}, data


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("GET", "/state", None),
        ("GET", "/events", None),
        ("GET", "/voice/audio/nonexistent-reply", None),
        ("POST", "/stop", {}),
        ("POST", "/voice/start", None),
        ("POST", "/telemetry", {"event": "playback", "client_ms": 1}),
    ],
)
async def test_wrong_session_token_cannot_inspect_or_control_another_customer(
    stack, method, suffix, body
):
    first, _, _ = await create(stack, customer_id="customer-a")
    _, other_auth, _ = await create(stack, customer_id="customer-b")
    before = first.store.events(first.id)
    response = await stack[1].request(
        method, f"/api/sessions/{first.id}{suffix}", headers=other_auth, json=body
    )
    assert response.status_code == 401
    assert first.service.session(first.ctx)["state"] == "active"
    assert first.store.events(first.id) == before


async def test_state_requires_token_and_unknown_session_returns_not_found(stack):
    runtime, auth, _ = await create(stack)
    ui = stack[1]
    assert (await ui.get(f"/api/sessions/{runtime.id}/state")).status_code == 401
    assert (await ui.get(f"/api/sessions/{runtime.id}/state", headers=auth)).status_code == 200
    unknown_auth = {"Authorization": "Bearer " + stack[0].token("unknown-session")}
    response = await ui.get("/api/sessions/unknown-session/state", headers=unknown_auth)
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("http://outside.example/api/health", None),
        ("/api/health", "https://outside.example"),
        ("/api/health", "http://localhost:9999"),
    ],
)
async def test_http_rejects_nonlocal_host_or_origin(stack, url, origin):
    headers = {"Origin": origin} if origin else {}
    response = await stack[1].get(url, headers=headers)
    assert response.status_code == 403


async def test_one_active_conversation_per_customer_and_host_stop_releases_it(stack):
    runtime, auth, _ = await create(stack, customer_id="same-customer")
    denied = await stack[1].post(
        "/api/sessions",
        json={"scenario_id": "financial_consultation", "customer_id": "same-customer"},
    )
    assert denied.status_code == 409
    stopped = await stack[1].post(f"/api/sessions/{runtime.id}/stop", headers=auth, json={})
    assert stopped.status_code == 200
    assert stopped.json()["state"]["session"]["state"] == "closed"
    replacement, _, _ = await create(stack, "financial_consultation", "same-customer")
    assert replacement.id != runtime.id


async def test_foreign_transcript_cannot_be_reused_or_become_customer_memory(stack):
    first, _, _ = await create(stack, customer_id="first")
    second, second_auth, _ = await create(stack, customer_id="second")
    original = first.service.add_message(
        first.ctx, "我在北京", origin="customer_transcript", message_id="first-audio"
    )
    assert original["status"] == "success"
    duplicate = second.service.add_message(
        second.ctx, "我在上海", origin="customer_transcript", message_id="first-audio"
    )
    assert duplicate["status"] == "conflict"
    rejected = second.service.write_memory(
        second.ctx,
        field="city", value="北京", message_id="first-audio", quote="北京"
    )
    assert rejected["status"] == "denied"
    response = await stack[1].get(f"/api/sessions/{second.id}/state", headers=second_auth)
    assert response.json()["messages"] == []
    assert response.json()["memory"] == []
    assert first.service.message(first.ctx, "first-audio")["text"] == "我在北京"


async def test_current_customer_memory_isolated_from_other_customers(stack):
    first, first_auth, _ = await create(stack, customer_id="memory-first")
    second, second_auth, _ = await create(stack, customer_id="memory-second")
    source = first.service.add_message(first.ctx, "我在北京")
    saved = first.service.write_memory(
        first.ctx,
        field="city",
        value="北京",
        message_id=source["data"]["message_id"],
        quote="北京",
    )
    assert saved["status"] == "success"
    own = await stack[1].get(f"/api/sessions/{first.id}/state", headers=first_auth)
    other = await stack[1].get(f"/api/sessions/{second.id}/state", headers=second_auth)
    assert own.json()["memory"][0]["value"] == "北京"
    assert other.json()["memory"] == []
    assert second.service.read_memory(second.ctx)["data"]["fields"] == []


@pytest.mark.parametrize(
    "scenario_id", ["meituan_rider", "meituan_merchant", "didi_driver", "financial_consultation"]
)
async def test_model_boundary_exposes_only_declared_conversation_and_business_tools(
    stack, scenario_id
):
    runtime, _, data = await create(stack, scenario_id)
    toolkit = create_toolkit(runtime.tools, runtime.service.scenario)
    schemas = await toolkit.get_tool_schemas()
    names = {item["function"]["name"] for item in schemas}
    declared = {tool["id"] for tool in runtime.service.scenario["tools"]}
    assert names == declared | {"Skill", "end_call"}
    assert "search_policy" in names
    assert not {"read_memory", "write_memory"} & names
    assert not names & {
        "query_progress",
        "prepare_guidance",
        "deliver_guidance",
        "submit_action",
        "query_operation",
        "stop_contact",
        "end_session",
        "book_consultation",
        "schedule_callback",
        "Bash",
        "Read",
        "Write",
        "ingest_event",
    }
    exposed = "\n".join(
        [
            system_prompt(runtime.service.scenario, runtime.business_summary()),
            json.dumps(schemas),
            await toolkit.get_skill_instructions(),
        ]
    )
    assert runtime.settings.session_secret not in exposed
    assert data["token"] not in exposed
    assert "hidden_concerns" not in exposed and "failure_plan" not in exposed
    assert set(data["state"]) == {"session", "messages", "memory", "audit", "field_definitions", "profile_update"}


@pytest.mark.parametrize(
    ("scenario_id", "tool_name", "arguments"),
    [
        ("meituan_rider", "search_nearby_stations", {"location": "北京朝阳区望京"}),
        ("didi_driver", "search_driver_service_points", {"location": "北京海淀区中关村"}),
        (
            "meituan_merchant",
            "query_delivery_options",
            {"area": "北京朝阳区望京", "store_category": "餐饮"},
        ),
        ("financial_consultation", "query_product_options", {"purpose": "了解借款费用"}),
    ],
)
async def test_business_queries_keep_auditable_sources_outside_model_without_changing_customer_state(
    stack, monkeypatch, scenario_id, tool_name, arguments
):
    runtime, _, _ = await create(stack, scenario_id)
    catalog_time = datetime(2026, 10, 29, tzinfo=UTC).timestamp()
    monkeypatch.setattr(runtime.catalog, "clock", lambda: catalog_time)
    before = runtime.service.snapshot(runtime.ctx)
    response = await runtime.tools[tool_name](**arguments)
    assert response["status"] == "success"
    assert "source_type" not in response["data"]
    assert "disclosure" not in response["data"]
    assert response["data"]["items"] and response["evidence"]
    audit = [
        event["data"]["result"]
        for event in runtime.store.events(runtime.id)
        if event["type"] == "business_query_audit"
    ][-1]
    assert audit["data"]["source_type"] == "local_catalog"
    for item in response["data"]["items"]:
        assert "source_type" not in item and "source" not in item
        assert item["updated_at"] and item["valid_until"]
    assert all(item["source"] for item in audit["data"]["items"])
    assert runtime.service.snapshot(runtime.ctx) == before


async def test_undeclared_business_tool_is_unavailable_before_querying_catalog(stack, monkeypatch):
    runtime, _, _ = await create(stack, "didi_driver")
    catalog = Mock(side_effect=AssertionError("unauthorized catalog read"))
    monkeypatch.setattr(runtime, "catalog", catalog)
    assert "search_nearby_stations" not in runtime.tools
    with pytest.raises(KeyError):
        runtime.tools["search_nearby_stations"]
    catalog.assert_not_called()


async def test_stopping_conversation_cancels_generation_and_disables_tools(stack):
    runtime, auth, _ = await create(stack)
    started = asyncio.Event()

    async def generation():
        started.set()
        await asyncio.Event().wait()

    pending = asyncio.create_task(generation())
    runtime.task = pending
    await started.wait()
    response = await stack[1].post(f"/api/sessions/{runtime.id}/stop", headers=auth, json={})
    assert response.status_code == 200
    assert pending.cancelled()
    assert runtime.stop_done.is_set() and not runtime.closing
    assert (await runtime.tools["search_policy"](query="报名条件"))["status"] == "denied"
    assert runtime.service.snapshot(runtime.ctx)["messages"] == []


@pytest.mark.parametrize("body", [{"dnc": True}, {"reason": "user_stop", "operation_id": "x"}])
async def test_stop_has_no_business_or_contact_management_payload(stack, body):
    runtime, auth, _ = await create(stack)
    response = await stack[1].post(f"/api/sessions/{runtime.id}/stop", headers=auth, json=body)
    assert response.status_code == 422
    assert runtime.service.session(runtime.ctx)["state"] == "active"


async def test_static_server_rejects_encoded_path_escape(stack):
    for path in ("/%2e%2e/%2e%2e/.env", "/assets/%2e%2e/%2e%2e/%2e%2e/pyproject.toml"):
        response = await stack[1].get(path)
        assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    ("scenario_id", "tool_name", "arguments"),
    [
        ("meituan_rider", "query_delivery_pay", {"area": "长沙", "delivery_type": "乐跑"}),
        ("didi_driver", "query_driver_earnings", {"area": "长沙", "service_type": "商务专车"}),
        (
            "meituan_merchant",
            "query_delivery_options",
            {"area": "长沙", "store_category": "鲜花店"},
        ),
        ("financial_consultation", "query_product_options", {"purpose": "比较费用计息方式"}),
    ],
)
async def test_unlisted_valid_conditions_keep_assumptions_without_mutation(
    stack, scenario_id, tool_name, arguments
):
    runtime, _, _ = await create(stack, scenario_id)
    before = runtime.service.snapshot(runtime.ctx)
    response = await runtime.tools[tool_name](**arguments)
    assert response["status"] == "success"
    data = response["data"]
    assert "match_type" not in data and "is_fallback" not in data
    assert data["requested_filters"] and data["assumed_filters"]
    assert data["items"] and "template_provenance" not in data
    assert "source_type" not in data
    assert runtime.service.snapshot(runtime.ctx) == before


async def test_unknown_financial_product_keeps_requested_and_calculated_identity_separate(stack):
    runtime, _, _ = await create(stack, "financial_consultation")
    before = runtime.service.snapshot(runtime.ctx)
    requested_product = "customer_named_product_unsupported"
    response = await runtime.tools["estimate_financing_cost"](
        product_id=requested_product, amount=10000, term_months=3
    )
    assert response["status"] == "success"
    data = response["data"]
    assert "match_type" not in data and "is_fallback" not in data
    assert data["requested_product_id"] == requested_product
    assert data["effective_product_id"] == "catalog_generic_financing"
    assert data["calculation"]["product_id"] == data["effective_product_id"]
    assert data["calculation"]["requested_product_id"] == requested_product
    assert "source_type" not in data
    assert runtime.service.snapshot(runtime.ctx) == before
