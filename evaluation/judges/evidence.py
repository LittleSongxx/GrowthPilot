"""从真实对话抽取可引用证据，避免复制完整轨迹和隐藏审计。"""

from __future__ import annotations

import json

BUSINESS_TOOLS = {
    "search_nearby_stations", "query_delivery_pay", "search_vehicle_rentals",
    "search_driver_service_points", "query_driver_earnings", "search_car_rentals",
    "query_delivery_options", "query_merchant_fee_plans", "query_merchant_offers",
    "query_product_options", "query_financial_offers", "estimate_financing_cost",
    "estimate_work_budget", "estimate_order_contribution",
}
FACT_SOURCE_KINDS = {
    "official_policy", "business_result", "business_calculation", "action_receipt",
    "tool_limitation",
}
POLICY_CONTEXT_FIELDS = (
    "title", "region", "section", "partial_sentence", "document_type", "business_type",
    "published_at", "valid_from", "valid_until", "document_id", "content_hash", "fetched_at",
    "pdf_pages",
)
BUSINESS_CONTEXT_FIELDS = (
    "requested_product_id", "effective_product_id", "requested_filters", "assumed_filters",
    "requested_location", "matched_location", "coverage_level", "unverified_conditions",
)
# 非成功返回只能证明本次限制，残留的条目、计算或回执不能升级成已完成结果。
SUCCESS_DATA_FIELDS = {"items", "clauses", "calculation", "delivery_status", "message_id"}


def readable_fields(value, path=""):
    """把结构化业务值逐项展开，引用单项时无需复制 JSON 标点或转义字符。"""
    if isinstance(value, dict) and value:
        return "\n".join(
            readable_fields(child, f"{path}.{key}" if path else str(key))
            for key, child in value.items()
        )
    if isinstance(value, list) and value:
        return "\n".join(
            readable_fields(child, f"{path}[{index}]") for index, child in enumerate(value)
        )
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return f"{path}: {text}" if path else text


def evidence_for(trace):
    """保留首次实际可见时间；同一来源重复检索时只保存一次正文。"""
    evidence, source_refs, seen_turns = {}, {}, set()

    def add(ref, number, kind, text, **metadata):
        if not isinstance(text, str) or not text.strip():
            return
        if kind in FACT_SOURCE_KINDS:
            identity = json.dumps([kind, text, metadata], ensure_ascii=False, sort_keys=True)
            if identity in source_refs:
                return
            source_refs[identity] = ref
        if ref in evidence:
            raise ValueError("证据标识重复")
        evidence[ref] = {"turn": number, "kind": kind, "text": text, **metadata}

    # 这是本通开场前真实注入 Agent 的画像；它只证明系统保留了客户历史自述。
    # 不附会具体来自哪一通，也不升级为政策、核验结果或本次客户同意。
    profile = trace.get("initial_profile", {})
    if not isinstance(profile, dict):
        raise TypeError("开场画像必须是字段对象")
    for field, item in profile.items():
        if not isinstance(field, str) or not isinstance(item, dict):
            raise TypeError("开场画像字段必须包含结构化值与状态")
        add(
            f"profile/{field}", -1, "historical_customer_profile",
            readable_fields({"field": field, "value": item.get("value"), "status": item.get("status")}),
            visible_at="current_call_start", call_number=trace.get("repeat", 1),
            provenance="frozen_profile_from_historical_customer_statements",
            supports="recorded_customer_statement_only",
        )

    turns = trace.get("turns")
    if not isinstance(turns, list):
        raise TypeError("轨迹必须包含轮次列表")
    previous = -1
    for turn in turns:
        if not isinstance(turn, dict):
            raise TypeError("轮次必须是对象")
        number = turn.get("turn")
        if type(number) is not int or number < 0 or number in seen_turns or number <= previous:
            raise ValueError("轮次必须是递增且唯一的非负整数")
        previous = number
        seen_turns.add(number)
        if turn.get("origin", "customer") == "customer":
            add(f"user/{number}", number, "customer_statement", turn.get("user_message"))
        if "reply" in turn:
            if not isinstance(turn["reply"], str) or not turn["reply"].strip():
                raise ValueError("实际回复为空，不能进行质量评审")
            add(f"reply/{number}", number, "agent_reply", turn["reply"])
        for event in turn.get("tool_events", []):
            payload = event.get("data", {})
            name, seq = payload.get("name"), event.get("seq")
            # Skill 和 Memory 是内部辅助材料，不能成为政策或个人审批状态的来源。
            if name not in BUSINESS_TOOLS | {"search_policy", "send_download_sms"}:
                continue
            if type(seq) is not int:
                raise ValueError("工具证据缺少有效事件序号")
            if event.get("type") == "tool_start":
                add(f"call/{seq}", number, "tool_call", readable_fields({
                    "tool": name, "arguments": payload.get("arguments", {}),
                }))
                continue
            if event.get("type") != "tool_result":
                continue
            result = payload.get("result", {})
            if not isinstance(result, dict):
                continue
            data = result.get("data", {})
            if not isinstance(data, dict):
                continue
            add(f"tool/{seq}", number, "tool_outcome", readable_fields({
                "tool": name, "state": payload.get("state"), "status": result.get("status"),
            }))
            if payload.get("state") != "success" or result.get("status") != "success":
                # 范围、可选期限和缺失条件也是 Agent 可见事实，但不证明业务执行成功。
                limitation = {key: value for key, value in data.items()
                              if key not in SUCCESS_DATA_FIELDS}
                add(f"source/{seq}/limitation", number, "tool_limitation", readable_fields({
                    "state": payload.get("state"), "status": result.get("status"),
                    "data": limitation,
                }), tool=name, supports="reported_constraints_or_failure_only")
                continue
            if name == "search_policy":
                for index, clause in enumerate(data.get("clauses", [])):
                    if not isinstance(clause, dict) or (
                        clause.get("scenario_id") != trace.get("scenario_id")
                        or clause.get("id") not in result.get("evidence", [])
                        or not str(clause.get("url", "")).startswith("https://")
                    ):
                        continue
                    # 上下文限制正文的适用范围；不将整理提示伪装成政策原文。
                    context = {key: clause[key] for key in POLICY_CONTEXT_FIELDS if key in clause}
                    add(f"source/{seq}/{index}", number, "official_policy", clause.get("text"),
                        url=clause["url"], context=context)
            elif name in BUSINESS_TOOLS:
                context = {key: data[key] for key in BUSINESS_CONTEXT_FIELDS if key in data}
                if context:
                    add(f"source/{seq}/context", number, "business_result",
                        readable_fields(context), tool=name)
                for index, item in enumerate(data.get("items", [])):
                    if isinstance(item, dict) and item.get("id"):
                        add(f"source/{seq}/{index}", number, "business_result",
                            readable_fields(item), tool=name)
                if isinstance(data.get("calculation"), dict) and data["calculation"]:
                    add(f"source/{seq}/calculation", number, "business_calculation",
                        readable_fields(data["calculation"]), tool=name)
            elif (name == "send_download_sms" and data.get("delivery_status") == "sent"
                  and data.get("message_id") in result.get("evidence", [])):
                add(f"source/{seq}/receipt", number, "action_receipt", readable_fields(data),
                    tool=name)
    return evidence



def _turns_for_judge(trace, evidence):
    """将新取得的依据放在当轮对话旁，减少裁判跨表查找。"""
    turns = []
    for turn in trace["turns"]:
        number = turn["turn"]
        row = {"turn": number}
        if turn.get("origin", "customer") == "customer" and turn.get("user_message"):
            row["customer"] = turn["user_message"]
        materials = []
        for source in evidence.values():
            if source["turn"] != number or source["kind"] not in FACT_SOURCE_KINDS:
                continue
            material = {"text": source["text"]}
            if source.get("tool"):
                material["tool"] = source["tool"]
            if source["kind"] == "official_policy":
                material["source"] = {
                    "url": source["url"],
                    **{key: value for key, value in source.get("context", {}).items()
                       if key not in {"document_id", "content_hash", "fetched_at"}},
                }
            materials.append(material)
        if materials:
            row["materials"] = materials
        if "reply" in turn:
            row["agent"] = turn["reply"]
        turns.append(row)
    return turns


def judge_payload(trace, case):
    """保留案例目标与本通可见依据，由裁判结合客户当前状态评价复访进展。"""
    goal = case.get("goal")
    if not isinstance(goal, dict) or not isinstance(goal.get("description"), str):
        raise TypeError("案例缺少明确目标")
    evidence = evidence_for(trace)
    if not any(row["kind"] == "agent_reply" for row in evidence.values()):
        raise ValueError("轨迹没有实际回复，不能进行质量评审")
    # 连续通话沿用原目标；不在代码里猜测历史完成项或抹掉尚未解决的条件。
    # 裁判依据实际开场记忆和本通原话判断，不读取客户模拟器的私有历史。
    objective = {
        "description": goal["description"],
        "success_conditions": goal.get("success_conditions", []),
        "call_number": trace.get("repeat", 1),
    }
    business_context = trace.get("business_context", {})
    if business_context:
        # 工具定义含面向 Agent 的执行指令，不属于裁判量表；业务回执仍按轮保留。
        objective["business_context"] = {
            key: business_context[key]
            for key in ("app_name", "agent_role")
            if key in business_context
        }
    return {
        "goal": objective,
        "memory_at_start": {
            field: {"value": item.get("value"), "status": item.get("status")}
            for field, item in trace.get("initial_profile", {}).items()
        },
        "turns": _turns_for_judge(trace, evidence),
    }
