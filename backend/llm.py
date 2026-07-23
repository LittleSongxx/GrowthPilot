"""按调用用途构造 OpenAI Chat Completions 参数，不根据供应商品牌选择模型类。"""

from copy import deepcopy

from backend.settings import MODEL_ROLES


def completion_parameters(settings, *, role="agent"):
    """只有显式配置的参数才会发送；特殊能力通过各用途的 extra_body 透传。"""
    if role not in MODEL_ROLES:
        raise ValueError("未知模型用途。")
    token_param = getattr(settings, f"{role}_max_tokens_param", "max_tokens")
    request = {token_param: getattr(settings, f"{role}_max_tokens")}
    temperature = getattr(settings, f"{role}_temperature", None)
    if temperature is not None:
        request["temperature"] = temperature
    effort = getattr(settings, f"{role}_reasoning_effort", None)
    if effort:
        request["reasoning_effort"] = effort
    extra = deepcopy(getattr(settings, f"{role}_extra_body", {}))
    if extra:
        request["extra_body"] = extra
    return request


def judge_generation(settings):
    """评审诊断和运行清单共用实际请求参数，避免把省略的温度记录成已生效。"""
    request = completion_parameters(settings, role="judge")
    extra = request.get("extra_body", {})
    thinking_option = extra.get("thinking")
    thinking = ((isinstance(thinking_option, dict) and thinking_option.get("type") == "enabled")
                or extra.get("enable_thinking") is True
                or request.get("reasoning_effort") not in {None, "none"})
    return {
        "thinking": thinking,
        "reasoning_effort": request.get("reasoning_effort"),
        "temperature": request.get("temperature"),
        "configured_temperature": settings.judge_temperature,
        "temperature_used": "temperature" in request,
        "max_tokens": settings.judge_max_tokens,
    }
