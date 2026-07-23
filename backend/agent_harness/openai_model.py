"""统一 OpenAI 兼容消息格式，模型调用及流式工具解析仍由 AgentScope 负责。"""

from agentscope.formatter import DeepSeekChatFormatter, OpenAIChatFormatter


class OpenAICompatibleFormatter(OpenAIChatFormatter):
    """保留项目里的中文角色名，同时发送各服务都能接受的纯文本消息。"""

    preserve_reasoning_content: bool = False

    async def format(self, msgs):
        # DeepSeek 思考模式要求工具调用的后续请求带回 reasoning_content。
        if self.preserve_reasoning_content:
            messages = await DeepSeekChatFormatter().format(msgs)
        else:
            messages = await super().format(msgs)
        for message in messages:
            # 协议里的 name 有字符限制，角色身份已由 role 和 tool_call_id 表达。
            message.pop("name", None)
            content = message.get("content")
            if isinstance(content, list) and all(part.get("type") == "text" for part in content):
                message["content"] = "\n".join(part["text"] for part in content)
        return messages
