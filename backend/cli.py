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

APPLICATION_MODULE = "backend.api.app:create_app"

if __name__ == "__main__":
    main()

