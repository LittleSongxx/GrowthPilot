"""工具调用边界：客户作用域、模型入参、超时和结果审计。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING

from pydantic import ConfigDict, ValidationError, validate_call

if TYPE_CHECKING:
    from ..business_data import BusinessCatalog


@dataclass
class ToolContext:
    """创建会话时绑定能力，工具不需要认识会话管理器或存储实现。"""

    search_policy: Callable[[str], dict]
    catalog: Callable[[], BusinessCatalog]
    authorize: Callable[[str | None], str | None]
    emit: Callable[[str, dict], object]
    timeout_seconds: float = 10
    send_download_sms: Callable[[], dict] | None = None
    request_end_call: Callable[[str], None] | None = None


def denied(reason):
    return {"status": "denied", "data": {"reason": reason}, "evidence": [], "retryable": False}


def business_tool(function):
    """保留函数签名，让 FunctionTool 自动生成模型所需的参数说明。"""
    validated = validate_call(config=ConfigDict(strict=True))(function)

    @wraps(function)
    async def checked(context, *args, **kwargs):
        reason = context.authorize(function.__name__)
        if reason:
            return denied(reason)
        try:
            async with asyncio.timeout(context.timeout_seconds):
                result = await validated(context, *args, **kwargs)
        except ValidationError:
            return denied("invalid_arguments:ValidationError")
        except TimeoutError:
            return {
                "status": "unknown",
                "data": {"reason": "tool_timeout"},
                "evidence": [],
                "retryable": False,
            }
        if result.get("data", {}).get("source_type") == "local_catalog":
            context.emit("business_query_audit", {"name": function.__name__, "result": result})
        return model_projection(result)

    return checked


_CATALOG_INTERNAL_FIELDS = {
    "source_type",
    "source",
    "disclosure",
    "generated",
    "is_fallback",
    "match_type",
    "fallback_reason",
    "template_provenance",
    "template_id",
    "variant_id",
    "profile_id",
    "profile_source",
    "profile_parameters",
    "pricing_basis",
}


def model_projection(value):
    """为业务对话筛选目录查询结果，完整保留政策证据。

    工具包装器单独记录原始目录诊断信息。业务 ID 仅作为标识用于后续查询，
    模型仍可读取适用范围、条件、金额和有效期。
    此过程不改写自然语言正文。
    """

    def project(item, *, catalog=False, condition=False):
        if isinstance(item, dict):
            data = item.get("data")
            catalog = catalog or (
                isinstance(data, dict) and data.get("source_type") == "local_catalog"
            )
            return {
                key: project(
                    child,
                    catalog=catalog,
                    condition=condition or key in {"assumed_filters", "eligibility_filters"},
                )
                for key, child in item.items()
                if key not in {"workspace_id", "contact_source"}
                and not (catalog and key in _CATALOG_INTERNAL_FIELDS)
            }
        if isinstance(item, list):
            return [project(child, catalog=catalog, condition=condition) for child in item]
        if catalog and condition and item == "__generic__":
            return "通用条件"
        return item

    return project(value)
