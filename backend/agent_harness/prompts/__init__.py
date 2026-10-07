"""用场景和客户资料填充 system.md，提示词的结构与规则统一在模板中维护。"""

import json
from pathlib import Path
from string import Template

from ..tools.boundary import model_projection

PROMPT_VERSION = "growth-agent-7.8"
SYSTEM_PROMPT = Path(__file__).with_name("system.md").read_text(encoding="utf-8")


def system_prompt(scenario, customer_context):
    """筛选模型可见资料并一次性替换占位符，不再追加提示词段落。"""
    identity = scenario["identity"]
    # 严格替换会让遗漏的模板变量直接报错；资料中的占位符样式文本不会再次展开。
    return Template(SYSTEM_PROMPT).substitute(
        role=identity["role"],
        app_name=identity["app_name"],
        customer_background=identity["background"],
        customer_context=json.dumps(
            model_projection(customer_context), ensure_ascii=False, indent=2
        ),
    )
