"""依据客户字段定义和原话，校验模型提出的记忆值。"""

import math

import re

from datetime import date, datetime

def validate_value(field, value):
    typ = field["type"]
    valid = {
        "string": lambda: isinstance(value, str),
        "enum": lambda: isinstance(value, str) and value in field["values"],
        "integer": lambda: type(value) is int,
        "number": lambda: type(value) in (int, float),
        "boolean": lambda: type(value) is bool,
        "date": lambda: isinstance(value, str),
        "datetime": lambda: isinstance(value, str),
    }[typ]()
    if not valid:
        raise ValueError(f"Invalid value type/range for {field['id']}")
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("Known values cannot be empty")
        if len(value) < field.get("min_length", 0) or len(value) > field.get("max_length", 10000):
            raise ValueError("String length outside field limits")
    if type(value) in (int, float) and (
        not math.isfinite(value)
        or value < field.get("minimum", float("-inf"))
        or value > field.get("maximum", float("inf"))
    ):
        raise ValueError("Number outside field limits")
    if typ == "date":
        date.fromisoformat(value)
    if typ == "datetime" and datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("datetime must have timezone")
    return value

