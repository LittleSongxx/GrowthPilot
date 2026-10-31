"""离线业务契约检查：四类场景工具集、可追溯查询、客户记忆和技能。

不请求模型、不操作外部业务状态，也不推测对话质量分数。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from backend.agent_harness import BackendService, load_scenario
from backend.database import DatabaseConfig, temporary_database
from backend.paths import portable_data, resolve_project_path
from backend.settings import Settings

from ..common.paths import PROJECT_ROOT
from .output_audit import audit_visible

# 已移除的外部业务控制能力不能通过场景配置重新注册。
RETIRED_TOOLS = {
    "query_progress", "prepare_guidance", "deliver_guidance", "submit_action",
    "book_consultation", "query_operation", "stop_contact", "end_session", "schedule_callback",
}

EXAMPLES = {
    "meituan_rider": [
        ("search_nearby_stations", {"location": "北京朝阳区望京", "delivery_type": "众包"}),
        ("query_delivery_pay", {"area": "北京朝阳区望京", "delivery_type": "众包"}),
        ("search_vehicle_rentals", {"location": "北京朝阳区望京"}),
    ],
    "didi_driver": [
        ("search_driver_service_points", {"location": "北京朝阳区望京"}),
        ("query_driver_earnings", {"area": "北京朝阳区望京", "service_type": "快车"}),
        ("search_car_rentals", {"location": "北京朝阳区望京"}),
    ],
    "meituan_merchant": [
        ("query_delivery_options", {"area": "北京朝阳区望京", "store_category": "餐饮"}),
        ("query_merchant_fee_plans", {"area": "北京朝阳区望京", "store_category": "餐饮",
                                      "delivery_mode": "平台配送"}),
        ("query_merchant_offers", {"area": "北京朝阳区望京", "store_category": "餐饮",
                                   "is_new_merchant": True}),
    ],
    "financial_consultation": [
        ("query_product_options", {"purpose": "了解借款费用"}),
        ("query_financial_offers", {"product_id": "catalog_flexible_loan", "is_new_user": True}),
        ("estimate_financing_cost", {"product_id": "catalog_flexible_loan", "amount": 10000,
                                     "term_months": 12}),
    ],
}

BUDGET_EXAMPLES = {
    "meituan_rider": [("estimate_work_budget", {
        "work_days": 8, "monthly_fixed_cost": 400, "daily_variable_cost": 10,
        "refundable_deposit": 500,
    })],
    "didi_driver": [("estimate_work_budget", {
        "work_days": 20, "monthly_fixed_cost": 3600, "daily_variable_cost": 50,
        "refundable_deposit": 6000, "monthly_income": 10000,
    })],
    "meituan_merchant": [("estimate_order_contribution", {
        "received_amount": 25, "fee_base": 30, "service_fee_percent": 6,
        "delivery_cost": 4, "goods_cost": 10, "packaging_cost": 2, "other_variable_cost": 1,
    })],
}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


async def check_end_call(conversation, schemas):
    """验证宿主提供的告别工具，实际挂断仍由播放完成后的宿主流程执行。"""
    from agentscope.message import ToolResultState

    schema = next(row["function"] for row in schemas if row["function"]["name"] == "end_call")
    parameters = schema["parameters"]
    require(set(parameters.get("required", [])) == {"message"}, "告别工具必须要求完整告别语")
    require(set(parameters.get("properties", {})) == {"message"}, "告别工具只接受告别语参数")
    message_schema = parameters["properties"]["message"]
    require(message_schema.get("type") == "string", "告别语必须是字符串")
    require(message_schema.get("minLength") == 1 and message_schema.get("pattern") == r"\S",
            "告别语不能是空字符串或只有空白")
    goodbye = "好的，感谢您的时间，祝您生活愉快，再见。"
    result = await conversation.tools["end_call"](message=goodbye)
    require(result.state == ToolResultState.SUCCESS, "活跃通话必须允许提交告别请求")
    require([block.text for block in result.content] == [goodbye], "工具结果必须保留完整告别语")
    require(conversation.end_call_message == goodbye and conversation.end_after_playback,
            "告别请求必须保留原文，并标记播放完成后挂断")
    require(conversation.service.session(conversation.ctx)["state"] == "active",
            "提交告别请求不能在播放完成前关闭通话")
    await conversation.stop("contract_complete")
    require(conversation.service.session(conversation.ctx)["state"] == "closed",
            "宿主执行挂断后必须关闭会话")
    denied = await conversation.tools["end_call"](message="这条迟到的告别不应执行。")
    require(denied.state == ToolResultState.DENIED, "已关闭会话必须拒绝新的告别请求")
    require(conversation.end_call_message == goodbye, "拒绝的请求不能覆盖已保存的告别语")
    return {
        "message": goodbye, "complete_message_preserved": True,
        "end_after_playback": True, "closed_session_denied": True,
    }


def worker_read(db, scenario_path, customer, workspace):
    service = BackendService(db, load_scenario(scenario_path), workspace_id=workspace)
    try:
        ctx = service.create_session(customer, run_id="memory-worker")
        result = service.read_memory(ctx)
        service.end_session(ctx, "worker_complete")
        return {"worker_pid": os.getpid(), "result": result}
    finally:
        service.close()


async def experiments(directory):
    """每次契约检查使用独立 schema，跨进程验证完成后自动清理。"""
    with temporary_database(Settings.from_env().app_db, prefix="growth_contracts") as database:
        return await _experiments(directory, database)


async def _experiments(directory, database):
    from agentscope.state import AgentState

    from backend.agent_harness.agent import FRAMEWORK_COMMIT, create_toolkit
    from backend.agent_harness.business_data import BusinessCatalog
    from backend.agent_harness.runtime.session_manager import SessionManager

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # 使用固定的参考日期，确保契约回归检查可以复现；运行时工具
    # 仍按实际当前日期校验，因此此检查不代表实时有效性测试。
    reference_time = datetime.fromisoformat("2026-10-29T12:00:00+08:00").timestamp()
    settings = Settings(PROJECT_ROOT, directory, database)
    services = {
        sid: BackendService(
            settings.app_db, load_scenario(PROJECT_ROOT / f"backend/scenarios/{sid}/scenario.yaml"),
            workspace_id="business-contracts",
        ) for sid in EXAMPLES
    }
    host = SessionManager(settings, services)
    report = {
        "kind": "offline_business_contracts", "model_requests": 0,
        "framework_commit": FRAMEWORK_COMMIT, "reference_date": "2026-10-29",
        "scope": "工具注册、查询边界、短信回执、告别挂断、记忆隔离与原生 Skill 工程契约",
        "dialogue_quality": "not_evaluated", "external_conversion": "not_measured",
        "scenarios": {}, "checks": {},
    }
    try:
        for sid, catalog_examples in EXAMPLES.items():
            examples = catalog_examples + BUDGET_EXAMPLES.get(sid, [])
            conversation = await host.create(sid, "contract-" + sid)
            conversation.catalog = BusinessCatalog.from_scenario(
                conversation.service.scenario, clock=lambda: reference_time,
            )
            toolkit = create_toolkit(conversation.tools, conversation.service.scenario)
            schemas = await toolkit.get_tool_schemas()
            names = {row["function"]["name"] for row in schemas}
            expected = {name for name, _ in examples} | {
                "search_policy", "send_download_sms", "end_call", "Skill",
            }
            require(names == expected, f"{sid}: unexpected model-visible tool set {names}")
            require(not names & RETIRED_TOOLS, "Retired technical controls must not be tools")
            before = conversation.service.snapshot(conversation.ctx)
            results = []
            for name, arguments in examples:
                result = await conversation.tools[name](**arguments)
                require(result["status"] == "success", f"{sid}/{name}: expected catalog match: {result}")
                require(not audit_visible(result, business_result=True), "Model-visible business result leaked internal language")
                if name in {"estimate_work_budget", "estimate_order_contribution"}:
                    require(bool(result.get("data", {}).get("calculation")),
                            "Budget tools must return explicit conditional calculations")
                    results.append({"tool": name, "arguments": arguments, "result": result})
                    continue
                audits = [event for event in host.store.events(conversation.id, limit=3000)
                          if event["type"] == "business_query_audit" and event["data"].get("name") == name]
                require(bool(audits), "Business query must preserve raw audit provenance")
                raw_result = audits[-1]["data"]["result"]
                data = raw_result["data"]
                require(data.get("source_type") == "local_catalog", "Internal source must remain auditable")
                require(bool(raw_result.get("evidence")), "Internal query audit needs provenance")
                require(bool(data.get("items")), "Successful query must contain selected source records")
                for item in data["items"]:
                    require(item.get("source_type") == "local_catalog", "Internal item source must be preserved")
                    require(all(item.get(key) for key in ("source", "valid_from", "valid_until")),
                            "Item provenance and validity are mandatory")
                results.append({"tool": name, "arguments": arguments, "result": result, "internal_audit": raw_result})
            after = conversation.service.snapshot(conversation.ctx)
            require(before["memory"] == after["memory"], "Business queries cannot change customer memory")
            require(not {"progress", "cards", "operations"} & set(after), "No external control state in snapshot")
            sms = await conversation.tools["send_download_sms"]()
            require(sms["status"] == "success", "Download SMS must return a send receipt")
            require(sms == await conversation.tools["send_download_sms"](),
                    "Repeated SMS calls must reuse the same receipt")
            sms_audits = [row for row in conversation.service.trace(conversation.ctx)
                          if row["kind"] == "download_sms_sent"]
            require(len(sms_audits) == 1, "Download SMS must be recorded only once per session")
            require(not audit_visible(sms, business_result=True),
                    "SMS receipt must not expose internal transport details")
            foreign = "query_product_options" if sid != "financial_consultation" else "search_nearby_stations"
            require(foreign not in conversation.tools, "Foreign scenario tools must not be registered")
            loaded_ids = []
            for skill in services[sid].scenario["skills"]:
                loaded = await toolkit.builtin_skill_viewer.tool(
                    skill=skill["id"], _agent_state=AgentState()
                )
                require(bool(loaded.content), "Declared Skill must load through native viewer")
                loaded_ids.append(skill["id"])
            after_names = {row["function"]["name"] for row in await toolkit.get_tool_schemas()}
            require(names == after_names, "Skill loading cannot expand authority")
            report["scenarios"][sid] = {
                "tool_names": sorted(names), "queries": results,
                "cross_scenario_denied": True, "skills_loaded": loaded_ids,
                "memory_unchanged_by_query": True,
                "download_sms": sms,
                "end_call": await check_end_call(conversation, schemas),
            }

        rider = await host.create("meituan_rider", "query-boundaries")
        rider.catalog = BusinessCatalog.from_scenario(
            rider.service.scenario, clock=lambda: reference_time,
        )
        city = await rider.tools["search_nearby_stations"](location="北京")
        changsha = await rider.tools["query_delivery_pay"](area="我现在在长沙", delivery_type="众包")
        generic = await rider.tools["query_delivery_pay"](area="青溪镇", delivery_type="众包")
        ambiguous = await rider.tools["search_nearby_stations"](location="附近")
        require(city["status"] == "success" and city["data"]["coverage_level"] == "city",
                "City-only input can use a city-level business result")
        require(changsha["status"] == "success" and changsha["data"]["coverage_level"] == "city",
                "Changsha must have a usable business result")
        require(generic["status"] == "success" and generic["data"]["coverage_level"] == "generic",
                "An unlisted town must receive a usable general business result")
        require(ambiguous["status"] == "needs_clarification", "A genuinely missing location needs clarification")
        report["checks"]["query_boundaries"] = {
            "city": city, "changsha": changsha, "generic": generic, "missing_location": ambiguous,
        }
        await rider.stop("boundaries_complete")

        service = services["didi_driver"]
        first = service.create_session("memory-example", run_id="memory-first")
        msg = service.add_message(first, "我在厦门，请记住厦门。")["data"]["message_id"]
        initial = service.write_memory(first, "city", "厦门", msg, "厦门")
        require(initial["status"] == "success", "Memory needs actual source evidence")
        msg = service.add_message(first, "更正：我现在在福州。")["data"]["message_id"]
        stale = service.write_memory(first, "city", "福州", msg, "福州", expected_version=0)
        require(stale["status"] == "conflict", "Stale correction must not overwrite current memory")
        corrected = service.write_memory(first, "city", "福州", msg, "福州", expected_version=1)
        require(corrected["status"] == "success", "Versioned correction must persist")
        service.end_session(first, "restart")
        completed = await asyncio.to_thread(subprocess.run, [
            sys.executable, "-m", "evaluation.checks.contracts", "--memory-worker", settings.app_db.schema,
            "backend/scenarios/didi_driver/scenario.yaml",
            "memory-example", "business-contracts",
        ], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True, timeout=30,
            env={**os.environ, "DATABASE_URL": settings.app_db.dsn})
        reloaded = json.loads(completed.stdout)
        require(reloaded["worker_pid"] != os.getpid(), "Persistence must cross a process boundary")
        require(reloaded["result"]["data"]["fields"][0]["value"] == "福州", "Corrected memory must survive restart")
        another = service.create_session("another-customer")
        require(service.read_memory(another)["status"] == "no_data", "Customer scopes cannot leak")
        service.end_session(another, "done")
        other_scenario = services["meituan_rider"]
        ctx = other_scenario.create_session("memory-example")
        require(other_scenario.read_memory(ctx)["status"] == "no_data", "Scenario scopes cannot leak")
        other_scenario.end_session(ctx, "done")
        report["checks"]["memory_persistence_and_isolation"] = {
            "cross_process_persistence": True, "stale_correction_rejected": True,
            "corrected_value": "福州", "customer_and_scenario_isolation": True,
        }
        report["passed"] = True
        return report
    finally:
        await host.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=resolve_project_path)
    parser.add_argument("--memory-worker", nargs=4)
    args = parser.parse_args()
    if args.memory_worker:
        schema, scenario_path, customer, workspace = args.memory_worker
        # 凭据由父进程环境传递，进程参数和失败异常中只出现 schema。
        database = DatabaseConfig(DatabaseConfig.from_env().dsn, schema)
        print(json.dumps(portable_data(worker_read(
            database, resolve_project_path(scenario_path), customer, workspace,
        )), ensure_ascii=False))
        return
    if args.output and args.output.exists():
        parser.error("Use a fresh output path; historical results must not be overwritten")
    with tempfile.TemporaryDirectory(prefix="growth-contracts-") as directory:
        report = asyncio.run(experiments(directory))
    text = json.dumps(portable_data(report), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
