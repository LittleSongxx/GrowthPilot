"""基于冻结的广泛业务用例和独立业务约束检查覆盖情况，不调用模型或网络。

运行命令：uv run python -m evaluation.checks.business_coverage --output .run/checks/business-coverage.json
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from backend.agent_harness import BackendService, load_scenario
from backend.agent_harness.business_data import BusinessCatalog
from backend.agent_harness.runtime.session_manager import SessionManager
from backend.database import temporary_database
from backend.paths import resolve_project_path
from backend.settings import Settings

from ..common.io import dump
from ..common.paths import PROJECT_ROOT
from .output_audit import audit_visible

DATASET = PROJECT_ROOT / "evaluation/checks/data/business-coverage-v1.json"
SCENARIOS = ("meituan_rider", "didi_driver", "meituan_merchant", "financial_consultation")
TIERS = {"curated_locality", "city_profile", "generic_fallback"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_cases(path=DATASET):
    data = json.loads(resolve_project_path(path).read_text())
    if data.get("version") != "business-coverage-v1" or not data.get("cases"):
        raise ValueError("Unknown or empty business coverage dataset")
    ids = [case["id"] for case in data["cases"]]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate business case IDs")
    matrix = data["city_matrix"]
    pairs = Counter()
    for case in data["cases"]:
        if case["scenario_id"] not in SCENARIOS or not case.get("dimensions"):
            raise ValueError("Missing case scope or dimensions")
        if not isinstance(case.get("arguments"), dict) or not case.get("expected", {}).get("status"):
            raise ValueError("Each frozen case needs explicit inputs and expected status")
        if "national_city_matrix" in case["dimensions"]:
            city = case["arguments"].get("location", case["arguments"].get("area"))
            pairs[(city, case["tool"])] += 1
    expected_pairs = {(city, tool) for city in matrix["cities"] for tool in matrix["tools"]}
    if set(pairs) != expected_pairs or set(pairs.values()) != {1}:
        raise ValueError("National city matrix has missing, unexpected or duplicate cells")
    if matrix["expected_cases"] != len(expected_pairs):
        raise ValueError("City matrix denominator changed")
    if not set(data["cross_process_case_ids"]) <= set(ids):
        raise ValueError("Cross-process checks must reference actual frozen cases")
    return data


def finite_nonnegative(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def assess(case, result):
    """依据面向用户的接口约定进行检查，不依赖目录行数或生成器公式。"""
    checks = []

    def check(name, passed, detail=""):
        checks.append({"assertion": name, "passed": bool(passed), "detail": detail})

    expected, args, tool = case["expected"], case["arguments"], case["tool"]
    status = result.get("status")
    data = result.get("data", {})
    items = data.get("items", [])
    check("expected_status", status == expected["status"], f"expected={expected['status']}; actual={status}")
    check("single_call_result", result.get("retryable") is False)
    if status != "success":
        check("unsuccessful_result_has_no_quote", not items and not data.get("calculation"))
        return checks
    check("nonempty_business_result", bool(items))
    tier = data.get("match_type")
    check("explicit_match_tier", tier in TIERS)
    if "match_type" in expected:
        check("expected_match_tier", tier == expected["match_type"], f"expected={expected['match_type']}; actual={tier}")
    check("internal_source_lineage", data.get("source_type") == "local_catalog"
          and bool(data.get("disclosure")))
    check("fallback_flag", data.get("is_fallback") is (tier != "curated_locality"))
    if tier != "curated_locality":
        provenance = data.get("template_provenance") or {}
        check("fallback_explains_assumptions", bool(data.get("fallback_reason"))
              and isinstance(data.get("assumed_filters"), list)
              and bool(data["assumed_filters"])
              and all(isinstance(value, dict) for value in data["assumed_filters"])
              and bool(provenance.get("template_id")) and bool(provenance.get("source")))
        check("fallback_template_validity", bool(provenance.get("valid_from"))
              and bool(provenance.get("valid_until")))
    location = args.get("location", args.get("area"))
    if location is not None:
        check("original_location_preserved", data.get("requested_location") == location)
        scope = data.get("coverage_level")
        scope_valid = (scope == {"curated_locality": "locality", "city_profile": "city"}.get(tier)
                       if tier != "generic_fallback" else scope in {"locality", "city", "generic"}
                       and bool(data.get("unverified_conditions")))
        check("location_scope_disclosed", scope_valid)
    if expected.get("matched_contains"):
        check("selected_location_matches_input", expected["matched_contains"] in data.get("matched_location", ""))
    requested = data.get("requested_filters", {})
    filter_args = {key: value for key, value in args.items()
                   if key not in {"location", "area", "amount", "term_months"}}
    check("requested_conditions_preserved", all(requested.get(key) == value for key, value in filter_args.items()))
    evidence_ids = {row.get("id") for row in result.get("evidence", []) if isinstance(row, dict)}
    now = datetime.fromisoformat(case.get("clock", "2026-10-29T12:00:00+08:00")).timestamp()
    for item in items:
        prefix = str(item.get("id", "missing-id"))
        check("item_source:" + prefix, item.get("source_type") == "local_catalog"
              and bool(item.get("source")) and item.get("id") in evidence_ids)
        try:
            start = datetime.fromisoformat(item["valid_from"]).timestamp()
            end = datetime.fromisoformat(item["valid_until"]).timestamp()
            valid = start <= now < end
        except (KeyError, ValueError, TypeError):
            valid = False
        check("item_current:" + prefix, valid)
        if tool in {"search_nearby_stations", "search_driver_service_points"}:
            check("contact_fields_usable:" + prefix, isinstance(item.get("address"), str)
                  and bool(item["address"].strip()) and re.fullmatch(r"1[3-9]\d{9}", str(item.get("phone", ""))))
            check("no_real_distance:" + prefix, item.get("distance_meters") is None
                  and bool(item.get("distance_note")) and bool(item.get("address_note")))
        elif tool == "query_delivery_pay":
            interval = item.get("per_order_yuan", {})
            check("bounded_pay:" + prefix, finite_nonnegative(interval.get("min"))
                  and finite_nonnegative(interval.get("max")) and interval["min"] <= interval["max"])
            check("pay_exclusions_and_no_monthly_promise:" + prefix, bool(item.get("excluded"))
                  and item.get("monthly_income") is None and bool(item.get("monthly_income_note")))
            if args.get("station_id"):
                check("station_pay_binding:" + prefix, item.get("station_id") == args["station_id"])
        elif tool in {"search_vehicle_rentals", "search_car_rentals"}:
            check("rental_full_upfront_cost:" + prefix,
                  finite_nonnegative(item.get("monthly_rent_yuan"))
                  and finite_nonnegative(item.get("deposit_yuan"))
                  and type(item.get("minimum_term_months")) is int
                  and item["minimum_term_months"] > 0)
            if tool == "search_vehicle_rentals":
                check("battery_and_maintenance_boundary:" + prefix,
                      type(item.get("battery_included")) is bool and bool(item.get("battery_service"))
                      and bool(item.get("maintenance")) and bool(item.get("other_fees_note")))
                if "battery_included" in args:
                    check("battery_selection:" + prefix, item["battery_included"] is args["battery_included"])
            else:
                check("car_cost_exclusions:" + prefix, bool(item.get("included")) and bool(item.get("excluded"))
                      and bool(item.get("eligibility_note")))
        elif tool == "query_driver_earnings":
            fare = item.get("fare_components", {})
            check("driver_cost_not_net_income:" + prefix,
                  all(finite_nonnegative(fare.get(key)) for key in (
                      "base_fare_yuan", "base_distance_km", "additional_km_yuan"))
                  and finite_nonnegative(item.get("platform_service_fee_percent"))
                  and bool(item.get("conditions")) and bool(item.get("income_note")))
        elif tool == "query_merchant_fee_plans":
            fee = item.get("delivery_fee_yuan_per_order")
            if item.get("delivery_mode") == "商家自配送":
                delivery_cost_valid = fee is None
            else:
                delivery_cost_valid = (isinstance(fee, dict) and finite_nonnegative(fee.get("min"))
                                       and finite_nonnegative(fee.get("max")) and fee["min"] <= fee["max"])
            check("merchant_cost_components:" + prefix,
                  finite_nonnegative(item.get("technical_service_fee_percent"))
                  and delivery_cost_valid and bool(item.get("excluded")) and bool(item.get("quote_note")))
        elif tool in {"query_merchant_offers", "query_financial_offers"}:
            check("offer_conditions_visible:" + prefix, bool(item.get("conditions")) and bool(item.get("eligibility_note")))
            flag = "is_new_merchant" if tool == "query_merchant_offers" else "is_new_user"
            if args.get(flag) is False:
                check("not_awarding_new_user_only_offer:" + prefix,
                      item.get("eligibility_filters", {}).get(flag) is not True)
    if expected.get("product_kind"):
        check("financial_purpose_not_invented", all(
            item.get("product_kind") == expected["product_kind"] for item in items))
        if expected["product_kind"] == "app_information":
            check("information_only_has_no_loan_quote", all(
                item.get("supports_cost_estimate") is False
                and not {"annual_rate_percent", "amount_range_yuan", "term_months"} & set(item)
                for item in items))
    if tool == "estimate_financing_cost":
        calc = data.get("calculation", {})
        check("financial_formula_disclosed", bool(calc.get("formula")) and bool(calc.get("assumptions"))
              and bool(calc.get("rounding")) and calc.get("personal_quote") is False)
        check("financial_input_preserved", calc.get("principal_yuan") == args["amount"]
              and calc.get("term_months") == args["term_months"])
        try:
            product = next(item for item in items if item["id"] == calc["product_id"])
            principal = Decimal(str(args["amount"]))
            interest = (principal * Decimal(str(product["annual_rate_percent"]))
                        * Decimal(args["term_months"]) / Decimal(1200)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            fee = Decimal(str(product["service_fee_yuan"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            correct = (Decimal(str(calc["interest_yuan"])) == interest
                       and Decimal(str(calc["total_cost_yuan"])) == interest + fee
                       and Decimal(str(calc["total_repayment_yuan"])) == principal + interest + fee)
        except (KeyError, StopIteration, ValueError, TypeError):
            correct = False
        check("financial_decimal_identity", correct)
        for field in ("interest_yuan", "total_repayment_yuan"):
            if field in expected:
                check("frozen_financial_oracle:" + field,
                      Decimal(str(calc.get(field, -1))) == Decimal(expected[field]))
        if "effective_product_id" in expected:
            check("unknown_product_not_impersonated", data.get("effective_product_id") == expected["effective_product_id"]
                  and calc.get("product_id") == expected["effective_product_id"]
                  and data.get("requested_product_id") == args["product_id"])
    return checks


def assess_visible(raw, visible):
    """内部来源信息保留在审计中，金额和业务条件仍保持可见。"""
    leaks = audit_visible(visible, business_result=True)
    checks = [{"assertion": "model_visible_no_internal_language", "passed": not leaks,
               "detail": json.dumps(leaks, ensure_ascii=False)},
              {"assertion": "visible_business_status_preserved",
               "passed": visible.get("status") == raw.get("status"), "detail": ""}]
    raw_data, data = raw.get("data", {}), visible.get("data", {})
    raw_items, items = raw_data.get("items", []), data.get("items", [])
    expected = {item.get("id"): item for item in raw_items}
    actual = {item.get("id"): item for item in items}
    checks.append({"assertion": "visible_selected_records_preserved",
                   "passed": len(raw_items) == len(items) and set(expected) == set(actual), "detail": ""})
    fields = {
        "id", "name", "address", "phone", "distance_meters", "area", "delivery_type", "station_id",
        "per_order_yuan", "monthly_income", "monthly_income_note", "monthly_rent_yuan", "deposit_yuan",
        "minimum_term_months", "battery_included", "battery_service", "maintenance", "other_fees_note",
        "included", "excluded", "eligibility_note", "quote_note", "conditions", "delivery_mode",
        "fare_components", "platform_service_fee_percent", "technical_service_fee_percent",
        "delivery_fee_yuan_per_order", "annual_rate_percent", "service_fee_yuan", "amount_range_yuan",
        "term_months", "product_kind", "supports_cost_estimate", "valid_from", "valid_until",
    }
    for item_id, item in expected.items():
        checks.append({"assertion": "visible_business_terms_preserved:" + str(item_id),
                       "passed": all(actual.get(item_id, {}).get(field) == item[field]
                                     for field in fields & item.keys()), "detail": ""})
    for field in ("calculation", "requested_location", "matched_location", "requested_product_id",
                  "effective_product_id"):
        if field in raw_data:
            checks.append({"assertion": "visible_context_preserved:" + field,
                           "passed": data.get(field) == raw_data[field], "detail": ""})
    return checks


async def execute_cases(cases, reference_clock, project_root=PROJECT_ROOT):
    """在隔离的真实会话中调用带类型定义的公开工具，不调用政策服务或模型供应商。"""
    results = []
    with (
        tempfile.TemporaryDirectory(prefix="growth-business-") as temporary,
        temporary_database(Settings.from_env().app_db, prefix="growth_business") as database,
    ):
        root = Path(temporary)
        settings = Settings(project_root, root, database)
        services = {sid: BackendService(settings.app_db, load_scenario(
            project_root / f"backend/scenarios/{sid}/scenario.yaml"), workspace_id="business-evaluation")
            for sid in SCENARIOS}
        manager = SessionManager(settings, services)
        try:
            conversations = {sid: await manager.create(sid, "coverage-" + sid) for sid in SCENARIOS}
            event_cursors = {sid: 0 for sid in SCENARIOS}
            for case in cases:
                conversation = conversations[case["scenario_id"]]
                now = datetime.fromisoformat(case.get("clock", reference_clock)).timestamp()
                conversation.catalog = BusinessCatalog.from_scenario(
                    conversation.service.scenario, clock=lambda now=now: now)
                before = conversation.service.snapshot(conversation.ctx)
                tool_calls = 0
                try:
                    tool = conversation.tools[case["tool"]]
                    tool_calls += 1
                    result = await tool(**case["arguments"])
                    tool_calls += 1
                    repeated = await tool(**copy.deepcopy(case["arguments"]))
                    after = conversation.service.snapshot(conversation.ctx)
                    events = manager.store.events(conversation.id, after=event_cursors[case["scenario_id"]], limit=3000)
                    event_cursors[case["scenario_id"]] = max(
                        (event["seq"] for event in events), default=event_cursors[case["scenario_id"]])
                    audits = [event["data"]["result"] for event in events
                              if event["type"] == "business_query_audit" and event["data"].get("name") == case["tool"]]
                    raw_result = audits[0] if audits else result
                    checks = assess(case, raw_result)
                    checks.extend(assess_visible(raw_result, result))
                    if raw_result.get("data", {}).get("source_type") == "local_catalog":
                        checks.append({"assertion": "raw_query_audit_captured",
                                       "passed": len(audits) == 2 and audits[0] == audits[1], "detail": ""})
                    checks.extend([
                        {"assertion": "repeat_determinism", "passed": result == repeated, "detail": ""},
                        {"assertion": "query_has_no_state_side_effect", "passed": before == after, "detail": ""},
                    ])
                    results.append({"id": case["id"], "dimensions": case["dimensions"],
                                    "tool": case["tool"], "result": raw_result, "model_result": result, "checks": checks,
                                    "passed": all(check["passed"] for check in checks), "tool_calls": tool_calls})
                except Exception as error:  # noqa: BLE001 - 保留失败用例，同时继续展示后续用例。
                    results.append({"id": case["id"], "dimensions": case["dimensions"],
                                    "tool": case["tool"], "passed": False,
                                    "error_category": type(error).__name__, "checks": [], "tool_calls": tool_calls})
        finally:
            await manager.close()
    return results


async def station_sequences(sequences, reference_clock):
    """业务工具返回的 ID 只能用于同一地区和同一类型。"""
    findings = []
    for sequence in sequences:
        station_case = {
            "id": sequence["id"], "scenario_id": "meituan_rider", "tool": "search_nearby_stations",
            "arguments": {"location": sequence["location"], "delivery_type": sequence["delivery_type"]},
            "expected": {"status": "success", "match_type": sequence["expected_tier"]},
            "dimensions": ["station_pay_consistency"],
        }
        initial = (await execute_cases([station_case], reference_clock))[0]
        items = initial.get("result", {}).get("data", {}).get("items", [])
        findings.append(initial)
        if not initial["passed"] or not items:
            continue
        station_id = items[0]["id"]
        args = {"area": sequence["location"], "delivery_type": sequence["delivery_type"], "station_id": station_id}
        cases = [{**station_case, "id": sequence["id"] + "-pay", "tool": "query_delivery_pay", "arguments": args}]
        for suffix, changes in (
            ("wrong-region", {"area": "上海"}),
            ("wrong-type", {"delivery_type": "专送"}),
            ("forged-id", {"station_id": "forged-station-does-not-exist"}),
        ):
            cases.append({**station_case, "id": sequence["id"] + "-" + suffix,
                          "tool": "query_delivery_pay", "arguments": {**args, **changes},
                          "expected": {"status": "needs_clarification"}})
        findings.extend(await execute_cases(cases, reference_clock))
    return findings


def evaluate_relations(relations, records):
    """比较各返回结果中有业务意义的差异，不依赖生成器内部实现。"""
    by_id = {row["id"]: row for row in records}
    findings = []
    for spec in relations:
        selected = [by_id.get(case_id) for case_id in spec["case_ids"]]
        values = set()
        complete = all(row and row["passed"] for row in selected)
        for row in selected:
            for item in (row or {}).get("result", {}).get("data", {}).get("items", []):
                value = item
                for key in spec["path"].split("."):
                    value = value.get(key) if isinstance(value, dict) else None
                if value is not None:
                    values.add(json.dumps(value, ensure_ascii=False, sort_keys=True))
        passed = complete and len(values) >= spec["minimum_distinct"]
        findings.append({"id": "relation-" + spec["id"], "dimensions": [spec["dimension"]],
                         "passed": passed, "tool_calls": 0,
                         "checks": [{"assertion": "independent_result_variation", "passed": passed,
                                     "detail": f"distinct={len(values)}; minimum={spec['minimum_distinct']}"}]})
    return findings


def summarize(records, expected_ids):
    by_id = {row["id"]: row for row in records}
    if len(by_id) != len(records) or set(by_id) - set(expected_ids):
        raise ValueError("Duplicate or unexpected coverage record")
    missing = sorted(set(expected_ids) - set(by_id))
    failures = [{"id": row["id"], "assertions": [c["assertion"] for c in row["checks"] if not c["passed"]],
                 **({"error_category": row["error_category"]} if row.get("error_category") else {})}
                for row in records if not row["passed"]]
    dimensions = defaultdict(lambda: {"cases": 0, "passed": 0, "failed": 0})
    for row in records:
        for dimension in row["dimensions"]:
            dimensions[dimension]["cases"] += 1
            dimensions[dimension]["passed" if row["passed"] else "failed"] += 1
    return {"passed": not failures and not missing, "expected_cases": len(expected_ids),
            "executed_cases": len(records), "passed_cases": sum(row["passed"] for row in records),
            "missing_cases": missing, "failures": failures, "dimensions": dict(dimensions),
            "assertions_executed": sum(len(row["checks"]) for row in records),
            "tool_calls": sum(row.get("tool_calls", 0) for row in records)}


async def run(output, dataset_path=DATASET):
    output = resolve_project_path(output)
    if output.exists():
        raise ValueError("Use a fresh report path; do not overwrite historical results")
    dataset = load_cases(dataset_path)
    results = await execute_cases(dataset["cases"], dataset["reference_clock"])
    sequences = await station_sequences(dataset["station_pay_sequences"], dataset["reference_clock"])
    results.extend(sequences)
    cross_cases = [case for case in dataset["cases"] if case["id"] in dataset["cross_process_case_ids"]]
    with tempfile.TemporaryDirectory(prefix="growth-business-worker-") as directory:
        request_path = Path(directory) / "request.json"
        request_path.write_text(json.dumps({"cases": cross_cases, "clock": dataset["reference_clock"]}, ensure_ascii=False))
        completed = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-m", "evaluation.checks.business_coverage", "--worker-json", str(request_path)],
            cwd=PROJECT_ROOT, capture_output=True, text=True, check=True, timeout=60,
        )
        worker = json.loads(completed.stdout)
    by_id = {row["id"]: row for row in results}
    cross_checks = []
    for row in worker:
        same = (row.get("result") == by_id[row["id"]].get("result")
                and row.get("model_result") == by_id[row["id"]].get("model_result"))
        cross_checks.append({"id": "cross-process-" + row["id"], "tool": row["tool"],
                             "dimensions": ["cross_process_determinism"], "passed": same, "tool_calls": row.get("tool_calls", 0),
                             "checks": [{"assertion": "same_result_in_fresh_process", "passed": same, "detail": ""}]})
    results.extend(cross_checks)
    results.extend(evaluate_relations(dataset.get("relations", []), results))
    # 统计分母仅由冻结的计划决定，不随实际完成的用例数量变化。
    expected_ids = [case["id"] for case in dataset["cases"]]
    expected_ids += [sequence["id"] for sequence in dataset["station_pay_sequences"]]
    expected_ids += [sequence["id"] + suffix for sequence in dataset["station_pay_sequences"]
                     for suffix in ("-pay", "-wrong-region", "-wrong-type", "-forged-id")]
    expected_ids += ["cross-process-" + case_id for case_id in dataset["cross_process_case_ids"]]
    expected_ids += ["relation-" + relation["id"] for relation in dataset.get("relations", [])]
    report = {"kind": "offline_business_coverage", "version": dataset["version"], "evaluation_revision": "2026-10-30-business-contracts",
              "dataset_sha256": digest(dataset), "reference_clock": dataset["reference_clock"],
              "model_requests": 0, "network_requests": 0, "dialogue_quality": "not_evaluated",
              "real_business_validity": "not_evaluated", "city_matrix": dataset["city_matrix"],
              "plan": {"national_city_cases": dataset["city_matrix"]["expected_cases"],
                       "additional_input_cases": len(dataset["cases"]) - dataset["city_matrix"]["expected_cases"],
                       "station_link_sequences": len(dataset["station_pay_sequences"]),
                       "cross_process_cases": len(dataset["cross_process_case_ids"]),
                       "cross_result_relations": len(dataset.get("relations", []))},
              "summary": summarize(results, expected_ids), "records": results}
    dump(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=resolve_project_path)
    parser.add_argument("--dataset", type=resolve_project_path, default=DATASET)
    parser.add_argument("--worker-json", type=resolve_project_path)
    args = parser.parse_args()
    if args.worker_json:
        request = json.loads(args.worker_json.read_text())
        print(json.dumps(asyncio.run(execute_cases(request["cases"], request["clock"])), ensure_ascii=False))
        return
    if not args.output:
        parser.error("--output is required")
    report = asyncio.run(run(args.output, args.dataset))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["summary"]["passed"] else 1)


if __name__ == "__main__":
    main()
