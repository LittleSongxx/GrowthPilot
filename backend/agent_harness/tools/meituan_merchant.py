"""美团商家工具：配送方案、收费规则、商家活动与单笔贡献计算。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ..business_data import budget
from .boundary import business_tool
from .common import Amount


@business_tool
async def query_delivery_options(
    context,
    area: Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")],
    store_category: Annotated[str, Field(min_length=1, max_length=40)],
) -> dict:
    """查询门店所在地区与类目对应的配送方案，帮助解释平台配送和自配送差别。

    Args:
        area: 客户已提供的门店城市、区县或商圈；城市级即可查询，无需为调用工具反复追问地标。
        store_category: 客户明确的门店类目，例如餐饮或便利店，不从品牌名推断；未精确匹配时按返回的参考条件解释。
    """
    return context.catalog().query("delivery_option", location=area, store_category=store_category)


@business_tool
async def query_merchant_fee_plans(
    context,
    area: Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")],
    store_category: Annotated[str, Field(min_length=1, max_length=40)],
    delivery_mode: Annotated[str, Field(min_length=1, max_length=40)],
) -> dict:
    """查询费率参考范围及收费条件，保留与实际合同报价的区别。

    Args:
        area: 客户已提供的城市、区县或商圈；城市级可直接查询，未精确匹配时保留结果的参考范围和条件。
        store_category: 门店类目，例如餐饮或便利店。
        delivery_mode: 客户明确选择的配送方式，例如平台配送或商家自配送；其他合法方式可返回参考条件，不等于该方式已获实际支持。
    """
    return context.catalog().query(
        "merchant_fee_plan",
        location=area,
        store_category=store_category,
        delivery_mode=delivery_mode,
    )


@business_tool
async def query_merchant_offers(
    context,
    area: Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")],
    store_category: Annotated[str, Field(min_length=1, max_length=40)],
    is_new_merchant: bool,
) -> dict:
    """按客户自述条件筛选商家活动，保留期限与使用条件，不核验真实账户资格。

    Args:
        area: 客户已提供的城市、区县或商圈；城市级可直接查询，未精确匹配时保留结果的参考范围和条件。
        store_category: 客户明确的门店类目。
        is_new_merchant: 客户是否明确自述新商家，未知时先询问。
    """
    return context.catalog().query(
        "merchant_offer",
        location=area,
        store_category=store_category,
        is_new_merchant=is_new_merchant,
    )


@business_tool
async def estimate_order_contribution(
    context,
    *,
    received_amount: Amount,
    goods_cost: Amount,
    packaging_cost: Amount,
    other_variable_cost: Amount,
    amount_basis: Literal["before_fees", "after_fees"] = "before_fees",
    fee_base: Amount | None = None,
    service_fee_percent: Annotated[int | float, Field(ge=0, le=100, allow_inf_nan=False)]
    | None = None,
    delivery_cost: Amount | None = None,
) -> dict:
    """区分扣费前订单实收与扣费后到账，计算商家单笔贡献，避免重复扣费。

    Args:
        received_amount: 商家承担优惠后的订单金额，人民币元，最多两位小数；须确认是扣费前实收还是已扣完平台及配送费用的到账，优惠不重复扣除。
        goods_cost: 客户确认且尚未从输入金额扣除的本单食材或货品成本，人民币元，最多两位小数；未知先澄清，可明确为零。
        packaging_cost: 客户确认且尚未从输入金额扣除的本单包装成本，人民币元，最多两位小数；未知先澄清，可明确为零。
        other_variable_cost: 客户确认且尚未从输入金额扣除的本单其他变动成本合计，人民币元，最多两位小数；不与其他成本重复，未知先澄清，可明确为零。
        amount_basis: before_fees表示尚未扣技术服务费和配送费，须明确收费基数、费率和配送成本，且仅支持简单比例计费；after_fees表示已扣完平台及配送费用、尚未扣货品等经营成本，须省略上述三项，禁止重复扣费。
        fee_base: 仅扣费前口径必填：独立确认的技术服务费收费基数，人民币元，最多两位小数，不能默认等于订单实收；扣费后到账口径必须省略。
        service_fee_percent: 仅扣费前口径必填：简单比例技术服务费率，百分数零至一百，最多两位小数；百分之六填写六，最低收费、阶梯、封顶或未知叠加规则不可套用；扣费后到账口径必须省略。
        delivery_cost: 仅扣费前口径必填：本单商家承担且尚未扣除的配送成本，人民币元，最多两位小数；未知先澄清，可明确为零；扣费后到账口径必须省略。
    """
    return budget.estimate_order_contribution(
        received_amount=received_amount,
        goods_cost=goods_cost,
        packaging_cost=packaging_cost,
        other_variable_cost=other_variable_cost,
        amount_basis=amount_basis,
        fee_base=fee_base,
        service_fee_percent=service_fee_percent,
        delivery_cost=delivery_cost,
    )
