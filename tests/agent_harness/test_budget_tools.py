"""验证条件预算的金额口径、缺项拒绝、十进制精度和场域权限。"""

from types import MethodType

import pytest
from agentscope.tool import FunctionTool

from backend.agent_harness.business_data import budget
from backend.agent_harness.tools import ToolContext, build_tools
from backend.agent_harness.tools.common import estimate_work_budget
from backend.agent_harness.tools.meituan_merchant import estimate_order_contribution

WORK_INPUT = {
    "work_days": 8,
    "monthly_fixed_cost": 1000,
    "daily_variable_cost": 30,
    "refundable_deposit": 500,
}
ORDER_INPUT = {
    "received_amount": 20,
    "fee_base": 25,
    "service_fee_percent": 6,
    "delivery_cost": 4,
    "goods_cost": 8,
    "packaging_cost": 1,
    "other_variable_cost": 0,
}
SETTLED_INPUT = {
    "amount_basis": "after_fees",
    "received_amount": 30,
    "goods_cost": 10,
    "packaging_cost": 2,
    "other_variable_cost": 0,
}


def context(allowed):
    def unused_catalog():
        raise AssertionError("条件预算不应查询或编造业务目录条款")

    return ToolContext(
        search_policy=lambda query: {},
        catalog=unused_catalog,
        authorize=lambda name: None if name in allowed else "tool_not_declared",
        emit=lambda *_args: None,
    )


def test_work_budget_separates_deposit_from_cost_and_retains_negative_balance():
    result = budget.estimate_work_budget(**WORK_INPUT, monthly_income=1000)
    assert result["status"] == "success"
    assert result["evidence"] == []
    assert result["retryable"] is False
    calculation = result["data"]["calculation"]
    assert calculation["monthly_operating_cost_yuan"] == 1240
    assert calculation["first_month_cash_budget_yuan"] == 1740
    assert calculation["monthly_income_threshold_yuan"] == 1240
    assert calculation["daily_income_threshold_yuan"] == 155
    assert calculation["budget_balance_yuan"] == -240
    assert calculation["personal_quote"] is False
    assert any("不代表签约当天" in note for note in calculation["assumptions"])
    assert calculation["excluded"] and calculation["formula"]


def test_missing_income_still_returns_cost_threshold_without_income_forecast():
    result = budget.estimate_work_budget(**WORK_INPUT)
    calculation = result["data"]["calculation"]
    assert calculation["monthly_income_assumption_yuan"] is None
    assert calculation["budget_balance_yuan"] is None
    assert calculation["monthly_income_threshold_yuan"] == 1240


def test_daily_threshold_rounds_up_so_rounded_daily_income_covers_cost():
    result = budget.estimate_work_budget(3, 1, 0, 0)
    calculation = result["data"]["calculation"]
    assert calculation["daily_fixed_cost_yuan"] == 0.33
    assert calculation["daily_income_threshold_yuan"] == 0.34
    assert calculation["monthly_income_threshold_yuan"] == 1


@pytest.mark.parametrize("days, daily_fixed, daily_threshold", [(8, 375, 435), (20, 150, 210)])
def test_daily_fixed_cost_allocation_is_distinct_from_full_operating_cost_threshold(
    days, daily_fixed, daily_threshold
):
    result = budget.estimate_work_budget(days, 3000, 60, 500)
    calculation = result["data"]["calculation"]
    assert calculation["daily_fixed_cost_yuan"] == daily_fixed
    assert calculation["daily_income_threshold_yuan"] == daily_threshold
    assert calculation["monthly_fixed_cost_yuan"] == 3000
    assert "仅用于分摊展示" in calculation["rounding"]


def test_daily_fixed_allocation_rounds_half_cent_up():
    result = budget.estimate_work_budget(2, 0.01, 0, 0)
    calculation = result["data"]["calculation"]
    assert calculation["daily_fixed_cost_yuan"] == 0.01
    assert calculation["monthly_operating_cost_yuan"] == 0.01


def test_explicit_zero_costs_and_income_are_valid_without_confusing_them_with_unknown():
    calculation = budget.estimate_work_budget(1, 0, 0, 0, 0)["data"]["calculation"]
    assert calculation["monthly_income_assumption_yuan"] == 0
    assert calculation["budget_balance_yuan"] == 0
    assert calculation["first_month_cash_budget_yuan"] == 0
    assert calculation["daily_fixed_cost_yuan"] == 0
    zero_order = dict.fromkeys(ORDER_INPUT, 0)
    result = budget.estimate_order_contribution(**zero_order)
    assert result["status"] == "success"
    assert result["data"]["calculation"]["order_contribution_yuan"] == 0


@pytest.mark.parametrize("work_days", [0, 32, True, 1.5, "8", None])
def test_work_days_require_real_integer_within_month(work_days):
    result = budget.estimate_work_budget(**{**WORK_INPUT, "work_days": work_days})
    assert result["status"] == "needs_clarification"
    assert "calculation" not in result["data"]


@pytest.mark.parametrize(
    "field", ["monthly_fixed_cost", "daily_variable_cost", "refundable_deposit"]
)
@pytest.mark.parametrize("invalid", [None, True, "10", -1, float("nan"), float("inf"), 1.001, 1e9])
def test_unknown_or_invalid_work_cost_never_becomes_zero(field, invalid):
    result = budget.estimate_work_budget(**{**WORK_INPUT, field: invalid})
    assert result["status"] == "needs_clarification"
    assert "calculation" not in result["data"]


@pytest.mark.parametrize("invalid", [True, "100", -1, float("nan"), float("inf"), 1.001, 1e9])
def test_invalid_income_cannot_enter_budget(invalid):
    assert budget.estimate_work_budget(**WORK_INPUT, monthly_income=invalid)["status"] == (
        "needs_clarification"
    )


def test_order_uses_independent_fee_base_and_does_not_subtract_discount_twice():
    result = budget.estimate_order_contribution(**ORDER_INPUT)
    calculation = result["data"]["calculation"]
    assert result["status"] == "success"
    assert calculation["service_fee_yuan"] == 1.5
    assert calculation["total_variable_cost_yuan"] == 14.5
    assert calculation["order_contribution_yuan"] == 5.5
    assert any("已扣费到账金额不能直接使用" in note for note in calculation["assumptions"])
    assert any("最低收费" in note for note in calculation["assumptions"])
    assert any("净利润" in note for note in calculation["assumptions"])


def test_order_rounds_service_fee_half_up_and_retains_loss():
    result = budget.estimate_order_contribution(
        **{**ORDER_INPUT, "received_amount": 1, "fee_base": 1.5, "service_fee_percent": 1}
    )
    calculation = result["data"]["calculation"]
    assert calculation["service_fee_yuan"] == 0.02
    assert calculation["order_contribution_yuan"] == -12.02


@pytest.mark.parametrize("field", list(ORDER_INPUT))
@pytest.mark.parametrize("invalid", [None, True, "1", -1, float("nan"), float("inf"), 0.001])
def test_all_order_values_require_confirmed_nonnegative_finite_cent_precision(field, invalid):
    result = budget.estimate_order_contribution(**{**ORDER_INPUT, field: invalid})
    assert result["status"] == "needs_clarification"
    assert "calculation" not in result["data"]


def test_percentage_boundaries_and_amount_limit():
    assert (
        budget.estimate_order_contribution(**{**ORDER_INPUT, "service_fee_percent": 100})["status"]
        == "success"
    )
    for values in ({"service_fee_percent": 100.01}, {"fee_base": 100000000.01}):
        assert budget.estimate_order_contribution(**{**ORDER_INPUT, **values})["status"] == (
            "needs_clarification"
        )
    assert budget.estimate_work_budget(31, 100000000, 0.01, 0)["status"] == "success"


@pytest.mark.asyncio
async def test_business_wrapper_preserves_strict_validation_and_required_costs():
    current = context({"estimate_work_budget", "estimate_order_contribution"})
    assert (await estimate_work_budget(current, **WORK_INPUT))["status"] == "success"
    for field, invalid in (
        ("monthly_fixed_cost", True),
        ("daily_variable_cost", "30"),
        ("daily_variable_cost", float("nan")),
        ("refundable_deposit", None),
        ("work_days", 8.0),
    ):
        assert (await estimate_work_budget(current, **{**WORK_INPUT, field: invalid}))[
            "status"
        ] == ("denied")
    missing_cost = {key: value for key, value in WORK_INPUT.items() if key != "refundable_deposit"}
    assert (await estimate_work_budget(current, **missing_cost))["status"] == "denied"
    assert (await estimate_work_budget(current, **{**WORK_INPUT, "daily_variable_cost": 1.001}))[
        "status"
    ] == "needs_clarification"
    missing_base = {key: value for key, value in ORDER_INPUT.items() if key != "fee_base"}
    assert (await estimate_order_contribution(current, **missing_base))["status"] == (
        "needs_clarification"
    )


@pytest.mark.asyncio
async def test_registry_uses_bound_authorization_before_budget_calculation():
    current = context({"estimate_work_budget"})
    registered = build_tools(current, [{"id": "estimate_work_budget"}])
    assert set(registered) == {"estimate_work_budget"}
    assert (await registered["estimate_work_budget"](**WORK_INPUT))["status"] == "success"
    assert (await estimate_order_contribution(current, **ORDER_INPUT))["status"] == "denied"
    unauthorized = context(set())
    assert (await estimate_work_budget(unauthorized, **WORK_INPUT))["status"] == "denied"


def test_native_schema_hides_context_and_keeps_unknown_costs_required():
    current = context({"estimate_work_budget", "estimate_order_contribution"})
    work_schema = FunctionTool(MethodType(estimate_work_budget, current)).input_schema
    assert "context" not in work_schema["properties"]
    assert set(work_schema["required"]) == set(WORK_INPUT)
    assert "monthly_income" in work_schema["properties"]
    order_schema = FunctionTool(MethodType(estimate_order_contribution, current)).input_schema
    assert set(order_schema["required"]) == {
        "received_amount",
        "goods_cost",
        "packaging_cost",
        "other_variable_cost",
    }
    assert order_schema["properties"]["amount_basis"]["enum"] == ["before_fees", "after_fees"]


def test_settled_amount_deducts_only_remaining_costs_without_reconstructing_fees():
    result = budget.estimate_order_contribution(**SETTLED_INPUT)
    calculation = result["data"]["calculation"]
    assert result["status"] == "success"
    assert calculation["amount_basis"] == "after_fees"
    assert calculation["order_contribution_yuan"] == 18
    assert calculation["additional_cost_to_deduct_yuan"] == 12
    assert calculation["service_fee_yuan"] is None
    assert calculation["delivery_cost_yuan"] is None
    assert calculation["total_variable_cost_yuan"] is None
    assert "到账金额" in calculation["formula"]
    assert any("不等于门店净利润" in note for note in calculation["assumptions"])


@pytest.mark.parametrize("field", ["fee_base", "service_fee_percent", "delivery_cost"])
@pytest.mark.parametrize("value", [0, 1])
def test_settled_amount_rejects_any_fee_input_even_explicit_zero(field, value):
    result = budget.estimate_order_contribution(**{**SETTLED_INPUT, field: value})
    assert result["status"] == "needs_clarification"
    assert "calculation" not in result["data"]


@pytest.mark.parametrize("field", ["fee_base", "service_fee_percent", "delivery_cost"])
def test_before_fees_requires_each_fee_condition_even_though_schema_allows_omission(field):
    inputs = {key: value for key, value in ORDER_INPUT.items() if key != field}
    assert budget.estimate_order_contribution(**inputs)["status"] == "needs_clarification"


@pytest.mark.parametrize("invalid", ["unknown", None, True, 0])
def test_unknown_amount_basis_cannot_choose_a_calculation(invalid):
    result = budget.estimate_order_contribution(**{**SETTLED_INPUT, "amount_basis": invalid})
    assert result["status"] == "needs_clarification"


def test_settled_amount_preserves_zero_loss_and_unknown_cost_boundaries():
    for values, expected in (({"received_amount": 0}, -12), ({"goods_cost": 40}, -12)):
        result = budget.estimate_order_contribution(**{**SETTLED_INPUT, **values})
        assert result["data"]["calculation"]["order_contribution_yuan"] == expected
    for value in (None, True, float("nan"), 1.001):
        result = budget.estimate_order_contribution(**{**SETTLED_INPUT, "goods_cost": value})
        assert result["status"] == "needs_clarification"


@pytest.mark.asyncio
async def test_native_tool_supports_settled_amount_but_rejects_mixed_or_missing_conditions():
    current = context({"estimate_order_contribution"})
    result = await estimate_order_contribution(current, **SETTLED_INPUT)
    assert result["data"]["calculation"]["order_contribution_yuan"] == 18
    assert (await estimate_order_contribution(current, **SETTLED_INPUT, delivery_cost=0))[
        "status"
    ] == "needs_clarification"
    missing_cost = {key: value for key, value in SETTLED_INPUT.items() if key != "goods_cost"}
    assert (await estimate_order_contribution(current, **missing_cost))["status"] == "denied"
    assert (
        await estimate_order_contribution(current, **{**SETTLED_INPUT, "amount_basis": "unknown"})
    )["status"] == "denied"
