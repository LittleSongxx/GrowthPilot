"""验证业务数据保真、查询范围和可复现的金融计算。"""

import json
from datetime import datetime

import pytest

from backend.agent_harness.business_data import BusinessCatalog
from backend.agent_harness.business_data.catalog import DISCLOSURE, KINDS, REQUIRED_CONDITIONS
from backend.agent_harness.scenario import load_scenario
from backend.agent_harness.tools import ToolContext, build_tools
from backend.agent_harness.tools.didi_driver import search_driver_service_points
from tests.paths import ROOT

NOW = datetime.fromisoformat("2026-10-29T00:00:00+00:00").timestamp()
LOCATION = "北京朝阳区望京"


def scenario(name):
    return load_scenario(ROOT / "backend/scenarios" / name / "scenario.yaml")


def catalog(name="meituan_rider", clock=lambda: NOW):
    return BusinessCatalog.from_scenario(scenario(name), clock=clock)


def tool_context(name="meituan_rider"):
    config = scenario(name)
    allowed = {tool["id"] for tool in config["tools"]}
    return ToolContext(
        search_policy=lambda query: {},
        catalog=lambda: catalog(name),
        authorize=lambda name: None if name in allowed else "tool_not_declared",
        emit=lambda *_args: None,
    )


def test_city_examples_and_unknown_localities_are_explicitly_labeled():
    query = catalog().query
    for location in ("", "附近", "这里"):
        result = query("rider_station", location=location)
        assert result["status"] == "needs_clarification"
        assert result["data"]["items"] == []
    for location in ("北京", "北京市", "北京朝阳区"):
        result = query("rider_station", location=location)
        assert result["status"] == "success"
        assert result["data"]["match_type"] == "city_profile"
        assert result["data"]["coverage_level"] == "city"
    result = query("rider_station", location="河北省承德市双桥区")
    assert result["data"]["match_type"] == "generic_fallback"
    assert result["data"]["requested_location"] == "河北省承德市双桥区"
    good = query("rider_station", location="北京市 朝阳区 望京")
    assert good["status"] == "success"
    assert good["data"]["match_type"] == "curated_locality"
    assert good["data"]["matched_location"] == LOCATION
    assert len(good["data"]["items"]) == 2
    for station in good["data"]["items"]:
        assert station["distance_meters"] is None
        assert station["address"].startswith(LOCATION)
        assert "到访前" in station["address_note"]
        assert station["recruitment_status"] == "招募情况需向站点确认"


def test_delivery_type_and_station_must_both_match_location():
    query = catalog().query
    station = query("rider_station", location=LOCATION, delivery_type="众包")["data"]["items"][0]
    pay = query("delivery_pay", location=LOCATION, delivery_type="众包", station_id=station["id"])
    assert pay["status"] == "success"
    assert pay["data"]["items"][0]["station_id"] == station["id"]
    assert (
        query("delivery_pay", location=LOCATION, delivery_type="专送", station_id=station["id"])[
            "status"
        ]
        == "needs_clarification"
    )
    assert (
        query(
            "delivery_pay",
            location="北京海淀区中关村",
            delivery_type="众包",
            station_id=station["id"],
        )["status"]
        == "needs_clarification"
    )
    generic = query("rider_station", location=LOCATION, delivery_type="兼职")
    assert generic["data"]["match_type"] == "generic_fallback"
    assert generic["data"]["requested_filters"]["delivery_type"] == "兼职"
    assert generic["data"]["items"][0]["eligibility_filters"]["delivery_type"] == "__generic__"


def test_queries_preserve_catalog_provenance_and_business_conditions():
    examples = [
        (
            "meituan_rider",
            "delivery_pay",
            {"location": LOCATION, "delivery_type": "众包"},
            "实际每单按距离、时段、订单规则另行计算",
        ),
        (
            "didi_driver",
            "driver_earnings",
            {"location": LOCATION, "service_type": "快车"},
            "不含时长费、远途费、动态调价、奖励及司机用车成本",
        ),
        (
            "meituan_merchant",
            "merchant_offer",
            {"location": LOCATION, "store_category": "餐饮", "is_new_merchant": True},
            "券不可提现，不等于现金收入",
        ),
        (
            "financial_consultation",
            "financial_offer",
            {"product_id": "catalog_flexible_loan", "is_new_user": True},
            "不要求借款或提交额度申请",
        ),
    ]
    for name, kind, filters, condition in examples:
        result = catalog(name).query(kind, **filters)
        assert result["status"] == "success"
        assert result["data"]["disclosure"] == DISCLOSURE
        item = result["data"]["items"][0]
        assert condition in item["conditions"]
        assert isinstance(item["eligibility_filters"], dict)
        assert item["source_type"] == "local_catalog"
        assert "非品牌官方" in item["source"]["label"]
        assert item["valid_from"] < item["valid_until"]
        assert result["evidence"][0]["source_type"] == "local_catalog"


def test_expired_or_future_rows_do_not_return_old_prices():
    expired = catalog(clock=lambda: datetime.fromisoformat("2028-01-01T00:00:00+00:00").timestamp())
    result = expired.query("delivery_pay", location=LOCATION, delivery_type="众包")
    assert result["status"] == "expired"
    assert result["data"]["items"] == []
    assert "per_order_yuan" not in json.dumps(result)
    future = catalog(clock=lambda: datetime.fromisoformat("2026-01-01T00:00:00+00:00").timestamp())
    result = future.query("delivery_pay", location=LOCATION, delivery_type="众包")
    assert result["status"] == "no_data"
    assert result["data"]["items"] == []


def test_rental_complete_costs_and_strict_typed_filter():
    query = catalog().query
    result = query("vehicle_rental", location=LOCATION, battery_included=False)
    item = result["data"]["items"][0]
    assert item["monthly_rent_yuan"] == 399
    assert item["deposit_yuan"] == 500
    assert item["battery_included"] is False
    assert "不含" in item["battery_service"]
    assert "逾期" in item["other_fees_note"]
    assert (
        query("vehicle_rental", location=LOCATION, battery_included=0)["status"]
        == "needs_clarification"
    )


def test_merchant_filters_do_not_promote_other_categories_or_old_merchants():
    query = catalog("meituan_merchant").query
    old = query("merchant_offer", location=LOCATION, store_category="餐饮", is_new_merchant=False)
    assert old["status"] == "success"
    assert old["data"]["items"][0]["eligibility_filters"]["is_new_merchant"] is False
    unknown = query(
        "merchant_offer", location=LOCATION, store_category="药店", is_new_merchant=True
    )
    assert unknown["data"]["match_type"] == "generic_fallback"
    assert unknown["data"]["items"][0]["eligibility_filters"]["store_category"] == "__generic__"
    result = query(
        "merchant_fee_plan", location=LOCATION, store_category="餐饮", delivery_mode="商家自配送"
    )
    item = result["data"]["items"][0]
    assert item["delivery_fee_yuan_per_order"] is None
    assert "商家自配送成本" in item["excluded"]
    assert "最终协议" in item["quote_note"]


def test_financing_math_uses_only_the_chosen_example_terms():
    query = catalog("financial_consultation")
    result = query.estimate_financing_cost("catalog_flexible_loan", 10000, 3)
    calculation = result["data"]["calculation"]
    assert result["status"] == "success"
    assert calculation["interest_yuan"] == 300
    assert calculation["total_cost_yuan"] == 300
    assert calculation["total_repayment_yuan"] == 10300
    assert calculation["personal_quote"] is False
    assert "按月单利" in calculation["repayment_method"]
    assert calculation["assumptions"]
    assert (
        query.estimate_financing_cost("catalog_flexible_loan", 1000.55, 1)["data"]["calculation"][
            "interest_yuan"
        ]
        == 10.01
    )
    unknown = query.estimate_financing_cost("unknown_real_product", 10000, 3)
    assert unknown["status"] == "success"
    assert unknown["data"]["requested_product_id"] == "unknown_real_product"
    assert unknown["data"]["calculation"]["product_id"] == "catalog_generic_financing"
    for amount, term in [
        (999, 3),
        (100001, 3),
        (1000, 2),
        (True, 3),
        (float("nan"), 3),
        (1000, True),
        (1000.001, 3),
    ]:
        result = query.estimate_financing_cost("catalog_flexible_loan", amount, term)
        assert result["status"] == "needs_clarification"
        assert "calculation" not in result["data"]


def test_query_results_and_inputs_cannot_mutate_catalog_snapshot():
    current = catalog()
    first = current.query("delivery_pay", location=LOCATION, delivery_type="众包")
    first["data"]["items"][0]["per_order_yuan"]["min"] = 9999
    fresh = current.query("delivery_pay", location=LOCATION, delivery_type="众包")
    assert fresh["data"]["items"][0]["per_order_yuan"]["min"] == 4.5


@pytest.mark.parametrize(
    "name, count",
    [
        ("meituan_rider", 12),
        ("didi_driver", 8),
        ("meituan_merchant", 20),
        ("financial_consultation", 2),
    ],
)
def test_scene_catalogs_keep_their_records_templates_and_valid_provenance(name, count):
    config = scenario(name)
    assert config["business_data"]["ref"] == "catalog.json"
    data = catalog(name).data
    assert len(data["records"]) == count
    expected_kinds = {kind for kind, owner in KINDS.items() if owner == name}
    assert set(data["expansion"]["templates"]) == expected_kinds
    locations = {row["id"] for row in data["locations"]}
    records = data["records"]
    assert len({row["id"] for row in records}) == len(records)
    for row in records:
        assert row["scenario_id"] == name
        assert row["kind"] in expected_kinds
        assert row["location_id"] in locations or name == "financial_consultation"
        assert set(row["conditions"]) == REQUIRED_CONDITIONS[row["kind"]]
    sources = records + list(data["expansion"]["templates"].values())
    sources += data["expansion"]["city_profiles"] + [data["expansion"]["generic_profile"]]
    for source in sources:
        assert source["source_type"] == "local_catalog"
        assert source["source"]["id"] and source["source"]["label"]
        start, end, updated = [
            datetime.fromisoformat(source[key])
            for key in ("valid_from", "valid_until", "updated_at")
        ]
        assert start < end and updated < end


def test_financial_templates_include_independent_generic_and_information_options():
    data = catalog("financial_consultation").data
    assert data["locations"] == []
    assert data["expansion"]["city_profiles"] == []
    assert data["expansion"]["generic_profile"]["parameters"] == {}
    variants = data["expansion"]["templates"]["financial_product"]["payloads"]
    assert "catalog_generic_financing" in {variant["variant_id"] for variant in variants}
    products = [row["payload"] for row in data["records"] if row["kind"] == "financial_product"]
    products += [variant["payload"] for variant in variants]
    for product in products:
        if product.get("supports_cost_estimate") is False:
            assert product["product_kind"] == "app_information"
            assert product["purpose_note"]
            assert "annual_rate_percent" not in product
        else:
            assert product["interest_method"] == "simple_interest_bullet"
            assert product["term_months"]
            assert all(type(term) is int and 1 <= term <= 120 for term in product["term_months"])
            assert 0 < product["amount_range_yuan"]["min"] <= product["amount_range_yuan"]["max"]
            assert 0 <= product["annual_rate_percent"] <= 100
            assert product["service_fee_yuan"] >= 0
            assert product["repayment_method"] and product["assumptions"]


@pytest.mark.asyncio
async def test_native_business_tools_register_current_scenario_and_preserve_query_results():
    context = tool_context()
    rider = build_tools(context, scenario("meituan_rider")["tools"])
    names = set(rider)
    assert names == {
        "search_policy",
        "send_download_sms",
        "search_nearby_stations",
        "query_delivery_pay",
        "search_vehicle_rentals",
        "estimate_work_budget",
    }
    result = await rider["search_nearby_stations"](location=LOCATION)
    assert result["status"] == "success"
    assert (await search_driver_service_points(context, location=LOCATION))["status"] == "denied"
    generic = await rider["search_nearby_stations"](location=LOCATION, delivery_type="兼职")
    assert generic["status"] == "success"
    assert generic["data"]["items"]
    assert (await rider["search_nearby_stations"]())["status"] == "denied"
    assert (await rider["search_vehicle_rentals"](location=LOCATION, battery_included="false"))[
        "status"
    ] == "denied"
    assert (await rider["search_nearby_stations"](location=LOCATION))["data"] == result["data"]
    assert context.catalog().query("financial_product", purpose="了解产品")["status"] == "denied"
