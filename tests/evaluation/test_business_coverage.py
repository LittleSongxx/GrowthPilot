"""业务数据必须体现业务条件之间的差异，不能仅返回成功状态。"""

import asyncio
import copy
import json

import pytest

from evaluation.checks.business_coverage import (
    assess,
    evaluate_relations,
    load_cases,
    run,
    summarize,
)
from evaluation.judges.evidence import evidence_for


@pytest.fixture(scope="module")
def coverage_report(tmp_path_factory):
    return asyncio.run(run(tmp_path_factory.mktemp("business-coverage") / "report.json"))


def test_frozen_matrix_has_explicit_diverse_inputs_and_complete_tool_coverage():
    dataset = load_cases()
    assert len(dataset["city_matrix"]["cities"]) == 40
    assert dataset["city_matrix"]["expected_cases"] == 360
    assert len(dataset["cases"]) >= 475
    assert len({case["tool"] for case in dataset["cases"]}) == 12
    assert any("我现在在长沙" in json.dumps(case, ensure_ascii=False) for case in dataset["cases"])
    assert any("青溪镇" in json.dumps(case, ensure_ascii=False) for case in dataset["cases"])
    assert {case["expected"].get("match_type") for case in dataset["cases"]} >= {
        "curated_locality", "city_profile", "generic_fallback",
    }
    assert len(dataset["relations"]) >= 10


def test_broad_business_contracts_all_execute_without_network_or_model_calls(coverage_report):
    report = coverage_report
    assert report["summary"]["passed"], report["summary"]["failures"]
    assert not report["summary"]["missing_cases"]
    assert report["summary"]["executed_cases"] == report["summary"]["expected_cases"]
    assert report["model_requests"] == report["network_requests"] == 0
    assert report["dialogue_quality"] == report["real_business_validity"] == "not_evaluated"
    assert report["plan"]["national_city_cases"] == 360
    assert report["plan"]["additional_input_cases"] >= 115
    assert report["summary"]["tool_calls"] > len(report["records"])
    dimensions = report["summary"]["dimensions"]
    for dimension in ("national_city_matrix", "curated", "generic_fallback", "natural_location",
                      "meaningful_city_variation", "meaningful_business_conditions",
                      "financial_decimal", "expired_no_resurrection", "cross_process_determinism",
                      "station_pay_consistency", "location_correction"):
        assert dimensions[dimension]["cases"] > 0 and dimensions[dimension]["failed"] == 0


@pytest.mark.parametrize("mutation,assertion", [
    ("remove_deposit", "rental_full_upfront_cost"),
    ("invent_real_source", "internal_source_lineage"),
    ("lose_requested_location", "original_location_preserved"),
    ("remove_fallback_provenance", "fallback_explains_assumptions"),
])
def test_success_status_cannot_hide_broken_business_contract(coverage_report, mutation, assertion):
    case = next(c for c in load_cases()["cases"] if c["id"] == "city-长沙-03")
    result = copy.deepcopy(next(r["result"] for r in coverage_report["records"] if r["id"] == case["id"]))
    if mutation == "remove_deposit":
        result["data"]["items"][0].pop("deposit_yuan")
    elif mutation == "invent_real_source":
        result["data"]["source_type"] = "official_realtime"
    elif mutation == "lose_requested_location":
        result["data"]["requested_location"] = "北京"
    else:
        result["data"]["template_provenance"] = None
    checks = assess(case, result)
    assert any(c["assertion"].startswith(assertion) and not c["passed"] for c in checks)


def test_decimal_rounding_and_unknown_product_identity_cannot_be_laundered(coverage_report):
    cases = {case["id"]: case for case in load_cases()["cases"]}
    by_id = {row["id"]: row for row in coverage_report["records"]}
    result = copy.deepcopy(by_id["finance-decimal-3"]["result"])
    result["data"]["calculation"]["interest_yuan"] = 30.04
    checks = assess(cases["finance-decimal-3"], result)
    assert any(c["assertion"] == "financial_decimal_identity" and not c["passed"] for c in checks)
    result = copy.deepcopy(by_id["finance-unknown-product"]["result"])
    result["data"]["calculation"]["product_id"] = "unknown_learning_product"
    checks = assess(cases["finance-unknown-product"], result)
    assert any(c["assertion"] == "unknown_product_not_impersonated" and not c["passed"] for c in checks)


def test_self_delivery_does_not_require_a_platform_delivery_fee(coverage_report):
    case = next(c for c in load_cases()["cases"] if c["id"] == "condition-11")
    result = next(r["result"] for r in coverage_report["records"] if r["id"] == case["id"])
    assert all(item["delivery_fee_yuan_per_order"] is None for item in result["data"]["items"])
    assert all(c["passed"] for c in assess(case, result))


def test_same_price_everywhere_and_single_option_fail_useful_variation_checks(coverage_report):
    relation = load_cases()["relations"][0]
    records = copy.deepcopy([row for row in coverage_report["records"] if row["id"] in relation["case_ids"]])
    for row in records:
        for item in row["result"]["data"]["items"]:
            item["per_order_yuan"] = {"min": 4, "max": 6}
    assert not evaluate_relations([relation], records)[0]["passed"]


def test_catalog_evidence_retains_source_role_and_does_not_become_policy(coverage_report):
    result = next(r["model_result"] for r in coverage_report["records"] if r["id"] == "fallback-青溪镇-01")
    trace = {"scenario_id": "meituan_rider", "turns": [{
        "turn": 1, "user_message": "我在青溪镇，附近有站点吗？", "reply": "可以先看看站点和租车条件。",
        "tool_events": [{"seq": 1, "type": "tool_result", "data": {
            "name": "search_nearby_stations", "state": "success", "result": result,
        }}],
    }]}
    package = evidence_for(trace)
    sources = [row for row in package.values() if row["kind"] == "business_result"]
    assert sources
    assert not any(row["kind"] == "official_policy" for row in package.values())
    assert all("source_type" not in row["text"] for row in sources)


def test_dataset_and_report_do_not_hide_missing_inputs_or_overwrite_history(tmp_path, coverage_report):
    data = load_cases()
    data["cases"] = data["cases"][1:]
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(data, ensure_ascii=False))
    with pytest.raises(ValueError, match="matrix"):
        load_cases(path)
    summary = summarize([], ["frozen-missing-case"])
    assert not summary["passed"] and summary["missing_cases"] == ["frozen-missing-case"]
    old_report = tmp_path / "exists.json"
    old_report.write_text("historical")
    with pytest.raises(ValueError, match="fresh"):
        asyncio.run(run(old_report))
    assert old_report.read_text() == "historical"
