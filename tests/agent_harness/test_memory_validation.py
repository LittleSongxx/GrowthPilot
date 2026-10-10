"""在写入边界校验客户或模型提供的记忆值。"""

import pytest

from backend.agent_harness.memory.validation import validate_value


@pytest.mark.parametrize(
    ("field", "valid", "invalid"),
    [
        ({"id": "age", "type": "integer", "minimum": 18, "maximum": 80}, 50, True),
        ({"id": "cost", "type": "number", "minimum": 0}, 10.5, float("nan")),
        ({"id": "ready", "type": "boolean"}, False, "false"),
        ({"id": "city", "type": "enum", "values": ["北京", "长沙"]}, "长沙", "深圳"),
        ({"id": "note", "type": "string", "max_length": 10}, "咨询费用", " "),
        ({"id": "date", "type": "date"}, "2026-10-30", "2026-02-30"),
        ({"id": "at", "type": "datetime"}, "2026-10-30T08:00:00Z", "2026-10-30T08:00:00"),
    ],
)
def test_customer_values_keep_types_ranges_and_timezones(field, valid, invalid):
    assert validate_value(field, valid) == valid
    with pytest.raises(ValueError):
        validate_value(field, invalid)
