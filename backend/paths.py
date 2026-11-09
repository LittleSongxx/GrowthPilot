"""统一对外展示路径，避免教学资料和运行日志记录本机用户名与目录。"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path, PurePath
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_PREFIX = "/growthpilot"

# 只匹配独立的本机路径；公开网站 URL 中的 /home/ 等业务路由保持原样。
_LOCAL_PATH = re.compile(
    r"(?<![\w/])(?:file://)?(?:"
    r"/(?:Users|home|root|private|tmp|var|usr|opt|Library|Volumes|mnt|workspace|app|srv|etc)/"
    r"|[A-Za-z]:[\\/](?:Users|Temp)[\\/]"
    r")[^\s\"'<>|)\]}，。；：]*"
)


def resolve_project_path(path: str | PurePath, project_root: Path | None = None) -> Path:
    """按项目根目录定位文件；兼容展示前缀和用户显式指定的外部目录。"""
    root = (project_root or PROJECT_ROOT).resolve()
    value = str(path)
    if value == PROJECT_PREFIX or value.startswith(PROJECT_PREFIX + "/"):
        relative = value.removeprefix(PROJECT_PREFIX).lstrip("/")
        resolved = (root / relative).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("项目展示路径不能超出项目目录")
        return resolved
    candidate = Path(value).expanduser()
    return (candidate if candidate.is_absolute() else root / candidate).resolve()


def portable_path(path: str | PurePath, project_root: Path | None = None) -> str:
    """生成展示路径；项目外仅保留文件名，返回值不能用于定位真实文件。"""
    root = (project_root or PROJECT_ROOT).resolve()
    value = str(path).replace("\\", "/")
    if value == PROJECT_PREFIX or value.startswith(PROJECT_PREFIX + "/"):
        return value
    candidate = Path(value)
    if not candidate.is_absolute() and not re.match(r"^[A-Za-z]:/", value):
        candidate = root / candidate
    try:
        relative = candidate.resolve().relative_to(root)
        suffix = relative.as_posix()
        return PROJECT_PREFIX if suffix == "." else f"{PROJECT_PREFIX}/{suffix}"
    except ValueError:
        # 其他机器上的同名项目也能得到稳定的展示路径。
        marker = PROJECT_PREFIX + "/"
        if marker in value:
            return marker + value.split(marker, 1)[1]
        return Path(value).name or "本地目录"


def redact_local_paths(text: str, project_root: Path | None = None) -> str:
    """脱敏文本和异常堆栈中的本机路径，不修改内部读写所用的 Path。"""
    root = (project_root or PROJECT_ROOT).resolve()
    value = str(text)
    # 先识别当前项目，支持项目父目录包含空格的情况。
    for prefix in {str(root), root.as_posix()}:
        value = re.sub(
            re.escape(prefix) + r"(?=$|[\\/\s\"'<>|)\]}，。；：])",
            PROJECT_PREFIX,
            value,
        )
    return _LOCAL_PATH.sub(
        lambda match: portable_path(match.group().removeprefix("file://"), root),
        value,
    )


def portable_data(value: Any, project_root: Path | None = None) -> Any:
    """复制待发布的 JSON 数据并脱敏；计算业务指纹时仍使用原始输入。"""
    if isinstance(value, PurePath):
        return portable_path(value, project_root)
    if isinstance(value, str):
        return redact_local_paths(value, project_root)
    if isinstance(value, dict):
        return {
            redact_local_paths(key, project_root) if isinstance(key, str) else key:
            portable_data(item, project_root)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [portable_data(item, project_root) for item in value]
    return value


class PortablePathFormatter(logging.Formatter):
    """在异常堆栈格式化后统一脱敏，避免只清理日志正文却遗漏文件位置。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact_local_paths(super().format(record))


class _WrappedPathFormatter(logging.Formatter):
    """保留第三方库原有的时间、级别和颜色格式，在最后一步清理路径。"""

    def __init__(self, original: logging.Formatter | None):
        super().__init__()
        self.original = original or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        return redact_local_paths(self.original.format(record))


def _write_path_safe_loguru(message: Any) -> None:
    """接收 Loguru 已格式化的消息和堆栈，写入应用当前的标准错误流。"""
    sys.stderr.write(redact_local_paths(str(message)))
    sys.stderr.flush()


def install_path_safe_logging() -> None:
    """接管已加载依赖的日志出口；保持标准日志的级别、过滤器和输出位置。"""
    loggers = [logging.getLogger(), *list(logging.Logger.manager.loggerDict.values())]
    handlers = {
        handler
        for logger in loggers
        if isinstance(logger, logging.Logger)
        for handler in logger.handlers
    }
    if logging.lastResort is not None:
        handlers.add(logging.lastResort)
    for handler in handlers:
        if not isinstance(handler.formatter, (PortablePathFormatter, _WrappedPathFormatter)):
            handler.setFormatter(_WrappedPathFormatter(handler.formatter))

    # FastEmbed 使用独立的 Loguru 出口，不经过标准 logging 或 Uvicorn 配置。
    # 使用其已有依赖与默认级别、格式配置，重复初始化也只保留一个脱敏出口。
    try:
        from loguru import logger as loguru_logger
    except ImportError:
        return
    loguru_logger.remove()
    loguru_logger.add(_write_path_safe_loguru)
