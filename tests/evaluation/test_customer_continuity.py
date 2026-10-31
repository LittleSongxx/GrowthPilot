"""离线验证同一客户跨通话延续可见信息，同时每通调用诊断独立。"""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from evaluation.dialogue import customer


@pytest.fixture
def local_customer_client(monkeypatch):
    state = {"clients": [], "requests": [], "connections": []}

    class Client:
        def __init__(self, **kwargs):
            state["connections"].append(kwargs)
            self.closed = False
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            state["clients"].append(self)

        async def create(self, **kwargs):
            state["requests"].append(deepcopy(kwargs))
            return SimpleNamespace(
                usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 7, "completion_tokens": 2}),
                choices=[SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content='{"text":"我先看一下短信，再见", "done":true}'),
                )],
            )

        async def close(self):
            self.closed = True

    monkeypatch.setattr(customer, "AsyncOpenAI", Client)
    settings = SimpleNamespace(
        customer_api_key="local-test", customer_base_url="https://example.com",
        customer_model="local-test-model", customer_temperature=0.7, customer_max_tokens=700,
    )
    profile = {
        "profile": "上海的兼职骑手", "initial_state": "未收到下载短信",
        "concerns": ["暂时没空"], "acceptance_conditions": ["可先看短信"],
        "refusal_conditions": [], "disclosure_rules": ["被问到城市时回答"],
        "goal": "隐藏目标不能传给客户",
    }
    return settings, profile, state


async def test_next_call_keeps_last_reply_sms_and_boundaries_with_separate_usage(local_customer_client):
    settings, profile, state = local_customer_client
    first = customer.OpenAICompatibleCustomer(settings, profile, call_number=1)
    await first.respond("请问你是在上海吗？")
    first_calls = first.calls
    sms = [{"type": "download_sms", "app_name": "美团众包", "delivery_status": "sent"}]
    first.end_call("下载短信已经发送，今天先不打扰您了。", sms)
    history = first.export_history()
    frozen_history = deepcopy(history)
    await first.close()

    second = customer.OpenAICompatibleCustomer(settings, profile, history=history, call_number=2)
    assert second.calls == []
    assert first_calls is not second.calls
    assert second.messages[1:-1] == frozen_history
    await second.respond("上次给您发的下载短信看过了吗？")
    second.end_call()
    await second.close()

    assert len(state["requests"]) == 2
    assert len(first_calls) == len(second.calls) == 1
    assert first_calls[0]["usage"]["prompt_tokens"] == 7
    assert history == frozen_history
    received = state["requests"][1]["messages"]
    payloads = [json.loads(message["content"]) for message in received[1:]]
    assert [item["call_boundary"] for item in payloads if "call_boundary" in item] == [
        "start", "end", "start",
    ]
    assert [item["call_number"] for item in payloads if "call_number" in item] == [1, 2]
    assert {"agent_message": "下载短信已经发送，今天先不打扰您了。", "visible_events": sms} in payloads
    assert {"text": "我先看一下短信，再见", "done": True} in payloads
    assert "隐藏目标不能传给客户" not in json.dumps(received, ensure_ascii=False)
    assert all(client.closed for client in state["clients"])


async def test_history_export_cannot_mutate_previous_call_or_share_between_customers(local_customer_client):
    settings, profile, state = local_customer_client
    first = customer.OpenAICompatibleCustomer(settings, profile)
    await first.respond("上次登记的是上海。")
    first.end_call()
    history = first.export_history()
    second = customer.OpenAICompatibleCustomer(settings, profile, history=history, call_number=2)
    unrelated = customer.OpenAICompatibleCustomer(settings, profile)
    history[0]["content"] = "外部修改不应影响客户上下文"
    assert "外部修改" not in json.dumps(first.messages, ensure_ascii=False)
    assert "外部修改" not in json.dumps(second.messages, ensure_ascii=False)
    assert "上次登记的是上海" in json.dumps(second.messages, ensure_ascii=False)
    assert "上次登记的是上海" not in json.dumps(unrelated.messages, ensure_ascii=False)
    assert all(message["role"] != "system" for message in first.export_history())
    assert len([message for message in second.messages if message["role"] == "system"]) == 1
    await first.close()
    await second.close()
    await unrelated.close()
    assert all(client.closed for client in state["clients"])


async def test_end_call_without_unseen_reply_only_records_boundary(local_customer_client):
    settings, profile, state = local_customer_client
    actor = customer.OpenAICompatibleCustomer(settings, profile)
    before = len(actor.messages)
    actor.end_call()
    assert len(actor.messages) == before + 1
    assert json.loads(actor.messages[-1]["content"]) == {"call_boundary": "end"}
    assert actor.calls == []
    assert state["requests"] == []
    await actor.close()


@pytest.mark.parametrize("model,token_parameter,temperature", [
    ("gpt-4.1-mini", "max_tokens", 0.6),
    ("qwen-plus", "max_tokens", 0.8),
    ("gpt-5-mini", "max_completion_tokens", None),
])
async def test_customer_compatible_models_omit_deepseek_parameters(
    local_customer_client, model, token_parameter, temperature,
):
    settings, profile, state = local_customer_client
    settings.customer_model = model
    settings.customer_max_tokens_param = token_parameter
    settings.customer_temperature = temperature
    settings.customer_max_tokens = 1200
    actor = customer.OpenAICompatibleCustomer(settings, profile)
    await actor.respond("现在方便沟通吗？")
    await actor.close()

    request = state["requests"][0]
    assert state["connections"][0]["api_key"] == settings.customer_api_key
    assert state["connections"][0]["base_url"] == settings.customer_base_url
    assert request["model"] == model
    if temperature is None:
        assert "temperature" not in request
    else:
        assert request["temperature"] == temperature
    assert request[token_parameter] == settings.customer_max_tokens
    assert len({"max_tokens", "max_completion_tokens"} & request.keys()) == 1
    assert "thinking" not in request.get("extra_body", {})
    assert "reasoning_effort" not in request


async def test_customer_resolves_inheritance_once_per_call(local_customer_client):
    settings, profile, state = local_customer_client
    settings.customer_api_key = settings.customer_base_url = settings.customer_model = ""
    settings.agent_api_key = "first-agent-secret"
    settings.agent_base_url = "https://first.example/v1"
    settings.agent_model = "first-model"
    first = customer.OpenAICompatibleCustomer(settings, profile)

    settings.agent_api_key = "second-agent-secret"
    settings.agent_base_url = "https://second.example/v1"
    settings.agent_model = "second-model"
    await first.respond("现在方便吗？")
    await first.close()
    second = customer.OpenAICompatibleCustomer(settings, profile)
    await second.respond("现在方便吗？")
    await second.close()

    assert [request["model"] for request in state["requests"]] == ["first-model", "second-model"]
    assert [connection["api_key"] for connection in state["connections"]] == [
        "first-agent-secret", "second-agent-secret",
    ]
    assert [connection["base_url"] for connection in state["connections"]] == [
        "https://first.example/v1", "https://second.example/v1",
    ]
    assert first.calls[0]["model"] == "first-model"
    assert second.calls[0]["model"] == "second-model"
