"""独立验证业务目录的覆盖边界，不调用付费模型，也不操作外部状态。"""

import copy
import json
import math
import re
import subprocess
import sys
from datetime import datetime

import pytest

from backend.agent_harness.business_data import BusinessCatalog
from tests.agent_harness.test_business_queries import LOCATION, NOW, catalog


@pytest.mark.parametrize(
    "text",
    ["长沙", "长沙市", "我在长沙", "我现在在长沙，想跑外卖", "长沙岳麓区", "湖南省长沙市岳麓区"],
)
def test_city_aliases_share_a_stable_scoped_example(text):
    current = catalog()
    result = current.query("rider_station", location=text, delivery_type="众包")
    canonical = current.query("rider_station", location="长沙", delivery_type="众包")
    assert result["status"] == "success"
    assert result["data"]["match_type"] == "city_profile"
    assert result["data"]["requested_location"] == text
    assert result["data"]["matched_location"] == "长沙"
    assert result["data"]["items"] == canonical["data"]["items"]
    assert result["data"]["template_provenance"]["profile_id"]


@pytest.mark.parametrize(
    "text",
    ["长沙或者北京", "上海市北京市", "北京上海", "北京还是长沙", "北京、长沙", "我从北京搬到长沙"],
)
def test_different_cities_are_not_silently_resolved(text):
    result = catalog().query("rider_station", location=text)
    assert result["status"] == "needs_clarification"
    assert result["data"]["reason"] == "conflicting_locations"
    assert result["data"]["items"] == []


@pytest.mark.parametrize(
    "text,city",
    [("上海南京西路", "上海"), ("广州北京路", "广州"), ("长沙县", "长沙"), ("长春朝阳区", "长春")],
)
def test_roads_and_counties_do_not_misidentify_other_cities(text, city):
    result = catalog().query("rider_station", location=text)
    assert result["status"] == "success"
    assert result["data"]["matched_location"] == city
    assert result["data"]["match_type"] == "city_profile"


def test_multiple_localities_fall_back_to_known_city_without_guessing_one():
    result = catalog().query("rider_station", location="北京中关村或望京")
    assert result["data"]["match_type"] == "city_profile"
    assert result["data"]["matched_location"] == "北京"
    assert "商圈尚未确定" in result["data"]["location_note"]
    assert (
        catalog().query("rider_station", location="北京海淀区望京")["status"]
        == "needs_clarification"
    )


def test_unknown_locality_is_deterministic_and_never_borrows_beijing_station_identity():
    one = catalog()
    two = BusinessCatalog(copy.deepcopy(one.data), "meituan_rider", clock=lambda: NOW)
    request = {"location": "云南南华县龙川镇", "delivery_type": "乐跑"}
    a = one.query("rider_station", **request)
    assert a == two.query("rider_station", **request)
    assert a["data"]["match_type"] == "generic_fallback"
    assert a["data"]["coverage_level"] == "generic"
    item = a["data"]["items"][0]
    assert item["id"].startswith("catalog_station_")
    assert item["location_id"] is None
    assert item["distance_meters"] is None
    assert item["eligibility_filters"]["delivery_type"] == "__generic__"
    assert a["data"]["requested_filters"]["delivery_type"] == "乐跑"


@pytest.mark.parametrize(
    "location,delivery_type", [("长沙", "众包"), ("合肥包河区", "专送"), ("云南南华县", "乐跑")]
)
def test_generated_station_pay_roundtrip_rejects_wrong_scope_and_type(location, delivery_type):
    query = catalog().query
    station = query("rider_station", location=location, delivery_type=delivery_type)["data"][
        "items"
    ][0]
    pay = query(
        "delivery_pay", location=location, delivery_type=delivery_type, station_id=station["id"]
    )
    assert pay["status"] == "success"
    assert pay["data"]["items"][0]["station_id"] == station["id"]
    for changed in (
        {"location": "武汉"},
        {"delivery_type": "不同配送类型"},
        {"station_id": "forged"},
    ):
        parameters = {
            "location": location,
            "delivery_type": delivery_type,
            "station_id": station["id"],
            **changed,
        }
        assert query("delivery_pay", **parameters)["status"] == "needs_clarification"


def test_city_profiles_change_numeric_catalog_assumptions_and_keep_results_bounded():
    current = catalog()
    profiles = current.data["expansion"]["city_profiles"]
    assert len(profiles) >= 40
    assert "长沙" in {p["city"] for p in profiles}
    prices = set()
    for profile in profiles:
        result = current.query("delivery_pay", location=profile["city"], delivery_type="众包")
        assert result["status"] == "success"
        assert result["data"]["match_type"] == "city_profile"
        assert len(result["data"]["items"]) <= 4
        item = result["data"]["items"][0]
        prices.add(item["per_order_yuan"]["min"])
        assert "参数配置" in item["pricing_basis"]["disclosure"]
        assert "city_profiles" not in result["data"]
    assert len(prices) >= 3


def test_unknown_conditions_remain_separate_from_the_assumed_example():
    cases = [
        ("didi_driver", "driver_earnings", {"service_type": "商务专车"}),
        (
            "meituan_merchant",
            "merchant_fee_plan",
            {"store_category": "鲜花店", "delivery_mode": "无人机配送"},
        ),
        ("didi_driver", "car_rental", {"vehicle_type": "氢能源"}),
    ]
    for scenario, kind, filters in cases:
        result = catalog(scenario).query(kind, location="长沙", **filters)
        assert result["data"]["match_type"] == "generic_fallback"
        assert result["data"]["requested_filters"] == filters
        assert result["data"]["unverified_conditions"]
        assert result["data"]["items"][0]["generated"] is True
        assert "__generic__" in result["data"]["items"][0]["eligibility_filters"].values()


def test_existing_user_examples_do_not_reuse_new_user_activity_conditions():
    for scenario, kind, filters in [
        (
            "meituan_merchant",
            "merchant_offer",
            {"store_category": "餐饮", "is_new_merchant": False},
        ),
        (
            "financial_consultation",
            "financial_offer",
            {"product_id": "catalog_flexible_loan", "is_new_user": False},
        ),
    ]:
        result = catalog(scenario).query(kind, location="长沙", **filters)
        assert result["status"] == "success"
        flag = "is_new_merchant" if scenario == "meituan_merchant" else "is_new_user"
        assert result["data"]["items"][0]["eligibility_filters"][flag] is False


@pytest.mark.parametrize(
    "purpose",
    [
        "登录",
        "我想了解注册",
        "看看理财功能",
        "看看账单",
        "只是想看看App怎么用，不想借款",
        "我只想了解这个App",
        "我想先看看钱怎么算，不准备借款",
        "给孩子做金融常识学习，了解App相关功能用途，无借款需求",
        "没有贷款意愿，想先看信息",
        "不需要借钱，了解注册就好",
    ],
)
def test_information_only_purposes_never_get_a_financing_quote(purpose):
    current = catalog("financial_consultation")
    result = current.query("financial_product", purpose=purpose)
    assert result["status"] == "success"
    item = result["data"]["items"][0]
    assert item["product_kind"] == "app_information"
    assert item["supports_cost_estimate"] is False
    assert "annual_rate_percent" not in item
    assert "amount_range_yuan" not in item
    assert (
        current.estimate_financing_cost(item["id"], 10000, 3)["data"]["reason"]
        == "product_has_no_financing_terms"
    )


def test_unknown_financing_product_is_independent_and_returned_id_roundtrips():
    current = catalog("financial_consultation")
    result = current.estimate_financing_cost("my-private-approval", 10000, 3)
    assert result["status"] == "success"
    details = result["data"]
    actual = details["effective_product_id"]
    assert details["requested_product_id"] == "my-private-approval"
    assert actual == "catalog_generic_financing"
    assert details["calculation"]["product_id"] == actual
    assert details["calculation"]["interest_yuan"] == 300
    assert details["calculation"]["total_repayment_yuan"] == 10300
    assert (
        current.query("financial_product", product_id=actual)["data"]["effective_product_id"]
        == actual
    )
    offer = current.query("financial_offer", product_id=actual, is_new_user=False)
    assert offer["data"]["effective_product_id"] == actual
    for amount in [float("nan"), float("inf"), -1, True, 1000.001]:
        assert (
            current.estimate_financing_cost("private", amount, 3)["status"] == "needs_clarification"
        )


def test_expiry_of_curated_record_profile_or_template_never_silently_resurrects_data():
    original = catalog().data
    for mutate, location in [
        (
            lambda data: [
                row.update(valid_until="2026-10-28T00:00:00Z", updated_at="2026-10-01T00:00:00Z")
                for row in data["records"]
                if row["kind"] == "rider_station"
            ],
            LOCATION,
        ),
        (
            lambda data: next(
                p for p in data["expansion"]["city_profiles"] if p["city"] == "长沙"
            ).update(valid_until="2026-10-28T00:00:00Z", updated_at="2026-10-01T00:00:00Z"),
            "长沙",
        ),
        (
            lambda data: data["expansion"]["templates"]["rider_station"].update(
                valid_until="2026-10-28T00:00:00Z", updated_at="2026-10-01T00:00:00Z"
            ),
            "未收录县城",
        ),
    ]:
        data = copy.deepcopy(original)
        mutate(data)
        result = BusinessCatalog(data, "meituan_rider", clock=lambda: NOW).query(
            "rider_station", location=location
        )
        assert result["status"] == "expired"
        assert result["data"]["items"] == []
    future = datetime.fromisoformat("2040-01-01T00:00:00+00:00").timestamp()
    result = BusinessCatalog(original, "meituan_rider", clock=lambda: future).query(
        "rider_station", location="未知地区"
    )
    assert result["status"] == "expired"


@pytest.mark.parametrize(
    "name",
    [
        "meituan_rider",
        "didi_driver",
        "meituan_merchant",
        "financial_consultation",
    ],
)
def test_scene_catalog_numbers_and_price_parameters_are_valid(name):
    def numbers(value):
        if isinstance(value, dict):
            for child in value.values():
                yield from numbers(child)
        elif isinstance(value, list):
            for child in value:
                yield from numbers(child)
        elif type(value) in (int, float):
            yield value

    data = catalog(name).data
    assert all(math.isfinite(value) for value in numbers(data))
    expected = {
        "meituan_rider": {"rider_pay_multiplier", "rental_multiplier"},
        "didi_driver": {"driver_fare_multiplier", "rental_multiplier"},
        "meituan_merchant": {"rider_pay_multiplier", "merchant_fee_adjustment_pp"},
        "financial_consultation": set(),
    }[name]
    for profile in data["expansion"]["city_profiles"] + [data["expansion"]["generic_profile"]]:
        assert set(profile["parameters"]) == expected
        assert all(
            value > 0 for key, value in profile["parameters"].items() if key.endswith("multiplier")
        )


@pytest.mark.parametrize("location", ["长沙镇", "福建省长春镇", "广州村"])
def test_same_named_towns_do_not_claim_city_profile_coverage(location):
    result = catalog().query("rider_station", location=location)
    assert result["status"] == "success"
    assert result["data"]["match_type"] == "generic_fallback"
    assert result["data"]["coverage_level"] == "generic"


@pytest.mark.parametrize(
    "location, city",
    [
        ("我住长沙雨花区附近", "长沙"),
        ("蓉城成都", "成都"),
        ("新疆乌鲁木齐市", "乌鲁木齐"),
        ("广西南宁市青秀区", "南宁"),
    ],
)
def test_natural_address_and_region_abbreviations_use_known_city(location, city):
    result = catalog().query("rider_station", location=location)
    assert result["status"] == "success"
    assert result["data"]["match_type"] == "city_profile"
    assert result["data"]["matched_location"] == city


BUSINESS_TEXT_INTERNAL_KEYS = {
    "id",
    "product_id",
    "station_id",
    "template_id",
    "variant_id",
    "profile_id",
    "pricing_basis",
    "source",
    "source_type",
    "template_provenance",
    "match_type",
    "fallback_reason",
    "requested_product_id",
    "effective_product_id",
}


def _business_text(value):
    if isinstance(value, dict):
        return " ".join(
            _business_text(v) for k, v in value.items() if k not in BUSINESS_TEXT_INTERNAL_KEYS
        )
    if isinstance(value, list):
        return " ".join(_business_text(v) for v in value)
    return value if isinstance(value, str) else ""


@pytest.mark.parametrize(
    "name",
    [
        "meituan_rider",
        "didi_driver",
        "meituan_merchant",
        "financial_consultation",
    ],
)
def test_all_payload_display_text_preserves_business_role_and_internal_audit(name):
    data = catalog(name).data
    payloads = [row["payload"] for row in data["records"]]
    payloads += [
        variant["payload"]
        for template in data["expansion"]["templates"].values()
        for variant in template["payloads"]
    ]
    forbidden = re.compile(
        r"演示|样例|示例|课程|画像|人为编排|人为设定|fixture|mock"
        r"|学习徽章|学习任务|教程|练习|复习|课堂|检查清单",
        re.IGNORECASE,
    )
    for payload in payloads:
        assert not forbidden.search(_business_text(payload)), payload
        if "recruitment_status" in payload:
            assert payload["recruitment_status"] == "招募情况需向站点确认"
    assert all(row["source_type"] == "local_catalog" for row in data["records"])
    assert all(
        template["source_type"] == "local_catalog"
        for template in data["expansion"]["templates"].values()
    )
    assert any(
        "本地版本化" in json.dumps(template, ensure_ascii=False)
        for template in data["expansion"]["templates"].values()
    )


@pytest.mark.parametrize("location", [LOCATION, "长沙", "云南南华县龙川镇"])
def test_contact_fields_are_available_without_role_breaking_notes(location):
    for scenario, kind in [
        ("meituan_rider", "rider_station"),
        ("meituan_rider", "vehicle_rental"),
        ("didi_driver", "driver_service_point"),
        ("didi_driver", "car_rental"),
    ]:
        result = catalog(scenario).query(kind, location=location)
        assert result["status"] == "success"
        for item in result["data"]["items"]:
            assert re.fullmatch(r"1[3-9]\d{9}", item["phone"])
            assert result["data"]["matched_location"] in item["address"]
            assert "号" in item["address"]
            assert "未提供" not in item.get("address_note", "")
            assert "未提供" not in item.get("location_note", "")
            assert "地址" not in item.get("availability", "")
            if kind in {"vehicle_rental", "car_rental"}:
                assert "需向服务方确认" in item["availability"]
            assert item.get("distance_meters") is None
            assert not re.search(r"演示|样例|示例|课程", _business_text(item))


def test_existing_unknown_store_offer_has_business_value_without_claiming_eligibility():
    result = catalog("meituan_merchant").query(
        "merchant_offer", location="长沙", store_category="鲜花店", is_new_merchant=False
    )
    assert result["status"] == "success"
    assert result["data"]["requested_filters"]["store_category"] == "鲜花店"
    item = result["data"]["items"][0]
    assert "50元运营服务费抵扣券" in item["benefit"]
    assert item["eligibility_filters"]["is_new_merchant"] is False
    conditions = " ".join(item["conditions"])
    assert "主动领取" in conditions and "30天" in conditions
    assert "不可提现" in conditions and "不承诺订单量或流量" in conditions
    assert "尚未核验" in item["eligibility_note"]


@pytest.mark.parametrize("is_new_user", [True, False])
def test_financial_account_services_do_not_reward_or_require_borrowing(is_new_user):
    result = catalog("financial_consultation").query(
        "financial_offer", product_id="unknown-account-product", is_new_user=is_new_user
    )
    assert result["status"] == "success"
    item = result["data"]["items"][0]
    assert "免费查看" in item["benefit"]
    assert "账户" in item["benefit"]
    conditions = " ".join(item["conditions"])
    assert "不要求借款" in conditions
    assert "不影响金融产品审批、利率或合同费用" in conditions
    assert not any(key in item for key in ("annual_rate_percent", "amount_range_yuan"))


def test_distinct_business_contacts_are_unique_and_same_entities_are_stable():
    rider, driver = catalog(), catalog("didi_driver")
    locations = [LOCATION, "北京海淀区中关村", "云南南华县龙川镇"] + [
        profile["city"] for profile in rider.data["expansion"]["city_profiles"]
    ]
    contacts = {}
    for location in locations:
        for current, kind in [
            (rider, "rider_station"),
            (rider, "vehicle_rental"),
            (driver, "driver_service_point"),
            (driver, "car_rental"),
        ]:
            result = current.query(kind, location=location)
            for item in result["data"]["items"]:
                key = (item["kind"], item["id"])
                contact = (item["address"], item["phone"])
                if key in contacts:
                    assert contacts[key] == contact
                contacts[key] = contact
    assert len(contacts) > 500
    assert len({phone for _, phone in contacts.values()}) == len(contacts)
    assert len({address for address, _ in contacts.values()}) == len(contacts)
    for alias in ["长沙", "长沙市", "我在长沙岳麓区"]:
        result = rider.query("rider_station", location=alias, delivery_type="众包")
        item = result["data"]["items"][0]
        assert contacts[(item["kind"], item["id"])] == (item["address"], item["phone"])


def test_generated_contacts_are_stable_in_a_fresh_python_process():
    from tests.paths import ROOT

    current = catalog()
    local = current.query("rider_station", location="长沙", delivery_type="众包")["data"]["items"][
        0
    ]
    program = """from pathlib import Path
from backend.agent_harness.scenario import load_scenario
from backend.agent_harness.business_data import BusinessCatalog
import json
import math
scenario = load_scenario(Path('backend/scenarios/meituan_rider/scenario.yaml'))
item = BusinessCatalog.from_scenario(scenario, clock=lambda: 1793232000.0).query('rider_station', location='长沙', delivery_type='众包')['data']['items'][0]
print(json.dumps({k: item[k] for k in ('id', 'address', 'phone')}, ensure_ascii=False))
"""
    completed = subprocess.run(
        [sys.executable, "-c", program], cwd=ROOT, check=True, capture_output=True, text=True
    )
    assert json.loads(completed.stdout) == {key: local[key] for key in ("id", "address", "phone")}


def test_contact_identity_is_independent_of_template_variant_names():
    original = catalog()
    changed = copy.deepcopy(original.data)
    for variant in changed["expansion"]["templates"]["vehicle_rental"]["payloads"]:
        variant["variant_id"] += "_renamed"
    updated = BusinessCatalog(changed, "meituan_rider", clock=lambda: NOW)
    for location in ["长沙", "云南南华县龙川镇"]:
        before = original.query("vehicle_rental", location=location)["data"]["items"]
        after = updated.query("vehicle_rental", location=location)["data"]["items"]
        assert [item["id"] for item in before] != [item["id"] for item in after]
        assert [(item["address"], item["phone"]) for item in before] == [
            (item["address"], item["phone"]) for item in after
        ]


def test_unknown_locality_contact_ignores_conversational_location_suffixes():
    query = catalog().query
    a = query("rider_station", location="云南南华县龙川镇", delivery_type="众包")
    b = query("rider_station", location="我目前在云南南华县龙川镇附近", delivery_type="众包")
    assert a["data"]["items"] == b["data"]["items"]
    assert b["data"]["requested_location"] == "我目前在云南南华县龙川镇附近"
    item = b["data"]["items"][0]
    assert item["name"].startswith("云南南华县龙川镇")
    assert "附近" not in item["address"]
