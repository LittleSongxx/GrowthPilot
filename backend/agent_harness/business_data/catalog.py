"""在指定范围内执行结果可复现的查询；此适配器不会修改外部业务状态。"""

from __future__ import annotations

import copy
import json
import math
import time
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from .expansion import BusinessExpansion
from .locations import LocationResolver

DISCLOSURE = "请结合适用条件和有效期使用以下信息；费用和服务范围以对应方案约定为准。"
KINDS = {
    "rider_station": "meituan_rider",
    "delivery_pay": "meituan_rider",
    "vehicle_rental": "meituan_rider",
    "driver_service_point": "didi_driver",
    "driver_earnings": "didi_driver",
    "car_rental": "didi_driver",
    "delivery_option": "meituan_merchant",
    "merchant_fee_plan": "meituan_merchant",
    "merchant_offer": "meituan_merchant",
    "financial_product": "financial_consultation",
    "financial_offer": "financial_consultation",
}
REQUIRED_CONDITIONS = {
    "rider_station": {"delivery_type"},
    "delivery_pay": {"delivery_type", "station_id"},
    "vehicle_rental": {"battery_included"},
    "driver_service_point": set(),
    "driver_earnings": {"service_type"},
    "car_rental": {"vehicle_type"},
    "delivery_option": {"store_category"},
    "merchant_fee_plan": {"store_category", "delivery_mode"},
    "merchant_offer": {"store_category", "is_new_merchant"},
    "financial_product": {"purposes"},
    "financial_offer": {"product_id", "is_new_user"},
}


class BusinessCatalog:
    """只读访问场景数据；通过注入时钟，让有效期判断可以复现。"""

    def __init__(self, data, scenario_id, *, clock=time.time):
        self.data = copy.deepcopy(data)
        self.scenario_id = scenario_id
        self.clock = clock
        self.records = self.data["records"]
        self.expansion = BusinessExpansion(self.data["expansion"], self._result, clock)
        self.location_resolver = LocationResolver(
            self.data["locations"], self.data["expansion"]["city_profiles"], self._result
        )

    @classmethod
    def from_scenario(cls, scenario, *, clock=time.time):
        ref = scenario.data["business_data"]["ref"]
        return cls(json.loads(scenario.resources[ref]), scenario.id, clock=clock)

    def _result(self, status, *, items=None, **details):
        items = items or []
        evidence = [
            {
                k: item[k]
                for k in ("id", "source_type", "source", "valid_from", "valid_until", "updated_at")
            }
            for item in items
        ]
        return {
            "status": status,
            "data": {
                "items": items,
                "source_type": "local_catalog",
                "disclosure": DISCLOSURE,
                "match_type": None,
                "is_fallback": False,
                "requested_location": None,
                "matched_location": None,
                "coverage_level": "not_applicable",
                "fallback_reason": None,
                "generated": False,
                "template_provenance": None,
                **details,
            },
            "evidence": evidence,
            "retryable": False,
        }

    def query(self, kind, *, location=None, **filters):
        """优先查找已整理的记录，再使用场景对应的城市模板或通用模板。"""
        if KINDS.get(kind) != self.scenario_id:
            return self._result("denied", reason="scenario_tool_mismatch")
        if not self._valid_filters(kind, filters):
            return self._result("needs_clarification", reason="invalid_business_filters")
        context, error = self._location_context(kind, location)
        if error:
            return error
        rows = self._matching_records(kind, filters, context["location_id"])
        product = None
        if kind == "financial_offer":
            product = self.query("financial_product", product_id=filters["product_id"])
            if product["status"] != "success":
                return product
        result = (
            self._curated_result(kind, rows, filters, context)
            if rows
            else self.expansion.query(kind, filters, context)
        )
        if product and (result["status"] == "success" or result["data"]["generated"]):
            self._link_offer_product(result, filters["product_id"], product)
        return result

    @staticmethod
    def _valid_filters(kind, filters):
        allowed = (
            REQUIRED_CONDITIONS[kind] if kind != "financial_product" else {"purpose", "product_id"}
        )
        optional = {
            "rider_station": {"delivery_type"},
            "delivery_pay": {"station_id"},
            "vehicle_rental": {"battery_included"},
            "car_rental": {"vehicle_type"},
        }.get(kind, set())
        if filters.keys() - allowed:
            return False
        if kind == "financial_product":
            if not (filters.get("purpose") or filters.get("product_id")):
                return False
        elif any(filters.get(key) is None for key in allowed - optional):
            return False
        for name, value in filters.items():
            if value is None and name in optional:
                continue
            if name in {"battery_included", "is_new_merchant", "is_new_user"}:
                if type(value) is not bool:
                    return False
            elif not isinstance(value, str) or not value.strip() or len(value) > 200:
                return False
        return True

    def _location_context(self, kind, location):
        if not kind.startswith("financial_"):
            return self.location_resolver.resolve(location)
        return {
            "scope_key": "financial",
            "location_id": None,
            "city": None,
            "requested_location": None,
            "matched_location": None,
            "coverage_level": "not_applicable",
            "match_type": "curated_locality",
            "fallback_reason": None,
        }, None

    def _matching_records(self, kind, filters, location_id):
        # 仅咨询信息的用途必须使用专门的非融资模板。
        if kind == "financial_product" and self.expansion.is_information_purpose(
            filters.get("purpose")
        ):
            return []
        rows = [
            row for row in self.records if row["kind"] == kind and row["location_id"] == location_id
        ]
        for name, value in filters.items():
            if value is None:
                continue
            if name == "purpose":
                rows = [row for row in rows if value in row["conditions"]["purposes"]]
            elif name == "product_id" and kind == "financial_product":
                rows = [row for row in rows if row["id"] == value]
            else:
                rows = [
                    row
                    for row in rows
                    if type(row["conditions"][name]) is type(value)
                    and row["conditions"][name] == value
                ]
        return rows

    def _curated_result(self, kind, rows, filters, context):
        details = {
            key: copy.deepcopy(value)
            for key, value in context.items()
            if key not in {"scope_key", "location_id", "city"}
        }
        details.update(filters=copy.deepcopy(filters), requested_filters=copy.deepcopy(filters))
        now = self.clock()
        active = [
            row
            for row in rows
            if datetime.fromisoformat(row["valid_from"]).timestamp()
            <= now
            < datetime.fromisoformat(row["valid_until"]).timestamp()
        ]
        if not active:
            expired = all(
                now >= datetime.fromisoformat(row["valid_until"]).timestamp() for row in rows
            )
            return self._result(
                "expired" if expired else "no_data",
                reason="no_current_business_data",
                unavailable=[
                    {key: row[key] for key in ("id", "valid_from", "valid_until")} for row in rows
                ],
                **details,
            )
        items = [self._record_item(row) for row in active]
        details["assumed_filters"] = [copy.deepcopy(row["conditions"]) for row in active]
        if kind == "financial_product":
            details.update(
                requested_product_id=filters.get("product_id"), effective_product_id=items[0]["id"]
            )
        return self._result("success", items=items, **details)

    @staticmethod
    def _record_item(row):
        item = copy.deepcopy(row["payload"])
        item.update(
            {
                key: copy.deepcopy(row[key])
                for key in (
                    "id",
                    "kind",
                    "location_id",
                    "source_type",
                    "source",
                    "valid_from",
                    "valid_until",
                    "updated_at",
                )
            }
        )
        item["eligibility_filters"] = copy.deepcopy(row["conditions"])
        item["generated"] = False
        return item

    @staticmethod
    def _link_offer_product(result, requested_id, product):
        effective_id = product["data"]["effective_product_id"]
        result["data"].update(requested_product_id=requested_id, effective_product_id=effective_id)
        if result["data"]["generated"]:
            for item in result["data"]["items"]:
                item["product_id"] = effective_id
            if requested_id != effective_id:
                result["data"]["fallback_reason"] = "unknown_product_id"

    def estimate_financing_cost(self, product_id, amount, term_months):
        """仅按目录中已披露的条款计算，使用十进制金额并精确到分，不生成个人报价。"""
        result = self.query("financial_product", product_id=product_id)
        if result["status"] != "success":
            return result
        product = result["data"]["items"][0]
        if product.get("supports_cost_estimate") is False:
            return self._result(
                "needs_clarification",
                reason="product_has_no_financing_terms",
                requested_product_id=product_id,
                effective_product_id=product["id"],
            )
        limits = product["amount_range_yuan"]
        if (
            type(amount) not in (int, float)
            or not math.isfinite(amount)
            or amount < limits["min"]
            or amount > limits["max"]
            or type(term_months) is not int
            or term_months not in product["term_months"]
        ):
            return self._result(
                "needs_clarification",
                reason="amount_or_term_outside_example",
                amount_range_yuan=limits,
                term_months=product["term_months"],
            )
        principal = Decimal(str(amount))
        if principal != principal.quantize(Decimal("0.01")):
            return self._result("needs_clarification", reason="amount_requires_cent_precision")
        rate = Decimal(str(product["annual_rate_percent"])) / Decimal(100)
        interest = (principal * rate * Decimal(term_months) / Decimal(12)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        fee = Decimal(str(product["service_fee_yuan"])).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        result["data"]["calculation"] = {
            "product_id": product["id"],
            "requested_product_id": product_id,
            "principal_yuan": float(principal),
            "term_months": term_months,
            "annual_rate_percent": product["annual_rate_percent"],
            "interest_yuan": float(interest),
            "service_fee_yuan": float(fee),
            "total_cost_yuan": float(interest + fee),
            "total_repayment_yuan": float(principal + interest + fee),
            "repayment_method": product["repayment_method"],
            "formula": "利息=本金×年利率×月数÷12；到期应还=本金+利息+服务费",
            "rounding": "人民币分，四舍五入",
            "assumptions": product["assumptions"],
            "personal_quote": False,
        }
        return result
