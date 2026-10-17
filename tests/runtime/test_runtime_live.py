"""仅显式启用时才对真实服务供应商执行冒烟检查，并使用独立的全新存储。"""

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
from dataclasses import replace

import pytest
from agentscope.state import AgentState

from backend.agent_harness import BackendService, load_scenario
from backend.agent_harness.agent import FRAMEWORK_COMMIT
from backend.agent_harness.prompts import PROMPT_VERSION
from backend.agent_harness.rag.index import PolicyIndex
from backend.agent_harness.runtime.session_manager import SessionManager
from backend.agent_harness.runtime.store import RuntimeStore
from backend.database import close_database
from backend.settings import Settings
from tests.db import database_for


def _persisted_live_state(database, session_id, knowledge_dir):
    """重新从 PostgreSQL 读取全部验收证据，不调用模型或复用内存中的会话对象。"""
    store = RuntimeStore(database)
    runtime = store.get(session_id)
    scenario = BackendService.saved_scenario(database, runtime["snapshot_hash"])
    service = BackendService(database, scenario, read_only=True)
    try:
        context = service.get_session(session_id)
        # 检查点不仅需要有内容，还必须能还原成框架认识的有效状态。
        state = AgentState.model_validate(runtime["state"])
        assert state.context
        policy = PolicyIndex(knowledge_dir, database=database)
        return {
            "snapshot": service.snapshot(context),
            "profile_job": service.profiles.state(context),
            "runtime": runtime,
            "events": store.events(session_id, limit=10000),
            "corpus": policy.corpus(scenario.id),
        }
    finally:
        service.close()


@pytest.mark.skipif(
    os.environ.get("GROWTH_RUN_LIVE") != "1",
    reason="real-provider smoke requires explicit GROWTH_RUN_LIVE=1",
)
@pytest.mark.asyncio
async def test_real_text_skill_memory_business_query(tmp_path):
    original_settings = Settings.from_env()
    settings = replace(
        original_settings,
        runtime_dir=tmp_path,
        app_db=database_for(tmp_path / "application"),
    )
    scenario = load_scenario(settings.project_root / "backend/scenarios/didi_driver/scenario.yaml")
    knowledge_dir = original_settings.runtime_dir / "knowledge"
    # 复用真实 BGE 模型缓存，首次缺少缓存时由 build 准备；全部数据仍隔离在临时 schema。
    policy = PolicyIndex(
        knowledge_dir,
        database=settings.app_db,
    )
    corpus_path = settings.project_root / "evaluation/datasets/corpus/didi_driver.jsonl"
    chunks = [json.loads(line) for line in corpus_path.read_text().splitlines() if line.strip()]
    assert chunks and all(chunk["scenario_id"] == scenario.id for chunk in chunks)
    await asyncio.to_thread(policy.build, chunks, scenario_id=scenario.id)
    corpus = policy.corpus(scenario.id)
    assert corpus["chunk_count"] == len(chunks)
    service = BackendService(settings.app_db, scenario)
    host = SessionManager(settings, {scenario.id: service})
    host._policy_index = policy
    rt = await host.create(scenario.id, "runtime-smoke-driver")
    await host.profile_worker.close()
    start = time.monotonic()
    try:
        reply = await rt.message(
            "方便。我在北京朝阳区望京，想了解司机注册条件和附近服务点。请记住我的城市北京，并查看适用技能，查询后口头解答。"
        )
        await rt.stop()
        assert await host.profile_worker.run_once()
        assert service.profiles.state(rt.ctx)["status"] == "completed"
        events = host.store.events(rt.id, limit=10000)
        report = {
            "type": "real_text_model_new_project_smoke",
            "framework_commit": FRAMEWORK_COMMIT,
            "prompt_version": PROMPT_VERSION,
            "scenario": scenario.id,
            "snapshot_hash": scenario.snapshot_hash,
            "model": settings.agent_model,
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "reply": reply,
            "events": events,
            "state": service.snapshot(rt.ctx),
        }
        directory = settings.project_root / ".run/checks/live-dialogue-probes"
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / f"runtime-text-smoke-{PROMPT_VERSION}-{int(time.time())}.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print("Report:", output)
        print("Reply:", reply)
        calls = [e["data"]["name"] for e in events if e["type"] == "tool_start"]
        print("Calls:", calls)
        assert (
            "Skill" in calls
            and "search_policy" in calls
            and "search_driver_service_points" in calls
        )
        assert service.read_memory(rt.ctx, "city")["data"]["fields"][0]["value"].startswith("北京")
        assert not {"query_progress", "deliver_guidance", "query_operation"} & set(calls)

        # 模型确实使用非空 pgvector 知识，并将原始证据写入工具事件和框架检查点。
        successful_policy_results = [
            event["data"]["result"]
            for event in events
            if event["type"] == "tool_result"
            and event["data"]["name"] == "search_policy"
            and event["data"]["state"] == "success"
            and event["data"]["result"].get("status") == "success"
        ]
        assert successful_policy_results, "真实模型必须至少获得一次成功的政策检索结果"
        checkpoint = host.store.get(rt.id)["state"]
        assert checkpoint
        checkpoint_text = json.dumps(checkpoint, ensure_ascii=False)
        known_ids = {chunk["id"] for chunk in chunks}
        for result in successful_policy_results:
            clauses = result["data"]["clauses"]
            assert clauses and result["evidence"]
            assert result["evidence"] == [clause["id"] for clause in clauses]
            assert set(result["evidence"]) <= known_ids
            assert all(clause["scenario_id"] == scenario.id for clause in clauses)
            assert result["data"]["corpus_version"] == corpus["version"]
            assert all(evidence in checkpoint_text for evidence in result["evidence"])

        await host.close()
        expected = _persisted_live_state(settings.app_db, rt.id, knowledge_dir)
        close_database(settings.app_db)
        # 全新解释器必须只靠 PostgreSQL 恢复同一结果；凭据只通过环境变量传递。
        program = textwrap.dedent("""
            import json
            import sys
            from pathlib import Path
            from backend.database import DatabaseConfig, close_all_databases
            from tests.runtime.test_runtime_live import _persisted_live_state

            try:
                restored = _persisted_live_state(
                    DatabaseConfig.from_env(), sys.argv[1], Path(sys.argv[2]),
                )
                print(json.dumps(restored, ensure_ascii=False))
            except Exception as error:
                # 不输出可能包含连接凭据的异常内容。
                print(type(error).__name__, file=sys.stderr)
                sys.exit(1)
            finally:
                close_all_databases()
        """)
        restored = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", program, rt.id, str(knowledge_dir)],
            cwd=settings.project_root,
            env={
                **os.environ,
                "DATABASE_URL": settings.app_db.dsn,
                "DATABASE_SCHEMA": settings.app_db.schema,
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert restored.returncode == 0, f"全新进程恢复失败：{restored.stderr}"
        assert json.loads(restored.stdout) == expected
        report["postgres_acceptance"] = {
            "corpus": corpus,
            "successful_policy_results": len(successful_policy_results),
            "checkpoint_saved": True,
            "fresh_process_recovery": "passed",
        }
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        await host.close()
        close_database(settings.app_db)


@pytest.mark.skipif(
    os.environ.get("GROWTH_RUN_LIVE") != "1",
    reason="real-provider smoke requires explicit GROWTH_RUN_LIVE=1",
)
@pytest.mark.asyncio
async def test_real_text_boolean_memory(tmp_path):
    settings = replace(
        Settings.from_env(),
        runtime_dir=tmp_path,
        app_db=database_for(tmp_path / "application"),
    )
    scenario = load_scenario(
        settings.project_root / "backend/scenarios/meituan_rider/scenario.yaml"
    )
    service = BackendService(settings.app_db, scenario)
    host = SessionManager(settings, {scenario.id: service})
    rt = await host.create(scenario.id, "runtime-smoke-typed-memory")
    await host.profile_worker.close()
    try:
        reply = await rt.message("方便。资料已准备，请记住我已经准备好了资料。")
        await rt.stop()
        assert await host.profile_worker.run_once()
        assert service.profiles.state(rt.ctx)["status"] == "completed"
        events = host.store.events(rt.id, limit=500)
        state = service.snapshot(rt.ctx)
        output = (
            settings.project_root
            / ".run/checks/live-dialogue-probes"
            / f"runtime-typed-memory-{int(time.time())}.json"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "type": "real_text_typed_memory",
                    "prompt_version": PROMPT_VERSION,
                    "framework_commit": FRAMEWORK_COMMIT,
                    "scenario": scenario.id,
                    "snapshot_hash": scenario.snapshot_hash,
                    "model": settings.agent_model,
                    "reply": reply,
                    "events": events,
                    "state": state,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print("Report:", output)
        assert any(
            item["field"] == "materials_ready" and item["value"] is True for item in state["memory"]
        )
        assert not any(
            event["type"] == "tool_result" and "Invalid value type/range" in json.dumps(event)
            for event in events
        )
    finally:
        await host.close()
