"""美团骑手工具：服务站点、配送计酬与车辆租赁。"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from .boundary import business_tool


@business_tool
async def search_nearby_stations(
    context,
    location: Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")],
    delivery_type: Annotated[str, Field(min_length=1, max_length=40, pattern=r"\S")] | None = None,
) -> dict:
    """按客户地点查询骑手服务点与可咨询事项；支持商圈和城市，未提供的地址或距离须说明未知。

    Args:
        location: 客户已提供的地点，例如长沙、北京或北京朝阳区望京。城市级即可查询；无地点或地点冲突时先澄清。未精确匹配时按结果范围解释，不推定已有当地实体站点。
        delivery_type: 客户已明确的配送类型，如众包或专送；保留其他合法表达供工具查询，兼职不自动等于众包。省略时展示可用类型。
    """
    return context.catalog().query("rider_station", location=location, delivery_type=delivery_type)


@business_tool
async def query_delivery_pay(
    context,
    area: Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")],
    delivery_type: Annotated[str, Field(min_length=1, max_length=40, pattern=r"\S")],
    station_id: str | None = None,
) -> dict:
    """查询地点与配送类型对应的计酬参考范围及条件，不承诺实际收入。

    Args:
        area: 客户已提供的城市、区县或商圈；城市级可直接查询，未精确匹配时保留结果的参考范围和条件。
        delivery_type: 客户明确的配送类型，未精确匹配时依据返回的计算条件说明，不能冒充该类型的实际规则。
        station_id: 可选，站点查询返回的原始站点 ID，不能虚构或替换区域。
    """
    return context.catalog().query(
        "delivery_pay", location=area, delivery_type=delivery_type, station_id=station_id
    )


@business_tool
async def search_vehicle_rentals(
    context,
    location: Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")],
    battery_included: bool | None = None,
) -> dict:
    """查询骑手电动车租赁方案，包含月租、押金、服务及未包含费用。

    Args:
        location: 客户已提供的城市、区县或商圈；城市级可直接查询，未精确匹配时依据返回的适用范围解释。
        battery_included: 是否要求包含电池服务，省略时保留两种方案供解释。
    """
    return context.catalog().query(
        "vehicle_rental", location=location, battery_included=battery_included
    )
