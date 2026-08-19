"""金融咨询工具：产品说明、活动与费用测算。"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from .boundary import business_tool


@business_tool
async def query_product_options(
    context, purpose: Annotated[str, Field(min_length=1, max_length=120)]
) -> dict:
    """查询App功能、信息用途或费用说明；不提供个人额度、利率或审批。

    Args:
        purpose: 客户明确的了解目的，例如了解借款费用、查看还款计划或账号安全；未精确匹配时根据返回的功能范围说明，不替客户推定需求。信息用途可能不含计息条款，不能把登录、注册、下载意向解释为借款需求。
    """
    return context.catalog().query("financial_product", purpose=purpose)


@business_tool
async def query_financial_offers(context, product_id: str, is_new_user: bool) -> dict:
    """查询产品活动说明与适用条件，仅按客户自述筛选，不核验个人资格。

    Args:
        product_id: 产品查询返回的 ID，或客户明确提供的产品标识；返回 requested_product_id 和 effective_product_id 不同时，说明未查到原产品活动条款，不能称为原产品已确认的权益。ID仅用于工具调用。
        is_new_user: 客户明确自述是否新用户，未知时先询问。
    """
    return context.catalog().query(
        "financial_offer", product_id=product_id, is_new_user=is_new_user
    )


@business_tool
async def estimate_financing_cost(
    context,
    product_id: str,
    amount: Annotated[int | float, Field(gt=0, le=100000000, allow_inf_nan=False)],
    term_months: Annotated[int, Field(ge=1, le=120)],
) -> dict:
    """按返回的明确条件计算利息与到期总额，展示公式和假设，不是个人报价或借款建议。

    Args:
        product_id: 产品查询返回或客户明确提供的产品标识；原 requested_product_id 与 effective_product_id 不同时，说明未查到该产品条款，按返回条件估算，不冒充原产品报价。标识仅用于工具调用。
        amount: 客户明确要求测算的人民币本金，最多两位小数，不代客户选金额。
        term_months: 客户明确要求的期限月数，须在返回条件支持范围内。
    """
    return context.catalog().estimate_financing_cost(product_id, amount, term_months)
