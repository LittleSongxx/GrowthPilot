"""参数错误作为业务结果返回；实现错误仍应暴露为真正的失败。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from backend.agent_harness.tools import ToolContext, build_tools
from backend.api.app import create_app
from tests.integration.test_integration_security import create
from tests.integration.test_integration_security import stack as stack  # noqa: PLC0414 - 导出 pytest 测试夹具


@pytest.fixture
async def error_client(stack):
    host = stack[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_app(host.settings, host), raise_app_exceptions=False
        ),
        base_url="http://127.0.0.1:8787",
    ) as client:
        yield client


def policy_tool(reader):
    context = ToolContext(
        search_policy=reader,
        catalog=Mock(),
        authorize=lambda _name: None,
        emit=Mock(),
    )
    return build_tools(context, [{"id": "search_policy"}])["search_policy"]


@pytest.mark.parametrize("error", [ValueError("defect"), KeyError("defect"), TypeError("defect")])
async def test_tool_does_not_relabel_domain_bug_as_bad_model_arguments(error):
    search_policy = policy_tool(Mock(side_effect=error))
    with pytest.raises(type(error), match="defect"):
        await search_policy(query="报名条件")


async def test_bad_tool_arguments_return_denied_without_running_business_code():
    reader = Mock()
    response = await policy_tool(reader)(query=123)
    assert response["status"] == "denied"
    assert "invalid_arguments" in response["data"]["reason"]
    reader.assert_not_called()


@pytest.mark.parametrize("error", [ValueError("invalid snapshot"), KeyError("broken snapshot")])
async def test_state_does_not_hide_local_storage_errors(stack, error_client, monkeypatch, error):
    runtime, auth, _ = await create(stack)
    monkeypatch.setattr(runtime.service, "snapshot", Mock(side_effect=error))
    response = await error_client.get(f"/api/sessions/{runtime.id}/state", headers=auth)
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert runtime.service.session(runtime.ctx)["state"] == "active"


@pytest.mark.parametrize("error", [ValueError("invalid context"), KeyError("broken contract")])
async def test_session_creation_does_not_relabel_programming_errors(
    stack, error_client, monkeypatch, error
):
    monkeypatch.setattr(stack[0], "create", AsyncMock(side_effect=error))
    response = await error_client.post(
        "/api/sessions", json={"scenario_id": "didi_driver", "customer_id": "new-customer"}
    )
    assert response.status_code == 500
    assert response.text == "Internal Server Error"


async def test_unknown_scenario_and_invalid_customer_are_rejected_before_creation(
    stack, monkeypatch
):
    create_session = AsyncMock()
    monkeypatch.setattr(stack[0], "create", create_session)
    missing = await stack[1].post(
        "/api/sessions", json={"scenario_id": "missing", "customer_id": "customer"}
    )
    invalid = await stack[1].post(
        "/api/sessions", json={"scenario_id": "didi_driver", "customer_id": "../customer"}
    )
    assert missing.status_code == 404
    assert invalid.status_code == 422
    create_session.assert_not_called()


async def test_business_denial_is_returned_without_turning_into_server_error(stack):
    runtime, _, _ = await create(stack)
    response = await runtime.tools["search_policy"](query=" ")
    assert response["status"] == "denied"
    assert response["retryable"] is False
    assert runtime.service.snapshot(runtime.ctx)["memory"] == []


async def test_stop_releases_customer_even_when_model_cleanup_fails(stack):
    runtime, _, _ = await create(stack)
    runtime.model_client = SimpleNamespace(
        close=AsyncMock(side_effect=ValueError("cleanup failed"))
    )
    with pytest.raises(ValueError, match="cleanup failed"):
        await runtime.stop("user_stop")
    assert runtime.stop_done.is_set() and not runtime.closing
    assert runtime.model_client is None
    assert runtime.store.get(runtime.id)["status"] == "closed"
    replacement = await stack[0].create(runtime.ctx.scenario_id, runtime.ctx.customer_id)
    assert replacement.id != runtime.id


@pytest.mark.parametrize("location", ["北京", "长沙", "云南省楚雄州南华县"])
async def test_valid_locations_produce_business_results_without_state_change(stack, location):
    runtime, _, _ = await create(stack, "meituan_rider")
    before = runtime.service.snapshot(runtime.ctx)
    response = await runtime.tools["search_nearby_stations"](location=location)
    assert response["status"] == "success"
    assert response["data"]["coverage_level"] in {"city", "generic"}
    assert response["data"]["requested_location"] == location
    assert response["data"]["items"]
    assert "source_type" not in response["data"]
    assert "is_fallback" not in response["data"]
    assert runtime.service.snapshot(runtime.ctx) == before


@pytest.mark.parametrize(
    ("location", "expected_reason"),
    [("附近", "location_required"), ("长沙或者北京", "conflicting_locations")],
)
async def test_missing_or_conflicting_location_still_requires_clarification(
    stack, location, expected_reason
):
    runtime, _, _ = await create(stack, "meituan_rider")
    before = runtime.service.snapshot(runtime.ctx)
    response = await runtime.tools["search_nearby_stations"](location=location)
    assert response["status"] == "needs_clarification"
    assert response["data"]["reason"] == expected_reason
    assert response["data"]["items"] == []
    assert "source_type" not in response["data"]
    assert runtime.service.snapshot(runtime.ctx) == before


@pytest.mark.parametrize("delivery_type", ["", " ", 123])
async def test_fallback_does_not_accept_invalid_delivery_type(stack, delivery_type):
    runtime, _, _ = await create(stack, "meituan_rider")
    response = await runtime.tools["query_delivery_pay"](area="长沙", delivery_type=delivery_type)
    assert response["status"] == "denied"
    assert response["data"]["reason"] == "invalid_arguments:ValidationError"
