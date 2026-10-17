"""显式启用后验证真实模型的下载短信决策，发送记录仍使用独立本地存储。"""

import json
import os
from dataclasses import replace

import pytest

from backend.agent_harness import BackendService, load_scenario
from backend.agent_harness.runtime.session_manager import SessionManager
from backend.settings import Settings
from tests.db import database_for


@pytest.mark.skipif(
    os.environ.get("GROWTH_RUN_LIVE") != "1",
    reason="下载短信模型冒烟需显式设置 GROWTH_RUN_LIVE=1",
)
@pytest.mark.parametrize(
    "scenario_id,message,expected_sends",
    [
        ("meituan_rider", "方便，我想下载骑手 App，给我发条下载短信吧。", 1),
        ("meituan_merchant", "方便，我想下载商家 App，给我发条下载短信吧。", 1),
        ("didi_driver", "方便，我想下载司机 App，给我发条下载短信吧。", 1),
        ("financial_consultation", "方便，我想下载度小满 App，给我发条下载短信吧。", 1),
        ("didi_driver", "今天不用了，不要发短信，先这样吧。", 0),
    ],
)
async def test_model_sends_requested_download_sms_and_respects_refusal(
    tmp_path, scenario_id, message, expected_sends
):
    settings = replace(
        Settings.from_env(), runtime_dir=tmp_path, app_db=database_for(tmp_path / "application")
    )
    scenario = load_scenario(
        settings.project_root / "backend/scenarios" / scenario_id / "scenario.yaml"
    )
    service = BackendService(settings.app_db, scenario)
    host = SessionManager(settings, {scenario_id: service})
    runtime = await host.create(scenario_id, "download-smoke")
    await host.profile_worker.close()
    try:
        reply = await runtime.message(message)
        calls = [
            event["data"]["name"]
            for event in host.store.events(runtime.id, limit=500)
            if event["type"] == "tool_start"
        ]
        records = [row for row in service.trace(runtime.ctx) if row["kind"] == "download_sms_sent"]
        print(json.dumps({"scenario": scenario_id, "reply": reply, "calls": calls}, ensure_ascii=False))
        assert reply and "模拟" not in reply and "simulated" not in reply
        assert len(records) == expected_sends
        assert calls.count("send_download_sms") == expected_sends
        # 短信刚发送不能挂断；确认收到或明确拒绝后才使用通话结束能力。
        assert runtime.end_after_playback is (expected_sends == 0)
        if expected_sends:
            reply = await runtime.message("好的，收到了。")
            assert reply and runtime.end_after_playback
        end_calls = [
            event["data"]
            for event in host.store.events(runtime.id, limit=1000)
            if event["type"] == "tool_start" and event["data"]["name"] == "end_call"
        ]
        assert len(end_calls) == 1
        assert reply == end_calls[0]["arguments"]["message"]
        assert service.session(runtime.ctx)["state"] == "active"
    finally:
        await host.close()


@pytest.mark.skipif(
    os.environ.get("GROWTH_RUN_LIVE") != "1",
    reason="自然收尾模型冒烟需显式设置 GROWTH_RUN_LIVE=1",
)
async def test_receipt_with_a_new_question_keeps_call_open(tmp_path):
    settings = replace(Settings.from_env(), app_db=database_for(tmp_path / "application"))
    scenario = load_scenario(settings.project_root / "backend/scenarios/meituan_rider/scenario.yaml")
    service = BackendService(settings.app_db, scenario)
    host = SessionManager(settings, {scenario.id: service})
    runtime = await host.create(scenario.id, "receipt-followup-smoke")
    await host.profile_worker.close()
    try:
        await runtime.message("方便，我想下载骑手 App，给我发条下载短信吧。")
        assert not runtime.end_after_playback
        reply = await runtime.message("收到了，不过我点开链接以后怎么下载安装？")
        assert reply and not runtime.end_after_playback
        assert not any(
            event["type"] == "tool_start" and event["data"]["name"] == "end_call"
            for event in host.store.events(runtime.id, limit=1000)
        )
        assert service.session(runtime.ctx)["state"] == "active"
    finally:
        await host.close()
