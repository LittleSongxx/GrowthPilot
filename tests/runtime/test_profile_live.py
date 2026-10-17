"""显式启用的四场景画像提取冒烟，使用合成对话和独立临时数据库。"""

import os
from dataclasses import replace

import pytest

from backend.agent_harness import BackendService, load_scenario
from backend.agent_harness.memory.worker import ProfileWorker
from backend.settings import Settings
from tests.db import database_for


@pytest.mark.skipif(
    os.environ.get("GROWTH_RUN_LIVE") != "1",
    reason="画像模型冒烟需要显式设置 GROWTH_RUN_LIVE=1",
)
@pytest.mark.parametrize(
    "scenario_id,messages,expected",
    [
        (
            "meituan_rider",
            [
                ("customer", "我在上海，想兼职，我的资料都备齐了。"),
                ("customer", "更正一下，我现在在杭州，准备全职做。我没有电动车。"),
            ],
            {
                "area": "杭州",
                "work_mode": "全职",
                "materials_ready": True,
                "has_vehicle": False,
            },
        ),
        (
            "meituan_merchant",
            [
                (
                    "customer",
                    "我在成都开餐饮店。入驻材料都准备好了，但我是已经营业两年的老商家。",
                )
            ],
            {"area": "成都", "store_type": "餐饮", "materials_ready": True, "is_new_merchant": False},
        ),
        (
            "didi_driver",
            [
                ("customer", "我在厦门，有自有车辆，想跑快车。"),
                ("customer", "说错了，不是自有车辆，我现在是租赁车辆。"),
            ],
            {"city": "厦门", "vehicle_type": "租赁车辆", "service_type": "快车"},
        ),
        (
            "financial_consultation",
            [
                ("customer", "我主要想了解账单查询。"),
                ("agent", "您之前使用过这个 App 吗？"),
                ("customer", "从来没用过，这是第一次了解。"),
            ],
            {"purpose": "账单查询", "is_new_user": True},
        ),
    ],
)
async def test_real_profile_extraction(tmp_path, scenario_id, messages, expected):
    settings = replace(Settings.from_env(), runtime_dir=tmp_path, app_db=database_for(tmp_path / "app"))
    scenario = load_scenario(
        settings.project_root / "backend/scenarios" / scenario_id / "scenario.yaml"
    )
    service = BackendService(settings.app_db, scenario)
    try:
        ctx = service.create_session("synthetic-profile-smoke")
        for origin, text in messages:
            service.add_message(ctx, text, origin=origin)
        service.end_session(ctx)
        worker = ProfileWorker(settings)
        assert await worker.run_once()
        assert service.profiles.state(ctx)["status"] == "completed", service.profiles.state(ctx)
        actual = {m["field"]: m["value"] for m in service.read_memory(ctx)["data"]["fields"]}
        print(scenario_id, actual)
        for field, value in expected.items():
            if scenario.fields[field]["type"] == "string":
                assert value in actual[field]
            else:
                assert actual[field] == value
        next_call = service.create_session("synthetic-profile-smoke")
        assert all(
            service.profiles.baseline(next_call)[field]["value"] == actual[field]
            for field in expected
        )
        service.end_session(next_call)
    finally:
        service.close()
