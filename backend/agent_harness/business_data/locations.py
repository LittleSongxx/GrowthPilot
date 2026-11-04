"""将客户提供的地点解析为具体地区、已知城市或明确标注的兜底结果。"""

import re

# 口语里常见“湖南长沙”“广东深圳”这类不带“省”的写法；列出省级名称后，
# 省名本身也成为城市别名的合法前缀，不再依赖“省”字。
PROVINCE_NAMES = (
    "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建",
    "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州",
    "云南", "陕西", "甘肃", "青海", "台湾", "香港", "澳门",
)
CITY_PREFIX_ENDINGS = (
    "省",
    "自治区",
    "自治州",
    *PROVINCE_NAMES,
    "广西",
    "新疆",
    "宁夏",
    "西藏",
    "内蒙古",
    "蓉城",
    "在",
    "到",
    "从",
    "和",
    "与",
    "或",
    "或者",
    "还是",
    "以及",
    "跟",
    "vs",
    "？",
    "?",
    "；",
    ";",
    "、",
    "，",
    ",",
    "/",
    " ",
)


class LocationResolver:
    def __init__(self, locations, city_profiles, result):
        self.locations = locations
        self.profiles = city_profiles
        self.aliases = tuple(alias for profile in city_profiles for alias in profile["aliases"])
        self.result = result

    def resolve(self, location):
        details = {"requested_location": location, "requested_filters": {}}
        if not isinstance(location, str) or not location.strip() or len(location) > 200:
            return None, self.result("needs_clarification", reason="location_required", **details)
        cleaned = re.sub(r"\s+", "", location.strip())
        cleaned = re.sub(
            r"^(?:我现在住在|我现在在|我目前在|我住在|我住|我在|目前在|现在在|这边是|我家在|在)",
            "",
            cleaned,
        )
        if cleaned in {"附近", "我附近", "这里", "这边", "本地", "当地", "附近有吗", "我的附近"}:
            return None, self.result("needs_clarification", reason="location_required", **details)
        cities = self._matching_cities(cleaned)
        if len(cities) > 1:
            return None, self.result(
                "needs_clarification",
                reason="conflicting_locations",
                candidate_cities=sorted(cities),
                **details,
            )
        if cities:
            return self._known_city(next(iter(cities)), cleaned, details)
        localities = self._matching_localities(cleaned)
        if len(localities) > 1:
            return None, self.result(
                "needs_clarification",
                reason="conflicting_locations",
                candidate_cities=sorted({item["city"] for item in localities}),
                **details,
            )
        if localities:
            return self._curated_locality(localities[0], details), None
        return self._unknown_location(cleaned, details)

    def _matching_cities(self, text):
        cities = set()
        for profile in self.profiles:
            for alias in profile["aliases"]:
                for match in re.finditer(re.escape(alias), text):
                    before, after = text[: match.start()], text[match.end() :]
                    street = re.match(r"(?:[东西南北中]?(?:路|街)|大街|大道|镇|乡|村)", after)
                    boundary = not before or before.endswith(CITY_PREFIX_ENDINGS)
                    adjacent_city = before.endswith(self.aliases)
                    if (boundary or adjacent_city) and not street:
                        cities.add(profile["city"])
        return cities

    def _matching_localities(self, text):
        """城市未命中时按商圈名解析；“望京路”这类街道名不能当成商圈。"""
        found = {}
        for locality in self.locations:
            names = {locality.get("locality") or "", *(locality.get("aliases") or [])}
            for name in names:
                if not name:
                    continue
                for match in re.finditer(re.escape(name), text):
                    after = text[match.end() :]
                    if re.match(r"(?:[东西南北中]?(?:路|街)|大街|大道|镇|乡|村)", after):
                        continue
                    found[locality["id"]] = locality
        return list(found.values())

    def _curated_locality(self, locality, details):
        return {
            **details,
            "scope_key": locality["id"],
            "location_id": locality["id"],
            "city": locality["city"],
            "matched_location": locality["label"],
            "coverage_level": "locality",
            "match_type": "curated_locality",
            "fallback_reason": None,
        }

    def _known_city(self, city, text, details):
        localities = [
            loc for loc in self.locations if loc["city"] == city and loc["locality"] in text
        ]
        if len(localities) == 1:
            locality = localities[0]
            districts = re.findall(r"(?:朝阳区|海淀区)", text)
            if districts and locality["district"] not in districts:
                return None, self.result(
                    "needs_clarification", reason="conflicting_locations", **details
                )
            return {
                **details,
                "scope_key": locality["id"],
                "location_id": locality["id"],
                "city": city,
                "matched_location": locality["label"],
                "coverage_level": "locality",
                "match_type": "curated_locality",
                "fallback_reason": None,
            }, None
        context = {
            **details,
            "scope_key": "city:" + city,
            "location_id": None,
            "city": city,
            "matched_location": city,
            "coverage_level": "city",
            "match_type": "city_profile",
            "fallback_reason": "locality_not_curated",
        }
        if len(localities) > 1:
            context["location_note"] = "具体商圈尚未确定，本次按城市范围提供方案"
        return context, None

    def _unknown_location(self, text, details):
        # 明确保留尚未核实的地点，不借用其他城市的记录。
        label = re.split(r"[，,。；;？！?!]", text, maxsplit=1)[0]
        label = re.sub(r"(?:附近|这边|那边|一带|周边)+$", "", label)
        if len(label) < 2 or not re.search(r"[\w\u4e00-\u9fff]", label):
            return None, self.result("needs_clarification", reason="location_required", **details)
        return {
            **details,
            "scope_key": "generic:" + label,
            "location_id": None,
            "city": None,
            "matched_location": label,
            "coverage_level": "generic",
            "match_type": "generic_fallback",
            "fallback_reason": "city_not_profiled",
        }, None
