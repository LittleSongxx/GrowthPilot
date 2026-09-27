"""组装模型、工具、Skill 和对话状态；AgentScope 负责模型与工具的循环。"""

from agentscope.agent import Agent, ModelConfig, ReActConfig
from agentscope.credential import OpenAICredential
from agentscope.model import OpenAIChatModel
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, Toolkit

from backend.llm import completion_parameters

from .openai_model import OpenAICompatibleFormatter
from .skills import skill_loaders

FRAMEWORK_COMMIT = "e4c1e074dee3f0f6cca28ca29e934c4ec0605354"


def create_model(settings):
    if not settings.agent_api_key:
        raise RuntimeError("缺少 AGENT_API_KEY；请配置服务端 .env。")
    generation = completion_parameters(settings, role="agent")
    extra_body = generation.pop("extra_body", {})
    return OpenAIChatModel(
        credential=OpenAICredential(
            api_key=settings.agent_api_key, base_url=settings.agent_base_url
        ),
        model=settings.agent_model,
        # 框架默认使用 max_completion_tokens；统一从配置注入，兼容只接受 max_tokens 的服务。
        parameters=OpenAIChatModel.Parameters(),
        extra_body={**generation, **extra_body},
        formatter=OpenAICompatibleFormatter(
            preserve_reasoning_content=(
                isinstance(extra_body.get("thinking"), dict)
                and extra_body["thinking"].get("type") == "enabled"
            ),
        ),
        stream=True,
        max_retries=1,
        client_kwargs={"timeout": 45},
    )


def create_toolkit(tools, scenario):
    return Toolkit(
        tools=[
            FunctionTool(
                function,
                is_concurrency_safe=False,
                permission=PermissionDecision(
                    behavior=PermissionBehavior.ALLOW,
                    message="宿主已绑定工具能力，每次调用仍校验当前会话及业务权限。",
                ),
            )
            for function in tools.values()
        ],
        skills_or_loaders=skill_loaders(scenario),
    )


def create_agent(model, prompt, toolkit, state, max_iterations, middlewares=None):
    return Agent(
        "业务回访助手",
        prompt,
        model,
        toolkit=toolkit,
        state=state,
        middlewares=middlewares,
        # 取消后保留框架的工具收尾，并将取消继续交给宿主，避免误触发语音播放。
        react_config=ReActConfig(max_iters=max_iterations, interruption_raise_cancelled_error=True),
        model_config=ModelConfig(),
    )
