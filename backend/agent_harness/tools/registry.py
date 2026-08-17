"""按场景开放业务工具，并在宿主提供通话能力时注册结束工具。"""

from types import MethodType

from . import common, didi_driver, financial_consultation, meituan_merchant, meituan_rider
from .boundary import ToolContext

TOOL_FUNCTIONS = {
    function.__name__: function
    for function in (
        common.search_policy,
        common.send_download_sms,
        meituan_rider.search_nearby_stations,
        meituan_rider.query_delivery_pay,
        meituan_rider.search_vehicle_rentals,
        didi_driver.search_driver_service_points,
        didi_driver.query_driver_earnings,
        didi_driver.search_car_rentals,
        meituan_merchant.query_delivery_options,
        meituan_merchant.query_merchant_fee_plans,
        meituan_merchant.query_merchant_offers,
        financial_consultation.query_product_options,
        financial_consultation.query_financial_offers,
        financial_consultation.estimate_financing_cost,
        common.estimate_work_budget,
        meituan_merchant.estimate_order_contribution,
    )
}


def build_tools(context: ToolContext, specs: list[dict]) -> dict:
    """绑定当前会话的能力，首参 context 不会暴露为模型参数。"""
    tools = {
        item["id"]: MethodType(TOOL_FUNCTIONS[item["id"]], context)
        for item in specs
        if item["id"] not in {"read_memory", "write_memory"}
    }
    if context.request_end_call is not None:
        tools["end_call"] = MethodType(common.end_call, context)
    return tools
