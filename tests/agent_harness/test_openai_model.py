"""通过真实 OpenAI SDK 的离线传输验证协议参数、消息和流式工具调用。"""

import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from agentscope.message import Msg, TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock
from agentscope.tool import ToolChoice

from backend.agent_harness.agent import create_model


def stream_body(deltas, *, finish_reason="stop", usage=None):
    """构造与服务端相同的 SSE 数据，让 SDK 负责解析事件。"""
    chunks = [
        {
            "id": "offline-response", "object": "chat.completion.chunk", "created": 1,
            "model": "compatible-demo", "choices": [{"index": 0, "delta": delta}],
        }
        for delta in deltas
    ]
    chunks[-1]["choices"][0]["finish_reason"] = finish_reason
    if usage:
        chunks.append({**chunks[-1], "choices": [], "usage": usage})
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


@pytest.fixture
async def model_factory(monkeypatch):
    """只替换网络传输，保留模型、消息格式化和 SDK 的实际实现。"""
    original_client = openai.AsyncClient
    clients = []

    def build(*, parameters=None, replies=None):
        requests = []
        responses = list(replies or [stream_body([{"content": "收到"}])])

        def handle(request):
            requests.append({"url": str(request.url), "body": json.loads(request.content)})
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=responses.pop(0)
            )

        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))

        def client(**kwargs):
            result = original_client(**kwargs, http_client=http)
            clients.append(result)
            return result

        monkeypatch.setattr(openai, "AsyncClient", client)
        settings = SimpleNamespace(
            agent_api_key="offline-test", agent_base_url="https://models.example.test/v1",
            agent_model="compatible-demo", agent_max_tokens_param="max_tokens",
            agent_extra_body={}, agent_temperature=0.0, agent_max_tokens=700,
            agent_reasoning_effort=None,
        )
        for name, value in (parameters or {}).items():
            setattr(settings, name, value)
        return create_model(settings), requests

    yield build
    for client in clients:
        await client.close()


@pytest.mark.parametrize(
    ("parameters", "expected", "absent"),
    [
        ({}, {"max_tokens": 700}, ["thinking", "enable_thinking", "reasoning_effort"]),
        ({"agent_extra_body": {"thinking": {"type": "disabled"}}},
         {"max_tokens": 700, "thinking": {"type": "disabled"}},
         ["max_completion_tokens", "enable_thinking", "reasoning_effort"]),
        ({"agent_extra_body": {"enable_thinking": False}},
         {"max_tokens": 700, "enable_thinking": False}, ["max_completion_tokens", "thinking"]),
        ({"agent_extra_body": {"thinking": False}}, {"max_tokens": 700, "thinking": False},
         ["reasoning_effort", "enable_thinking"]),
        ({"agent_max_tokens_param": "max_completion_tokens", "agent_temperature": None},
         {"max_completion_tokens": 700}, ["max_tokens", "thinking", "temperature"]),
    ],
)
async def test_explicit_parameters_and_chinese_messages_reach_the_sdk(
    model_factory, parameters, expected, absent
):
    model, requests = model_factory(parameters=parameters)
    messages = [
        Msg(name="系统", role="system", content=[TextBlock(text="帮助用户了解业务")]),
        Msg(name="业务回访助手", role="assistant", content=[TextBlock(text="您好")]),
        Msg(name="客户", role="user", content=[TextBlock(text="你好")]),
    ]
    chunks = [chunk async for chunk in await model(messages)]
    request = requests[0]
    assert request["url"] == "https://models.example.test/v1/chat/completions"
    body = request["body"]
    assert body["model"] == "compatible-demo" and body["stream"] is True
    assert body.items() >= expected.items()
    assert not any(field in body for field in absent)
    assert all("name" not in message and isinstance(message["content"], str)
               for message in body["messages"])
    assert messages[1].name == "业务回访助手"
    assert chunks[-1].is_last and chunks[-1].content[0].text == "收到"


async def test_streamed_tool_arguments_and_results_round_trip(model_factory):
    tool_reply = stream_body(
        [
            {"reasoning_content": "先查询业务条件"},
            {"tool_calls": [{"index": 0, "id": "lookup-1", "type": "function",
                             "function": {"name": "lookup", "arguments": '{"city":'}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": '"杭州"}'}}]},
        ],
        finish_reason="tool_calls",
        usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
    )
    model, requests = model_factory(replies=[tool_reply, stream_body([{"content": "已查到条件"}])])
    messages = [Msg(name="客户", role="user", content=[TextBlock(text="查询杭州的条件")])]
    tools = [{"type": "function", "function": {
        "name": "lookup", "description": "查询条件", "parameters": {
            "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]
        }
    }}]
    chunks = [chunk async for chunk in await model(
        messages, tools=tools, tool_choice=ToolChoice(mode="lookup")
    )]
    result = chunks[-1]
    tool = next(block for block in result.content if isinstance(block, ToolCallBlock))
    assert tool.id == "lookup-1" and tool.name == "lookup"
    assert json.loads(tool.input) == {"city": "杭州"}
    assert result.usage.input_tokens == 20 and result.usage.output_tokens == 8
    assert requests[0]["body"]["tool_choice"] == {
        "type": "function", "function": {"name": "lookup"}
    }
    assert requests[0]["body"]["tools"] == tools
    messages.extend([
        Msg(name="业务回访助手", role="assistant", content=result.content),
        Msg(name="工具", role="assistant", content=[
            ToolResultBlock(id=tool.id, name=tool.name, output="杭州支持报名")
        ]),
    ])
    reply = [chunk async for chunk in await model(messages)]
    history = requests[1]["body"]["messages"]
    assert history[-2]["tool_calls"][0]["id"] == "lookup-1"
    assert history[-1] == {"role": "tool", "tool_call_id": "lookup-1", "content": "杭州支持报名"}
    assert all("reasoning_content" not in message and "name" not in message for message in history)
    assert reply[-1].content[0].text == "已查到条件"


async def test_deepseek_thinking_preserves_reasoning_in_tool_history(model_factory):
    model, requests = model_factory(parameters={
        "agent_extra_body": {"thinking": {"type": "enabled"}},
        "agent_reasoning_effort": "high", "agent_temperature": None,
    })
    messages = [
        Msg(name="业务回访助手", role="assistant", content=[
            ThinkingBlock(thinking="需要查询条件"),
            ToolCallBlock(id="lookup-1", name="lookup", input="{}"),
        ]),
        Msg(name="工具", role="assistant", content=[
            ToolResultBlock(id="lookup-1", name="lookup", output="支持报名")
        ]),
    ]
    _ = [chunk async for chunk in await model(messages)]
    body = requests[0]["body"]
    assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "high"
    assert "temperature" not in body
    assert body["messages"][0]["reasoning_content"] == "需要查询条件"
    assert body["messages"][1]["tool_call_id"] == "lookup-1"


def test_missing_credential_fails_before_constructing_a_client(monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("缺少密钥时不应创建客户端")

    monkeypatch.setattr(openai, "AsyncClient", forbidden)
    with pytest.raises(RuntimeError, match="AGENT_API_KEY"):
        create_model(SimpleNamespace(agent_api_key=""))
