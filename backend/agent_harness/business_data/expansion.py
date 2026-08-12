"""基于快照中的城市参数，以可复现的方式扩展业务方案。"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime

from .pricing import apply_prices

META = ("source_type", "source", "valid_from", "valid_until", "updated_at")
GENERIC = "__generic__"
GENERIC_PRODUCT_ID = "catalog_generic_financing"
CONTACT_KINDS = {"rider_station", "driver_service_point", "vehicle_rental", "car_rental"}


def entity_contact(entity_id, location):
    """根据实体标识生成稳定的联系方式，并在目录元数据中保留来源。"""
    seed = hashlib.sha256(("contacts-v1:" + entity_id + ":" + location).encode()).hexdigest()
    number = int(seed, 16)
    streets = ("兴业路", "新兴路", "创业路", "民和路", "富康路", "永安路", "顺达路", "长兴路")
    street = streets[(number >> 32) % len(streets)]
    building = (number >> 40) % 298 + 1
    room = (number >> 48) % 18 + 101
    return {
        "address": f"{location}{street}{building}号{room}室",
        "phone": "13" + f"{number % 1_000_000_000:09d}",
    }


def stable_id(prefix, *parts):
    body = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return prefix + "_" + hashlib.sha256(body.encode()).hexdigest()[:20]


class BusinessExpansion:
    """选择本地模板，并应用所查询城市的业务参数。"""

    def __init__(self, data, result, clock):
        self.data = data
        self.result = result
        self.clock = clock
        self.profiles = {profile["city"]: profile for profile in data["city_profiles"]}

    def is_information_purpose(self, purpose):
        return purpose and any(
            variant["assumed_filters"].get("purpose") == purpose
            and variant["payload"].get("supports_cost_estimate") is False
            for variant in self.data["templates"]["financial_product"]["payloads"]
        )

    def _active(self, value):
        return (
            datetime.fromisoformat(value["valid_from"]).timestamp()
            <= self.clock()
            < datetime.fromisoformat(value["valid_until"]).timestamp()
        )

    def _unavailable(self, sources, details):
        expired = all(
            self.clock() >= datetime.fromisoformat(source["valid_until"]).timestamp()
            for source in sources
        )
        return self.result(
            "expired" if expired else "no_data",
            reason="no_current_business_data",
            unavailable=[
                {k: source[k] for k in ("valid_from", "valid_until")} for source in sources
            ],
            **details,
        )

    def _select(self, kind, filters, template):
        if kind == "financial_product" and filters.get("product_id"):
            exact = [v for v in template["payloads"] if v["variant_id"] == filters["product_id"]]
            if exact:
                return exact, False
            return [v for v in template["payloads"] if v["variant_id"] == GENERIC_PRODUCT_ID], True
        selection_filters = dict(filters)
        if kind == "financial_product" and filters.get("purpose"):
            purpose = self._financial_purpose(filters["purpose"], template)
            if purpose is None:
                return [
                    variant
                    for variant in template["payloads"]
                    if variant["variant_id"] == "catalog_purpose_clarification"
                ], True
            selection_filters["purpose"] = purpose
        candidates = []
        for variant in template["payloads"]:
            assumptions = variant["assumed_filters"]
            score, generic, valid = 0, False, True
            for name, expected in assumptions.items():
                value = selection_filters.get(name)
                if name == "station_id":
                    continue
                if name == "product_id" and kind == "financial_offer":
                    continue
                if value is None:
                    score -= 1 if expected == GENERIC else 0
                elif expected == GENERIC:
                    generic = True
                elif type(expected) is type(value) and expected == value:
                    score += 1
                else:
                    valid = False
            if valid:
                candidates.append((score, generic, variant))
        if not candidates:
            return [], True
        top = max(row[0] for row in candidates)
        chosen = [row for row in candidates if row[0] == top]
        # 未指定的筛选维度存在多个选项时，优先展示假设条件更具体的备选方案。
        chosen.sort(key=lambda row: (row[1], row[2]["variant_id"]))
        return [row[2] for row in chosen[:4]], any(row[1] for row in chosen[:4])

    @staticmethod
    def _financial_purpose(purpose, template):
        known_purposes = {v["assumed_filters"].get("purpose") for v in template["payloads"]}
        explicit_nonborrowing = bool(
            re.search(
                r"(?:无|没有)(?:任何)?(?:借款|贷款|借钱)(?:需求|意愿|意向)?|"
                r"(?:不想|不愿|不要|不准备|不打算|不需要|没打算|没准备|暂不|暂时不|不)(?:去|再)?(?:借款|贷款|借钱)",
                purpose,
            )
        )
        financial_topic = any(
            word in purpose
            for word in ("费用", "利息", "利率", "计息", "本金", "还款", "借款", "贷款", "周转")
        )
        information_words = (
            "登录",
            "登陆",
            "注册",
            "账号安全",
            "信息管理",
            "信息安全",
            "理财",
            "账单",
        )
        if explicit_nonborrowing or (
            purpose not in known_purposes
            and not financial_topic
            and not any(word in purpose for word in information_words)
        ):
            return None
        for words, canonical in [
            (("登录", "登陆"), "登录"),
            (("注册",), "注册"),
            (("账号安全", "信息管理", "信息安全"), "安全与信息管理"),
            (("理财",), "理财"),
            (("账单",), "账单"),
        ]:
            if any(word in purpose for word in words):
                return canonical
        return purpose

    def query(self, kind, filters, context):
        template = self.data["templates"][kind]
        profile = self.profiles.get(context["city"], self.data["generic_profile"])
        details = {
            key: copy.deepcopy(value)
            for key, value in context.items()
            if key not in {"scope_key", "location_id", "city"}
        }
        details.update(requested_filters=copy.deepcopy(filters), generated=True, is_fallback=True)
        unavailable = [source for source in (template, profile) if not self._active(source)]
        if unavailable:
            return self._unavailable(unavailable, details)
        variants, generic_conditions = self._select(kind, filters, template)
        if not variants:
            return self.result("no_data", reason="no_matching_business_data", **details)
        self._describe_match(
            details, kind, filters, context, template, profile, variants, generic_conditions
        )
        items = [
            self._item(kind, filters, context, template, profile, variant) for variant in variants
        ]
        if (
            kind == "delivery_pay"
            and filters.get("station_id") is not None
            and any(item["station_id"] != filters["station_id"] for item in items)
        ):
            return self.result("needs_clarification", reason="station_scope_mismatch", **details)
        if kind == "financial_product":
            details["requested_product_id"] = filters.get("product_id")
            details["effective_product_id"] = items[0]["id"]
        return self.result("success", items=items, **details)

    @staticmethod
    def _describe_match(
        details, kind, filters, context, template, profile, variants, generic_conditions
    ):
        if context["match_type"] == "curated_locality" or generic_conditions:
            details["match_type"] = "generic_fallback"
            details["fallback_reason"] = "business_conditions_not_curated"
        if (
            kind == "financial_product"
            and filters.get("product_id")
            and variants[0]["variant_id"] != filters["product_id"]
        ):
            details["fallback_reason"] = "unknown_product_id"
        if kind.startswith("financial_"):
            details["match_type"] = "generic_fallback"
            details["coverage_level"] = "not_applicable"
        details["template_provenance"] = {
            "template_id": template["template_id"],
            "source": copy.deepcopy(template["source"]),
            "valid_from": template["valid_from"],
            "valid_until": template["valid_until"],
            "profile_id": profile["profile_id"],
            "profile_source": copy.deepcopy(profile["source"]),
            "profile_parameters": copy.deepcopy(profile["parameters"]),
        }
        details["assumed_filters"] = [copy.deepcopy(v["assumed_filters"]) for v in variants]
        details["unverified_conditions"] = [
            "服务范围和接待安排以服务方确认信息为准",
            "客户提供的条件尚未经过平台准入或账户资格核验",
        ]
        if generic_conditions:
            details["unverified_conditions"].append(
                "暂未查到完全对应的方案，以下按通用条件说明；不代表该车型、类目或用途已经符合要求"
            )

    def _item(self, kind, filters, context, template, profile, variant):
        item = copy.deepcopy(variant["payload"])
        apply_prices(kind, item, profile)
        assumptions = copy.deepcopy(variant["assumed_filters"])
        contact_conditions = assumptions
        item_id = stable_id("catalog_" + kind, context["scope_key"], variant["variant_id"])
        if kind in {"rider_station", "delivery_pay"}:
            delivery_type = assumptions.get("delivery_type", GENERIC)
            if delivery_type == GENERIC:
                delivery_type = "generic:" + str(filters.get("delivery_type", "unspecified"))
            contact_conditions = {"delivery_type": delivery_type}
            station_id = stable_id("catalog_station", context["scope_key"], delivery_type)
            if kind == "rider_station":
                item_id = station_id
            else:
                item["station_id"] = station_id
        if kind == "financial_product":
            item_id = variant["variant_id"]
        for key in META:
            item[key] = copy.deepcopy(template[key])
        item.update(
            id=item_id,
            kind=kind,
            location_id=context.get("location_id"),
            generated=True,
            template_id=template["template_id"],
            variant_id=variant["variant_id"],
            profile_id=profile["profile_id"],
            eligibility_filters=assumptions,
        )
        if not kind.startswith("financial_"):
            self._add_location(item, kind, context, contact_conditions)
        return item

    @staticmethod
    def _add_location(item, kind, context, contact_conditions):
        item["area"] = context["matched_location"]
        item["location_note"] = "按当前地区提供服务信息，距离未计算"
        item["distance_meters"] = None
        if kind in CONTACT_KINDS:
            if item.get("name") and not item["name"].startswith(context["matched_location"]):
                item["name"] = context["matched_location"] + item["name"]
            contact_key = stable_id("contact", kind, context["scope_key"], contact_conditions)
            item.update(entity_contact(contact_key, context["matched_location"]))
            item["address_note"] = "到访前请先通过联系电话确认接待时间和具体安排"
