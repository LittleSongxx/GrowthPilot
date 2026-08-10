"""仅按客户确认的条件做预算，不推测收入、价格或个人适用条款。"""

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

_CENT = Decimal("0.01")
_MAX_AMOUNT = Decimal(100000000)


def _number(value, *, maximum=_MAX_AMOUNT):
    """拒绝布尔值、非有限值和分以下金额，未知费用不能替换为零。"""
    if type(value) not in (int, float):
        return None
    number = Decimal(str(value))
    if not number.is_finite() or not Decimal(0) <= number <= maximum:
        return None
    if number != number.quantize(_CENT):
        return None
    return number


def _clarification(reason):
    return {
        "status": "needs_clarification",
        "data": {"reason": reason},
        "evidence": [],
        "retryable": False,
    }


def _result(calculation):
    return {
        "status": "success",
        "data": {"calculation": calculation},
        "evidence": [],
        "retryable": False,
    }


def estimate_work_budget(
    work_days: int,
    monthly_fixed_cost: float,
    daily_variable_cost: float,
    refundable_deposit: float,
    monthly_income: float | None = None,
) -> dict:
    """计算月度经营成本、覆盖成本的收入门槛和押金现金占用。"""
    if type(work_days) is not int or not 1 <= work_days <= 31:
        return _clarification("请确认每月实际出勤天数，须为一至三十一之间的整数。")
    costs = [
        _number(value) for value in (monthly_fixed_cost, daily_variable_cost, refundable_deposit)
    ]
    if any(value is None for value in costs):
        return _clarification(
            "请确认每月固定成本、每日变动成本和可退押金；"
            "金额须为零至一亿元、最多两位小数，未知项不能填零。"
        )
    fixed, daily, deposit = costs
    income = _number(monthly_income) if monthly_income is not None else None
    if monthly_income is not None and income is None:
        return _clarification(
            "请确认月收入假设；金额须为零至一亿元、最多两位小数，"
            "口径是已扣平台费用但未扣经营成本的收入。"
        )

    variable = daily * work_days
    operating = fixed + variable
    daily_fixed = (fixed / work_days).quantize(_CENT, rounding=ROUND_HALF_UP)
    daily_threshold = (operating / work_days).quantize(_CENT, rounding=ROUND_CEILING)
    balance = income - operating if income is not None else None
    return _result(
        {
            "work_days": work_days,
            "monthly_fixed_cost_yuan": float(fixed),
            "daily_fixed_cost_yuan": float(daily_fixed),
            "daily_variable_cost_yuan": float(daily),
            "monthly_variable_cost_yuan": float(variable),
            "monthly_operating_cost_yuan": float(operating),
            "refundable_deposit_yuan": float(deposit),
            "monthly_income_assumption_yuan": float(income) if income is not None else None,
            "monthly_income_threshold_yuan": float(operating),
            "daily_income_threshold_yuan": float(daily_threshold),
            "first_month_cash_budget_yuan": float(operating + deposit),
            "budget_balance_yuan": float(balance) if balance is not None else None,
            "personal_quote": False,
            "formula": (
                "月变动成本＝每日变动成本×出勤天数；月经营成本＝月固定成本＋月变动成本；"
                "每日固定成本分摊＝月固定成本÷实际出勤天数；"
                "月收入门槛＝月经营成本；每日收入门槛＝月经营成本÷出勤天数；"
                "首月现金预算＝月经营成本＋可退押金；预算余额＝月收入假设－月经营成本。"
            ),
            "rounding": (
                "金额以人民币元计；每日固定成本分摊四舍五入到分，仅用于分摊展示；"
                "每日收入门槛向上取整到分，以覆盖全月成本。"
            ),
            "assumptions": [
                "所有输入均为客户提供或明确确认的条件，不是个人报价或实际经营记录。",
                (
                    "月收入假设已扣平台费用，尚未扣经营成本，不重复扣平台费用；"
                    "没有收入假设时只计算成本与门槛，不预测收入。"
                ),
                "固定成本按整月计入，变动成本按实际出勤天数计算；同一费用不能重复计入。",
                "每日固定成本分摊仅为展示，不能代替覆盖全部经营成本的每日收入门槛。",
                "可退押金仅计入现金占用，不作为经营成本；是否退还及扣退条件仍需核实。",
                "首月现金预算表示整月成本与押金的合计，不代表签约当天应付款。",
                "预算余额只是当前条件下的差额，可为负数，不是保证净收入。",
            ],
            "excluded": [
                "未纳入客户确认成本的税费、折旧、维修、保险、违约金及其他未知支出。",
                "未确认的奖励、实际订单量、未来收入变化及押金扣退损失。",
            ],
        }
    )


def estimate_order_contribution(
    *,
    received_amount: float,
    goods_cost: float,
    packaging_cost: float,
    other_variable_cost: float,
    amount_basis: str = "before_fees",
    fee_base: float | None = None,
    service_fee_percent: float | None = None,
    delivery_cost: float | None = None,
) -> dict:
    """区分扣费前订单实收和扣费后到账，避免重复扣费，不将贡献称为净利润。"""
    if amount_basis not in ("before_fees", "after_fees"):
        return _clarification(
            "请先明确金额是尚未扣平台及配送费用的订单实收，还是已扣完的到账金额。"
        )
    amounts = [
        _number(value)
        for value in (received_amount, goods_cost, packaging_cost, other_variable_cost)
    ]
    if any(value is None for value in amounts):
        return _clarification(
            "请确认订单金额、货品成本、包装成本和其他变动成本；"
            "金额须为零至一亿元，最多两位小数；成本可明确为零，但不能用零代替未知。"
        )
    received, goods, packaging, other = amounts
    base = percent = delivery = service_fee = None
    remaining_cost = goods + packaging + other
    if amount_basis == "after_fees":
        if any(value is not None for value in (fee_base, service_fee_percent, delivery_cost)):
            return _clarification(
                "已扣费到账口径不能同时填写收费基数、技术服务费率或配送成本，"
                "请确认已扣完平台及配送费用并省略这些项目，避免重复扣除。"
            )
        basis_note = "已扣完平台及配送费用、尚未扣货品等经营成本的到账金额。"
        formula = (
            "待扣经营成本＝货品成本＋包装成本＋其他变动成本；"
            "单笔贡献＝已扣平台及配送费用的到账金额－待扣经营成本。"
        )
        assumptions = [
            "到账金额已减去商家承担的优惠，并已扣完平台及配送费用；这些费用不再扣除。",
            "货品、包装和其他变动成本尚未从本次到账金额中扣除，必须逐项确认。",
            "无需反推扣费前金额或平台计费规则；已扣费用明细未知，不能推导全部变动成本。",
        ]
    else:
        base, delivery = _number(fee_base), _number(delivery_cost)
        percent = _number(service_fee_percent, maximum=Decimal(100))
        if base is None or delivery is None or percent is None:
            return _clarification(
                "扣费前口径须明确收费基数、技术服务费率和配送成本，不能省略或用零代替未知；"
                "金额须为零至一亿元，费率须为百分之零至百分之一百，最多两位小数。"
            )
        service_fee = (base * percent / 100).quantize(_CENT, rounding=ROUND_HALF_UP)
        remaining_cost += service_fee + delivery
        basis_note = "商家承担优惠后、尚未扣技术服务费和配送费的订单实收。"
        formula = (
            "技术服务费＝收费基数×技术服务费率÷100；"
            "单笔变动成本＝技术服务费＋配送成本＋货品成本＋包装成本＋其他变动成本；"
            "单笔贡献＝优惠后且未扣技术和配送费用的订单实收－单笔变动成本。"
        )
        assumptions = [
            (
                "订单实收已经减去商家承担的优惠，但尚未扣技术服务费和配送费；"
                "已扣费到账金额不能直接使用此口径，优惠不能再次扣除。"
            ),
            "收费基数独立确认，不能默认等于优惠后的订单实收。",
            "仅适用于已确认的简单比例收费，不适用于最低收费、阶梯收费、封顶或未知叠加规则。",
            "配送成本须确认由谁承担、是否已扣及本单适用金额；同一费用不能重复计入。",
        ]
    return _result(
        {
            "amount_basis": amount_basis,
            "amount_basis_note": basis_note,
            "received_amount_yuan": float(received),
            "fee_base_yuan": float(base) if base is not None else None,
            "service_fee_percent": float(percent) if percent is not None else None,
            "service_fee_yuan": float(service_fee) if service_fee is not None else None,
            "delivery_cost_yuan": float(delivery) if delivery is not None else None,
            "goods_cost_yuan": float(goods),
            "packaging_cost_yuan": float(packaging),
            "other_variable_cost_yuan": float(other),
            "additional_cost_to_deduct_yuan": float(remaining_cost),
            "total_variable_cost_yuan": (
                float(remaining_cost) if amount_basis == "before_fees" else None
            ),
            "order_contribution_yuan": float(received - remaining_cost),
            "personal_quote": False,
            "formula": formula,
            "rounding": "金额以人民币元计；扣费前口径的技术服务费按四舍五入保留到分。",
            "assumptions": [
                "所有输入均为客户提供或明确确认的条件，不是个人报价，未核验后台经营记录。",
                *assumptions,
                "单笔贡献可为负数，不等于门店净利润。",
            ],
            "excluded": [
                "门店租金、固定人员成本、税费和未提供的其他支出。",
                "未确认的活动补贴、未结算费用和其他未知支出。",
            ],
        }
    )
