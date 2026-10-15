"""验证下载短信的场景绑定、发送记录、重复调用和模型可见边界。"""

import json

import pytest

from backend.agent_harness import BackendService
from backend.agent_harness.agent import create_toolkit
from backend.agent_harness.prompts import system_prompt
from tests.integration.test_integration_security import create
from tests.integration.test_integration_security import stack as stack  # noqa: PLC0414 - 复用会话夹具


def sent_records(runtime):
    return [
        row for row in runtime.service.trace(runtime.ctx) if row["kind"] == "download_sms_sent"
    ]


@pytest.mark.parametrize(
    "scenario_id,app_name",
    [
        ("meituan_rider", "美团骑手 App"),
        ("meituan_merchant", "美团外卖商家 App"),
        ("didi_driver", "滴滴司机 App"),
        ("financial_consultation", "度小满 App"),
    ],
)
async def test_sms_uses_bound_scenario_and_exposes_only_business_receipt(stack, scenario_id, app_name):
    runtime, _, _ = await create(stack, scenario_id)
    scenario = runtime.service.scenario
    toolkit = create_toolkit(runtime.tools, scenario)
    schemas = {
        item["function"]["name"]: item["function"] for item in await toolkit.get_tool_schemas()
    }
    schema = schemas["send_download_sms"]
    assert schema["parameters"]["properties"] == {}
    assert not schema["parameters"].get("required")
    native_tool = await toolkit.get_tool("send_download_sms")
    native_result = str(await native_tool())
    result = await runtime.tools["send_download_sms"]()
    assert result["status"] == "success"
    assert result["data"]["app_name"] == app_name
    assert result["data"]["delivery_status"] == "sent"
    assert result["evidence"] == [result["data"]["message_id"]]
    assert result["data"]["message_id"] in native_result
    assert set(result["data"]) == {"message_id", "app_name", "delivery_status"}
    records = sent_records(runtime)
    assert len(records) == 1
    internal = records[0]["payload"]
    assert internal["transport"] == "simulated"
    assert internal["message"]["app_name"] == app_name
    assert internal["message"]["download_url"] in internal["message"]["text"]
    visible = json.dumps(schema, ensure_ascii=False) + native_result
    visible += system_prompt(scenario, runtime.business_summary())
    for marker in ("simulated", "模拟", "download.example.com"):
        assert marker not in visible
    assert runtime.service.snapshot(runtime.ctx)["memory"] == []


async def test_sms_receipt_is_persistent_and_isolated_by_customer_and_session(stack):
    first, _, _ = await create(stack, customer_id="customer-a")
    second, _, _ = await create(stack, customer_id="customer-b")
    receipt = await first.tools["send_download_sms"]()
    assert sent_records(second) == []
    other_receipt = await second.tools["send_download_sms"]()
    assert receipt["data"]["message_id"] != other_receipt["data"]["message_id"]
    reopened = BackendService(first.service.db_path, first.service.scenario)
    try:
        assert reopened.send_download_sms(reopened.get_session(first.id)) == receipt
    finally:
        reopened.close()
    assert len(sent_records(first)) == 1
    await first.stop()
    following, _, _ = await create(stack, customer_id="customer-a")
    next_receipt = await following.tools["send_download_sms"]()
    assert next_receipt["data"]["message_id"] != receipt["data"]["message_id"]


@pytest.mark.parametrize(
    "arguments",
    [{"customer_id": "someone-else"}, {"download_url": "https://untrusted.example/link"}],
)
async def test_model_cannot_override_sms_recipient_or_download_entry(stack, arguments):
    runtime, _, _ = await create(stack)
    result = await runtime.tools["send_download_sms"](**arguments)
    assert result["status"] == "denied"
    assert sent_records(runtime) == []


async def test_closed_or_read_only_session_cannot_send_sms(stack):
    runtime, _, _ = await create(stack)
    await runtime.stop()
    assert (await runtime.tools["send_download_sms"]())["status"] == "denied"
    assert runtime.service.send_download_sms(runtime.ctx)["status"] == "denied"
    reader = BackendService(runtime.service.db_path, runtime.service.scenario, read_only=True)
    try:
        assert reader.send_download_sms(runtime.ctx)["status"] == "denied"
    finally:
        reader.close()
    assert sent_records(runtime) == []


async def test_sms_requires_scenario_permission(stack):
    runtime, _, _ = await create(stack)
    runtime.service.scenario.data["tools"] = [
        item for item in runtime.service.scenario["tools"] if item["id"] != "send_download_sms"
    ]
    assert (await runtime.tools["send_download_sms"]())["status"] == "denied"
    assert runtime.service.send_download_sms(runtime.ctx)["status"] == "denied"
    assert sent_records(runtime) == []


async def test_unconfigured_download_entry_does_not_report_sent(stack, monkeypatch):
    from backend.channels.sms import APP_DOWNLOADS

    runtime, _, _ = await create(stack)
    monkeypatch.delitem(APP_DOWNLOADS, runtime.ctx.scenario_id)
    result = await runtime.tools["send_download_sms"]()
    assert result["status"] == "no_data"
    assert sent_records(runtime) == []
