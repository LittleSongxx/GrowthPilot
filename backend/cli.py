"""统一的本地命令入口；离线检查不会隐式调用模型。"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from backend.paths import portable_data, portable_path, redact_local_paths, resolve_project_path
from backend.settings import FRAMEWORK_COMMIT, Settings


def emit(value):
    print(json.dumps(portable_data(value), ensure_ascii=False, indent=2, default=str))


def positive_integer(value):
    """并发名额必须是正整数，在加载运行资源之前拒绝无效输入。"""
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是正整数") from error
    if number < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def safe_error(error):
    """服务供应商异常可能包含授权请求头或带凭证的 URL。"""
    if not isinstance(error, (ValueError, FileNotFoundError)):
        return f"{type(error).__name__}: 操作未完成，请检查配置或本地脱敏日志。"
    message = str(error)
    from dotenv import dotenv_values

    root = resolve_project_path(os.environ.get("GROWTH_PROJECT_ROOT", "."))
    values = {**dotenv_values(root / ".env"), **os.environ}
    for key, value in values.items():
        if value and any(
            word in key.upper() for word in ("SECRET", "TOKEN", "API_KEY", "PASSWORD", "DATABASE_URL")
        ):
            message = message.replace(str(value), "[REDACTED]")
    message = re.sub(r"postgres(?:ql)?://[^\s]+", "[REDACTED_DATABASE_URL]", message)
    message = re.sub(r"(?i)(password\s*=\s*)(?:'[^']*'|\S+)", r"\1[REDACTED]", message)
    return f"{type(error).__name__}: {redact_local_paths(message, root)}"


def scenario_path(settings, value):
    if value:
        path = resolve_project_path(value, settings.project_root)
        if path.is_file():
            return path
        candidate = settings.project_root / "backend/scenarios" / value / "scenario.yaml"
        if candidate.is_file():
            return candidate.resolve()
        raise ValueError(f"找不到场景入口：{value}")
    choices = sorted((settings.project_root / "backend/scenarios").glob("*/scenario.yaml"))
    preferred = [p for p in choices if p.parent.name == settings.default_scenario_id]
    if not choices:
        raise ValueError("没有场景入口 YAML")
    return (preferred or choices)[0]


def init_secrets(settings):
    from dotenv import dotenv_values, set_key

    path = settings.project_root / ".env"
    if not path.exists():
        path.touch(mode=0o600)
    os.chmod(path, 0o600)
    existing = dotenv_values(path)
    created = []
    for key in ("SESSION_SECRET",):
        if not existing.get(key):
            set_key(str(path), key, secrets.token_urlsafe(48))
            created.append(key)
    return created


def port_available(port):
    with socket.socket() as sock:
        # 与 uvicorn 保持一致：关闭后的 TIME_WAIT 状态不能阻止立即重启。
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def doctor(args, settings):
    if args.init_secrets:
        print("已生成缺失的本地秘密字段：" + ", ".join(init_secrets(settings)))
        settings = Settings.from_env()
    from backend.agent_harness.scenario import load_scenario

    errors = []
    configs = []
    for path in sorted((settings.project_root / "backend/scenarios").glob("*/scenario.yaml")):
        try:
            scenario = load_scenario(path)
            configs.append({"id": scenario.id, "snapshot": scenario.snapshot_hash})
        except (ValueError, OSError) as error:
            errors.append(f"{path.relative_to(settings.project_root)}: {error}")
    try:
        dist = importlib.metadata.distribution("agentscope")
        source = json.loads(dist.read_text("direct_url.json") or "{}")
        commit = source.get("vcs_info", {}).get("commit_id")
    except (importlib.metadata.PackageNotFoundError, json.JSONDecodeError):
        commit = None
    if commit != FRAMEWORK_COMMIT:
        errors.append("AgentScope 安装提交与锁定提交不符；运行 uv sync --locked")
    if sys.version_info[:2] != (3, 11):
        errors.append("必须使用 Python 3.11")
    if not configs:
        errors.append("无有效场景")
    result = {
        "python": sys.version.split()[0],
        "agentscope_commit": commit,
        "scenario_count": len(configs),
        "scenarios": configs,
        "missing_required_credentials": settings.missing(),
        "voice_available": bool(settings.dashscope_api_key and settings.dashscope_base_url),
        "frontend_built": (settings.project_root / "frontend/dist/index.html").exists(),
        "ports": {
            str(p): "available" if port_available(p) else "occupied" for p in (settings.app_port,)
        },
        "application_database": str(settings.app_db),
        "online_probe": "not_requested",
        "errors": errors,
    }
    if args.online:
        from openai import OpenAI

        from backend.llm import completion_parameters

        client = OpenAI(
            api_key=settings.agent_api_key,
            base_url=settings.agent_base_url,
            timeout=20,
            max_retries=0,
        )
        response = client.chat.completions.create(
            model=settings.agent_model,
            messages=[{"role": "user", "content": "连通性检查，请只回答 OK。"}],
            **completion_parameters(settings, role="agent"),
        )
        result["online_probe"] = {
            "model": response.model,
            "request_id": response.id,
            "usage": response.usage.model_dump() if response.usage else None,
        }
    emit(result)
    return 1 if errors or settings.missing() else 0


def validate(args, settings):
    from backend.agent_harness.scenario import load_scenario

    paths = (
        [scenario_path(settings, args.scenario)]
        if args.scenario
        else sorted((settings.project_root / "backend/scenarios").glob("*/scenario.yaml"))
    )
    from backend.agent_harness.business_data.catalog import BusinessCatalog

    records = []
    for path in paths:
        scenario = load_scenario(path)
        BusinessCatalog.from_scenario(scenario)
        records.append(
            {
                "id": scenario.id,
                "version": scenario.version,
                "snapshot_hash": scenario.snapshot_hash,
                "entry": portable_path(path, settings.project_root),
                "tools": scenario.data["tools"],
            }
        )
    if not records:
        raise ValueError("没有场景入口 YAML")
    emit({"status": "valid", "model_calls": 0, "scenarios": records})
    return 0


def knowledge(args, settings):
    """默认从固定教学语料构建；只有显式刷新才采集网页。"""
    from backend.agent_harness.rag.corpus import CORPUS_RELATIVE_PATH, corpus_status, load_corpus
    from backend.agent_harness.rag.index import PolicyIndex

    directory = settings.runtime_dir / "knowledge"
    index = PolicyIndex(directory, database=settings.app_db)
    if args.action == "status":
        emit({
            **index.status(),
            "teaching_corpus": corpus_status(settings.project_root / CORPUS_RELATIVE_PATH),
        })
        return 0

    scenario_ids = sorted(
        p.parent.name for p in (settings.project_root / "backend/scenarios").glob("*/scenario.yaml")
    )
    if args.scenario and args.scenario not in scenario_ids:
        raise ValueError(f"未知知识场景：{args.scenario}")
    if args.action == "search":
        if not args.scenario or not args.query:
            raise ValueError("search 需要 --scenario 和 --query")
        emit(index.search(args.query, args.scenario))
        return 0

    from backend.agent_harness.rag.ingest import collect, load_chunks

    collected = None
    teaching = None
    if getattr(args, "refresh", False):
        collected = collect(
            settings.project_root / "backend/agent_harness/rag/sources.yaml", directory, args.scenario
        )
        chunks = load_chunks(directory, args.scenario)
    elif args.from_cache:
        if not (directory / "catalog.json").exists():
            raise ValueError("没有在线采集快照；直接运行 growth knowledge build 可使用固定教学语料")
        chunks = load_chunks(directory, args.scenario)
    else:
        corpus_dir = settings.project_root / CORPUS_RELATIVE_PATH
        chunks = load_corpus(corpus_dir, args.scenario)
        teaching = corpus_status(corpus_dir)
    prepare_database(settings)
    # 显式指定场景范围后，即使来源消失或刷新失败，也会清除该范围内的旧向量。
    built = index.build(chunks, scenario_id=args.scenario)
    emit({"teaching_corpus": teaching, "collection": collected, "index": built})
    return 1 if collected and collected["failed"] else 0


def prepare_knowledge_once(settings):
    """校验教学语料与数据库版本，启动时仅恢复固定内容，不采集网页。"""
    from backend.agent_harness.rag.corpus import CORPUS_RELATIVE_PATH, load_corpus
    from backend.agent_harness.rag.index import PolicyIndex, corpus_versions

    index = PolicyIndex(settings.runtime_dir / "knowledge", database=settings.app_db)
    chunks = load_corpus(settings.project_root / CORPUS_RELATIVE_PATH)
    expected = corpus_versions(chunks)

    def matches(status):
        actual = {
            item["scenario_id"]: {"version": item["version"], "chunk_count": item["chunk_count"]}
            for item in status.get("scenarios", [])
        }
        return status["status"] == "ready" and actual == expected

    status = index.status()
    if matches(status):
        index.prepare_model()
        return
    print("正在从固定教学语料准备四场景 PostgreSQL 知识库；首次可能需要下载向量模型。")
    status = index.build(chunks)
    if not matches(status):
        raise ValueError(
            "知识库初始化未完成，索引与固定教学语料不一致；请运行 growth knowledge build。"
        )
    print("四个场景的固定教学知识库已准备完成；后续启动校验版本并复用。")


def prepare_database(settings):
    """启动前校验 PostgreSQL、pgvector 和迁移；不隐式启动或修改其他数据库服务。"""
    import psycopg

    from backend.database import initialize_database

    try:
        initialize_database(settings.app_db)
    except psycopg.Error:
        raise ValueError(
            "PostgreSQL 初始化失败。请检查 DATABASE_URL 和 DATABASE_SCHEMA，"
            "确认 PostgreSQL 已启动且安装 pgvector，再运行 growth database init。"
            "本地环境可先运行 docker compose up -d postgres。"
        ) from None


def database(args, settings):
    """显式初始化或只读检查数据库；输出仅含不带凭据的定位信息。"""
    from backend.database import connect, table_exists

    if args.action == "init":
        prepare_database(settings)
    with connect(settings.app_db, read_only=True) as db:
        version = db.execute("SELECT current_setting('server_version') AS version").fetchone()
        extension = db.execute(
            "SELECT extversion FROM pg_extension WHERE extname='vector'"
        ).fetchone()
        schema = db.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname=%s", (settings.app_db.schema,)
        ).fetchone()
        migrations = (
            db.execute("SELECT version,name FROM schema_migrations ORDER BY version").fetchall()
            if schema and table_exists(db, "schema_migrations")
            else []
        )
        required_tables = (
            "sessions", "messages", "memory", "runtime_sessions", "runtime_events", "profile_jobs",
            "snapshots", "audit", "rag_index_meta", "rag_corpora", "rag_chunks", "rag_chunk_terms",
        )
        missing_tables = [name for name in required_tables if not table_exists(db, name)]
    ready = bool(schema and extension and migrations and not missing_tables)
    emit(
        {
            "status": "ready" if ready else "not_initialized",
            "database": settings.app_db.identity,
            "postgresql_version": version["version"],
            "pgvector_version": extension["extversion"] if extension else None,
            "schema_exists": bool(schema),
            "migrations": migrations,
            "missing_tables": missing_tables,
        }
    )
    return 0 if ready else 1


APPLICATION_MODULE = "backend.api.app:create_app"


def process_field(pid, field):
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", field + "="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return ""


def owned_process(record, project_root=None):
    """不能仅因 PID 被复用或端口被占用，就向无关进程发送信号。"""
    if (
        type(record.get("pid")) is not int
        or record["pid"] <= 0
        or record.get("module") != APPLICATION_MODULE
        or type(record.get("port")) is not int
    ):
        return False
    command = shlex.split(process_field(record["pid"], "command"))
    expected = [
        "-m",
        "uvicorn",
        record["module"],
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        str(record["port"]),
        "--no-access-log",
    ]
    if len(command) < 2 or command[1:] not in (
        expected, [*expected, "--log-config", "backend/config/logging.json"]
    ):
        return False
    if record.get("process_started"):
        return process_field(record["pid"], "lstart") == record["process_started"]
    # 无法获取进程创建时间时，必须确认进程所属的代码检出目录一致。
    # 缺少进程归属证据时，不能向该进程发送信号。
    if project_root is None:
        return False
    root = Path(project_root).resolve()
    proc_cwd = Path("/proc") / str(record["pid"]) / "cwd"
    try:
        if proc_cwd.exists():
            return proc_cwd.resolve() == root
        lsof = shutil.which("lsof")
        if lsof:
            output = subprocess.check_output(
                [lsof, "-a", "-p", str(record["pid"]), "-d", "cwd", "-Fn"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            return any(line == "n" + str(root) for line in output.splitlines())
    except (subprocess.CalledProcessError, OSError):
        pass
    return False


def down(args, settings):
    state = settings.runtime_dir / "services.json"
    if not state.exists():
        print("没有由 growth up 启动的服务。数据库保留。")
        return 0
    records = json.loads(state.read_text())["processes"]
    for record in records:
        if owned_process(record, settings.project_root):
            try:
                os.kill(record["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass  # 已验证的进程在检查完成后、发送信号前结束了。
    deadline = time.monotonic() + 15
    while (
        any(owned_process(record, settings.project_root) for record in records)
        and time.monotonic() < deadline
    ):
        time.sleep(0.2)
    pending = [r for r in records if owned_process(r, settings.project_root)]
    if pending:
        emit(
            {
                "status": "shutdown_pending",
                "pids": [r["pid"] for r in pending],
                "reason": "等待框架保存状态，未强杀",
            }
        )
        return 1
    state.unlink(missing_ok=True)
    print("服务已停止，媒体与端口已释放；历史数据库保留。")
    return 0


def up(args, settings):
    import httpx

    from backend.agent_harness.scenario import load_scenario

    entry = scenario_path(settings, args.scenario)
    scenario = load_scenario(entry)
    if settings.missing():
        raise ValueError(
            "缺少配置："
            + ", ".join(settings.missing())
            + "；请参照 .env.example，运行 growth doctor --init-secrets"
        )
    state = settings.runtime_dir / "services.json"
    if state.exists():
        old = json.loads(state.read_text())
        running = [r for r in old["processes"] if owned_process(r, settings.project_root)]
        if running:
            print(
                f"服务已运行：http://127.0.0.1:{running[0]['port']}；如需更换启动配置先 growth down"
            )
            return 0
    for port in (settings.app_port,):
        if not port_available(port):
            raise ValueError(f"本机端口 {port} 已占用，请关闭冲突服务或修改 .env")
    prepare_database(settings)
    prepare_knowledge_once(settings)
    front = settings.project_root / "frontend"
    if args.build or not (front / "dist/index.html").exists():
        npm = shutil.which("npm")
        if not npm:
            raise ValueError("需要 Node.js/npm 构建 TypeScript 页面；先 npm ci && npm run build")
        if not (front / "node_modules").exists():
            subprocess.run([npm, "ci"], cwd=front, check=True)
        subprocess.run([npm, "run", "build"], cwd=front, check=True)
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GROWTH_PROJECT_ROOT": os.path.relpath(
            settings.project_root, Path(__file__).resolve().parents[1]
        ),
        "GROWTH_RUNTIME_DIR": os.path.relpath(settings.runtime_dir, settings.project_root),
        "GROWTH_SCENARIO": os.path.relpath(entry, settings.project_root),
        "PYTHONUNBUFFERED": "1",
    }
    records = []
    try:
        for name, module, port in [
            ("application", "backend.api.app:create_app", settings.app_port),
        ]:
            log_path = settings.runtime_dir / f"{name}.log"
            with log_path.open("ab") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        module,
                        "--factory",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--no-access-log",
                        "--log-config",
                        "backend/config/logging.json",
                    ],
                    cwd=settings.project_root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            records.append(
                {
                    "pid": process.pid,
                    "module": module,
                    "port": port,
                    "name": name,
                    "process_started": process_field(process.pid, "lstart"),
                    "project_root": "/growthpilot",
                }
            )
            state.write_text(json.dumps({"scenario": scenario.id, "processes": records}, indent=2))
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"{name} 启动失败，查看 {portable_path(log_path, settings.project_root)}"
                    )
                try:
                    response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
                    if response.status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(0.2)
            else:
                raise RuntimeError(
                    f"{name} 健康检查超时，查看 {portable_path(log_path, settings.project_root)}"
                )
    except BaseException:
        down(args, settings)
        raise
    print(
        f"GrowthPilot 已启动：http://127.0.0.1:{settings.app_port}\n当前场景：{scenario.data['scenario']['title']}\n语音："
        + (
            "可用，点击页面开始通话"
            if settings.dashscope_api_key and settings.dashscope_base_url
            else "缺少语音凭据，请检查 .env"
        )
        + "\n停止：growthpilot down（保留历史）"
    )
    return 0


def evaluate(args, settings):
    if args.retry_judge_from:
        if (args.suite != "dialogue" or args.scenario or args.limit is not None
                or args.case or args.repeats is not None):
            raise ValueError("--retry-judge-from 仅支持 dialogue，沿用源计划，不接受题集筛选或 repeats")
        from evaluation.judges.retry import run_retry

        return run_retry(
            settings, args.retry_judge_from, run_id=args.run,
            execute=args.execute, concurrency=args.concurrency,
        )
    if args.suite == "voice":
        from evaluation.checks.voice_review import load_observations, summarize_current_acceptance

        emit(
            summarize_current_acceptance(
                load_observations(settings.app_db),
                settings.project_root / "evaluation/checks/config/acceptance.yaml",
            )
        )
        return 0
    from evaluation.runner import run_suite

    return run_suite(
        args.suite, settings, execute=args.execute, run_id=args.run,
        scenario=args.scenario, limit=args.limit,
        repeats=args.repeats if args.repeats is not None else 2, case_ids=args.case,
        concurrency=args.concurrency,
    )


def replay(args, settings):
    from backend.database import connect, table_exists
    from evaluation.common.io import load, stored_json_path
    from evaluation.common.paths import resolve_run_dir

    report = resolve_run_dir(settings.project_root, args.run)
    if report is not None:
        path = stored_json_path(report / "traces.json")
        if path.is_file():
            emit(
                {
                    "run_id": args.run,
                    "mode": "read_only",
                    "source": path.name,
                    "data": load(path),
                }
            )
            return 0
    if "/" in args.run:
        raise ValueError("指定评测没有可回放的对话轨迹")
    result = []
    with connect(settings.app_db, read_only=True) as db:
        if not table_exists(db, "sessions"):
            raise ValueError("应用数据库尚未初始化，无可回放内容")
        rows = db.execute(
            "SELECT * FROM sessions WHERE run_id=%s ORDER BY started", (args.run,)
        ).fetchall()
        for row in rows:
            messages = [
                dict(r)
                for r in db.execute(
                    "SELECT id,origin,text,created FROM messages WHERE session=%s ORDER BY created,seq",
                    (row["id"],),
                )
            ]
            audit = [
                dict(r)
                for r in db.execute(
                    "SELECT kind,payload,created FROM audit WHERE session=%s ORDER BY id",
                    (row["id"],),
                )
            ]
            for event in audit:
                event["payload"] = json.loads(event["payload"])
            runtime_trace = []
            if table_exists(db, "runtime_events"):
                for event in db.execute(
                    "SELECT seq,type,at,data_json FROM runtime_events WHERE session_id=%s ORDER BY seq",
                    (row["id"],),
                ):
                    runtime_trace.append(
                        {
                            "seq": event["seq"],
                            "type": event["type"],
                            "at": event["at"],
                            "data": json.loads(event["data_json"]),
                        }
                    )
            result.append(
                {
                    "session": dict(row),
                    "messages": messages,
                    "trace": audit,
                    "runtime_trace": runtime_trace,
                }
            )
    emit({"run_id": args.run, "mode": "read_only", "sessions": result})
    return 0


def reset(args, settings):
    from backend.maintenance import reset_run

    emit(reset_run(settings, args.run, execute=args.execute))
    return 0


def clean(args, settings):
    from backend.retention import maintain_retention

    result = maintain_retention(settings, execute=args.execute)
    emit(result)
    return 1 if result["status"] == "incomplete" else 0


def voice_case(args, settings):
    """准备人工语音验收用例，不调用模型或外部服务。"""
    import uuid
    from urllib.parse import urlencode

    from evaluation.checks.voice_review import CASE_SCENARIOS, REVIEW_VERSION

    case = args.case
    if case not in CASE_SCENARIOS:
        raise ValueError(f"请选择 {REVIEW_VERSION} 的当前语音案例")
    scenario_id = CASE_SCENARIOS[case]
    run_id = "human-" + case.lower() + "-" + uuid.uuid4().hex[:12]
    customer_id = args.customer or run_id
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", customer_id):
        raise ValueError("客户标识仅支持字母、数字、下划线和短横线")
    url = f"http://127.0.0.1:{settings.app_port}/?" + urlencode(
        {
            "scenario": scenario_id,
            "customer": customer_id,
            "run": run_id,
            "voice_case": case,
            "review_version": REVIEW_VERSION,
        }
    )
    emit(
        {
            "case": case,
            "review_version": REVIEW_VERSION,
            "run_id": run_id,
            "customer_id": customer_id,
            "url": url,
            "mode": "conversation_only",
            "model_requests": 0,
            "next": (
                "打开链接并开始通话，实际体验后填写人工语音观察。"
                + (
                    " D03/D04 需要同一客户标识；再次准备另一例时传入 --customer "
                    + customer_id
                    + "。"
                    if case.endswith(("-D03", "-D04"))
                    else ""
                )
            ),
        }
    )
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="growthpilot", description="GrowthPilot · 四场景语音用户增长工作台"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("doctor", help="离线检查；仅 --online 发起模型请求")
    p.set_defaults(handler=doctor)
    p.add_argument("--online", action="store_true")
    p.add_argument("--init-secrets", action="store_true")
    p = sub.add_parser("up", help="启动对话应用与前端")
    p.set_defaults(handler=up)
    p.add_argument("--scenario")
    p.add_argument("--build", action="store_true")
    sub.add_parser("down", help="结束服务且保留资料").set_defaults(handler=down)
    p = sub.add_parser("validate", help="离线加载场景配置与业务资源")
    p.set_defaults(handler=validate)
    p.add_argument("--scenario")
    p = sub.add_parser("database", help="初始化 PostgreSQL + pgvector 或只读检查数据库状态")
    p.set_defaults(handler=database)
    p.add_argument("action", choices=("init", "status"))
    p = sub.add_parser("knowledge", help="构建官方政策知识库、自然语言检索或查看状态")
    p.set_defaults(handler=knowledge)
    p.add_argument("action", choices=("build", "search", "status"))
    p.add_argument("--scenario", help="场景 ID；build 默认处理全部场景")
    p.add_argument("--query", help="search 使用的自然语言问题")
    source = p.add_mutually_exclusive_group()
    source.add_argument("--from-cache", action="store_true", help="使用本机显式采集的快照；默认使用固定教学语料")
    source.add_argument("--refresh", action="store_true", help="显式采集最新网页构建实验索引；下次启动恢复教学版本")
    p = sub.add_parser("evaluate", help="显示冻结清单或运行评测")
    p.set_defaults(handler=evaluate)
    p.add_argument("--suite", required=True, help="正式评测：rag 或 dialogue")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--run", help="可选运行标签；Agent 自动按模型和日期建目录，RAG 用此值命名目录")
    p.add_argument("--scenario", help="只运行所选业务，默认全部四场景")
    p.add_argument("--limit", type=int, help="每场景最多运行多少个案例")
    p.add_argument("--repeats", type=int, help="同一客户在本轮评测中的连续通话次数，默认 2")
    p.add_argument(
        "--retry-judge-from", metavar="RUN", help="仅 dialogue：复用 agent/运行目录或唯一运行名，只补跑失败裁判",
    )
    p.add_argument(
        "--concurrency", type=positive_integer, default=16,
        help="仅 dialogue：同时评测的客户数量，默认 16；同一客户的多次通话顺序执行",
    )
    p.add_argument("--case", action="append", help="指定案例 ID，可重复传入")
    p = sub.add_parser("replay", help="只读回放实验")
    p.set_defaults(handler=replay)
    p.add_argument("--run", required=True, help="agent/运行目录、唯一评测运行名或应用数据库运行名")
    p = sub.add_parser("reset", help="预览指定运行的会话清理；--execute 才执行，保留客户记忆")
    p.set_defaults(handler=reset)
    p.add_argument("--run", required=True)
    p.add_argument("--execute", action="store_true")
    p = sub.add_parser("clean", help="预览或清理超过30天的已结束会话轨迹，保留客户记忆")
    p.set_defaults(handler=clean)
    p.add_argument("--expired", action="store_true", required=True)
    p.add_argument("--execute", action="store_true")
    p = sub.add_parser("voice-case", help="准备独立人工语音案例，输出无凭据入口")
    p.set_defaults(handler=voice_case)
    p.add_argument("--case", required=True, help="当前人工语音验收案例编号")
    p.add_argument("--customer", help="显式客户标识；当前版本的 D03/D04 使用同一标识")
    args = parser.parse_args()
    try:
        from backend.paths import install_path_safe_logging

        install_path_safe_logging()
        code = args.handler(args, Settings.from_env())
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        code = 130
    except Exception as error:  # noqa: BLE001 - 命令行入口必须返回安全的失败退出码。
        # 不打印服务供应商的请求对象、请求头或环境变量值。
        print(safe_error(error), file=sys.stderr)
        code = 1
    raise SystemExit(code or 0)


if __name__ == "__main__":
    main()
