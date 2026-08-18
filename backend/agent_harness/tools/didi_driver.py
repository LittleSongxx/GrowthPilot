"""滴滴司机工具：服务点、计费参考与租车方案。"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from .boundary import business_tool


@business_tool
async def search_driver_service_points(
    context, location: Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")]
) -> dict:
    """查询司机服务点及可咨询事项；未返回的具体地址、联系方式和距离均不可推断。

    Args:
        location: 客户已提供的城市、区县或商圈；城市级已足够查询，仅无地点或地点冲突时澄清。
    """
    return context.catalog().query("driver_service_point", location=location)


@business_tool
async def query_driver_earnings(
    context,
    area: Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")],
    service_type: Annotated[str, Field(min_length=1, max_length=40)],
) -> dict:
    """查询车型服务的计费参考范围与平台服务费条件，不把乘客车费当司机净收入。

    Args:
        area: 客户已提供的城市、区县或商圈；城市级可直接查询，未精确匹配时保留结果的参考范围和条件。
        service_type: 客户关注的业务类型，例如快车；未精确匹配时返回请求条件与计算假设，不冒充该业务的实际规则。
    """
    return context.catalog().query("driver_earnings", location=area, service_type=service_type)


@business_tool
async def search_car_rentals(
    context,
    location: Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")],
    vehicle_type: str | None = None,
) -> dict:
    """查询司机租车方案及成本条件，不证明车辆或驾驶员已满足准入。

    Args:
        location: 客户已提供的城市、区县或商圈；城市级可直接查询，未精确匹配时依据返回的适用范围解释。
        vehicle_type: 已明确的能源类型，例如纯电动或燃油；省略时展示可用方案。
    """
    return context.catalog().query("car_rental", location=location, vehicle_type=vehicle_type)
