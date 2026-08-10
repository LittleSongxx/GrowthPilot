"""应用场景中明确配置的价格参数，并将金额舍入到分。"""

import copy
from decimal import ROUND_HALF_UP, Decimal


def apply_prices(kind, item, profile):
    parameters = profile["parameters"]
    original = {}

    def scale(container, key, parameter):
        if container[key] is None:
            return
        value = container[key]
        original[key] = copy.deepcopy(value)
        factor = Decimal(str(parameters[parameter]))
        if isinstance(value, dict):
            container[key] = {
                name: float(
                    (Decimal(str(number)) * factor).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                )
                for name, number in value.items()
            }
        else:
            container[key] = float(
                (Decimal(str(value)) * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )

    if kind == "delivery_pay":
        scale(item, "per_order_yuan", "rider_pay_multiplier")
    elif kind == "driver_earnings":
        for key in ("base_fare_yuan", "additional_km_yuan"):
            scale(item["fare_components"], key, "driver_fare_multiplier")
    elif kind in {"vehicle_rental", "car_rental"}:
        for key in ("monthly_rent_yuan", "deposit_yuan"):
            scale(item, key, "rental_multiplier")
    elif kind == "merchant_fee_plan":
        scale(item, "delivery_fee_yuan_per_order", "rider_pay_multiplier")
        original["technical_service_fee_percent"] = item["technical_service_fee_percent"]
        item["technical_service_fee_percent"] = float(
            Decimal(str(item["technical_service_fee_percent"]))
            + Decimal(str(parameters["merchant_fee_adjustment_pp"]))
        )
    if original:
        item["pricing_basis"] = {
            "base_values": original,
            "profile_parameters": copy.deepcopy(parameters),
            "method": "显式模板基础值按标注倍率相乘；商家技术服务费加示例百分点，金额四舍五入到分",
            "disclosure": "城市档位按参数配置，不反映实际价格或经济等级",
        }
