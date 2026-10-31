"""检测内部用语，不修改用户输入、政策原文或业务事实。

结构化 ID 仅作为工具调用标识使用，不得直接播报。
"""

from __future__ import annotations

import re

INTERNAL_FIELDS = {
    "source_type", "disclosure", "generated", "is_fallback", "match_type", "fallback_reason",
    "template_provenance", "template_id", "variant_id", "profile_id", "pricing_basis",
}
VERBATIM_INPUT_FIELDS = {"requested_location", "requested_filters", "filters", "quote", "raw_quote"}
OPAQUE_FIELDS = {"id", "station_id", "product_id", "requested_product_id", "effective_product_id"}
# 集中维护回归检查词表；测试引用此处，避免复制旧文案。
INTERNAL_LANGUAGE_RULES = (
    ("教学", ("数据", "环境", "模式", "模板", "示例", "产品", "方案")),
    ("模拟", ("数据", "环境", "电话", "地址", "站点", "客户", "通话")),
    ("沙箱", ("数据", "环境", "服务", "接口", "业务")),
)
INTERNAL_LANGUAGE_FRAGMENTS = tuple(word for word, _ in INTERNAL_LANGUAGE_RULES)
PATTERNS = (
    ("internal_environment_language", re.compile("|".join(
        re.escape(word) + "(?:" + "|".join(map(re.escape, suffixes)) + ")"
        for word, suffixes in INTERNAL_LANGUAGE_RULES))),
    ("fabricated_contact_language", re.compile(r"(?:虚拟|虚构)(?:的)?(?:电话|号码|地址|站点|联系人|门店)")),
    ("internal_implementation_token", re.compile(
        r"\b(?:local_catalog|generic_fallback|city_profile|curated_locality)\b", re.IGNORECASE)),
    ("internal_handle", re.compile(r"\bcatalog_[a-z0-9_]+\b", re.IGNORECASE)),
)


def audit_text(text, path="reply"):
    """返回内部术语出现的准确位置，不修改或修复文本。"""
    findings = []
    for rule, pattern in PATTERNS:
        for match in pattern.finditer(text):
            findings.append({"path": path, "rule": rule, "match": match.group(),
                             "start": match.start(), "end": match.end()})
    return findings


def audit_visible(value, *, business_result=False, path="tool", key=None):
    """检查自然语言值，以及仅供内部业务诊断使用的字段名。

    保留政策来源和证据字段。工具标识字段中的 ID 用于跨工具调用，
    但同样的 ID 出现在自然语言正文中时，检查应判为不通过。
    """
    findings = []
    if key in VERBATIM_INPUT_FIELDS:
        return findings
    if isinstance(value, dict):
        for name, child in value.items():
            child_path = f"{path}.{name}"
            if business_result and name in INTERNAL_FIELDS:
                findings.append({"path": child_path, "rule": "internal_field", "match": name})
            findings.extend(audit_visible(child, business_result=business_result,
                                          path=child_path, key=name))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(audit_visible(child, business_result=business_result,
                                          path=f"{path}[{index}]", key=key))
    elif isinstance(value, str) and key not in OPAQUE_FIELDS:
        findings.extend(audit_text(value, path))
    return findings


def audit_trace(trace, business_tools):
    findings = []
    for turn in trace.get("turns", []):
        number = turn["turn"]
        if "reply" in turn:
            findings.extend(audit_text(turn["reply"], f"reply/{number}"))
        # 明确排除原始审计事件，因为它们不会作为文本提供给模型。
        for event in turn.get("tool_events", []):
            if event.get("type") == "tool_result":
                payload = event.get("data", {})
                if payload.get("name") not in business_tools:
                    continue
                findings.extend(audit_visible(payload.get("result", {}),
                                              business_result=payload.get("name") in business_tools,
                                              path=f"tool/{event['seq']}"))
    return {"passed": not findings, "findings": findings,
            "scope": "internal_terminology_only_not_semantic_quality"}
