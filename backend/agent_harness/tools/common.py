"""跨场景公共工具：政策检索、下载短信、通话结束和骑手/司机条件预算。"""

from __future__ import annotations

import asyncio
from typing import Annotated

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk
from pydantic import Field

from ..business_data import budget
from .boundary import business_tool, denied

Amount = Annotated[int | float, Field(ge=0, le=100000000, allow_inf_nan=False)]


@business_tool
async def search_policy(
    context, query: Annotated[str, Field(min_length=1, max_length=1000, pattern=r"\S")]
) -> dict:
    """用自然语言检索当前业务的官方政策条款，返回原文、适用范围和来源。

    knowledge_not_indexed、scenario_not_indexed 或 index_rebuild_required 表示知识库
    尚未准备好，应停止本轮检索并说明暂时无法核实；更换关键词不能解决这些错误。

    Args:
        query: 客户的具体政策问题，可包含已明确的城市、业务类型等条件；不要包含姓名、电话等个人资料。证据不足时可换一种问法补查，不限于预设主题。
    """
    return await asyncio.to_thread(context.search_policy, query)


@business_tool
async def send_download_sms(context) -> dict:
    """客户同意接收后，向当前客户发送当前业务的 App 下载短信。

    收件客户和下载入口由当前会话确定，无需询问手机号或提供链接。
    根据返回结果告知客户发送情况；发送成功不代表客户已收到、下载或注册。
    同一次通话重复调用会返回已有发送记录。
    """
    if context.send_download_sms is None:
        return denied("sms_not_available")
    return context.send_download_sms()


async def end_call(
    context, message: Annotated[str, Field(min_length=1, max_length=300, pattern=r"\S")]
) -> ToolChunk:
    """决定自然收尾时调用，并把完整的一句告别放入 message；不能仅口头告别而不调用。

    当前事项已办妥且客户确认没有新诉求，或客户拒绝继续、表示没空、告别时使用。
    短信发送成功后仍需等待客户反馈；客户确认收到但继续提问时，应先解答，不要结束。
    先完成本轮其他业务操作，再单独调用。message 会直接播放，不再生成后续回答。
    告别播放完成后才挂断；客户插话会撤销结束请求并继续交流。

    Args:
        message: 直接对客户说的一句简短、自然的完整告别，不追加问题或新的邀请。
    """
    # 通话结束是宿主能力，只校验当前会话，不受场景业务工具清单限制。
    reason = context.authorize(None)
    if reason:
        return ToolChunk(content=[TextBlock(text=reason)], state=ToolResultState.DENIED)
    if context.request_end_call is None:
        return ToolChunk(
            content=[TextBlock(text="end_call_not_available")], state=ToolResultState.DENIED
        )
    context.request_end_call(message)
    return ToolChunk(content=[TextBlock(text=message)], state=ToolResultState.SUCCESS)


@business_tool
async def estimate_work_budget(
    context,
    work_days: Annotated[int, Field(ge=1, le=31)],
    monthly_fixed_cost: Amount,
    daily_variable_cost: Amount,
    refundable_deposit: Amount,
    monthly_income: Amount | None = None,
) -> dict:
    """计算骑手或司机的条件预算、固定成本每日分摊、成本覆盖门槛和押金现金占用，不预测收入。

    Args:
        work_days: 客户明确的每月实际出勤天数，一至三十一天，不用单日工作时长代替。
        monthly_fixed_cost: 客户确认的月固定成本总额，人民币元，最多两位小数；未知先澄清，可明确为零。
        daily_variable_cost: 客户确认的每出勤日变动成本总额，人民币元，最多两位小数；避免与固定成本重复，未知先澄清，可明确为零。
        refundable_deposit: 客户确认的可退押金金额，人民币元，最多两位小数；仅计现金占用，不计经营成本，未知先澄清，可明确为零。
        monthly_income: 客户确认的月收入假设，已扣平台费用但未扣经营成本，人民币元，最多两位小数；未知时不填，只计算成本和收入门槛，不能由计价参考猜出收入。
    """
    return budget.estimate_work_budget(
        work_days, monthly_fixed_cost, daily_variable_cost, refundable_deposit, monthly_income
    )
