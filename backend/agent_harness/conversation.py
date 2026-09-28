"""一次会话：接收客户消息，交给 AgentScope，保存回复，结束时释放资源。"""

from __future__ import annotations

import asyncio

import json

import time

import uuid

from functools import cached_property

from agentscope.message import TextBlock, UserMsg

from agentscope.middleware import MiddlewareBase

from agentscope.model import ChatResponse

from agentscope.state import AgentState

from . import agent

from .business_data import BusinessCatalog

from .prompts import system_prompt

from .runtime.events import ConversationEvents

from .tools import ToolContext, build_tools

class Conversation(MiddlewareBase):
    def __init__(self, service, ctx, *, settings, store, search_policy):
        self.service, self.ctx = service, ctx
        self.settings, self.store = settings, store
        self.id = ctx.session_id
        self.channel = ctx.channel
        self.lock = asyncio.Lock()
        self.agent = self.task = None
        self.input_task = None
        self.speech_tasks = set()
        self.voice_generation = 0
        self.interrupt_lock = asyncio.Lock()
        self.interrupting = False
        self.reply_interrupted = False
        self.end_call_message = None
        self.model_client = None
        self.expiry_task = None
        self.closing = False
        self.stop_done = asyncio.Event()
        self.stop_owner = None
        if service.read_only or service.session(ctx)["state"] == "closed":
            self.stop_done.set()
        self.events = ConversationEvents(self)
        context = ToolContext(
            search_policy=search_policy,
            catalog=lambda: self.catalog,
            authorize=self.tool_denial,
            emit=self.emit,
            send_download_sms=lambda: service.send_download_sms(ctx),
            request_end_call=self.request_end_call,
            timeout_seconds=min(10, service.scenario["policies"]["tool_timeout_seconds"]),
        )
        self.tools = build_tools(context, service.scenario["tools"])

    @cached_property
    def catalog(self):
        return BusinessCatalog.from_scenario(self.service.scenario, clock=self.service.clock)

    def start_agent(self):
        model = agent.create_model(self.settings)
        self.model_client = model.client
        self.agent = agent.create_agent(
            model=model,
            prompt=system_prompt(self.service.scenario, self.business_summary()),
            toolkit=agent.create_toolkit(self.tools, self.service.scenario),
            state=self.framework_state(),
            max_iterations=self.settings.agent_max_iterations,
            middlewares=[self],
        )

    def request_end_call(self, message):
        """只记录结束意图，实际挂断等待告别播放完成。"""
        self.end_call_message = message

    async def on_model_call(self, agent, input_kwargs, next_handler):
        """结束工具已执行时直接返回告别语，其余请求交给模型。"""
        if self.end_call_message is not None:
            # 工具结果已由框架保存；本地短路不会向供应商发起第二次生成请求。
            return ChatResponse(content=[TextBlock(text=self.end_call_message)], is_last=True)
        return await next_handler()

    @property
    def end_after_playback(self):
        return self.end_call_message is not None

    @property
    def scope(self):
        return {
            key: getattr(self.ctx, key)
            for key in ("workspace_id", "customer_id", "scenario_id", "snapshot_hash", "run_id")
        }

    @property
    def remaining_seconds(self):
        limit = min(
            300,
            self.settings.session_max_seconds,
            self.service.scenario["policies"]["session_timeout_seconds"],
        )
        return limit - (time.time() - self.service.session(self.ctx)["started_at"])

    def emit(self, kind, data):
        return self.store.emit(self.id, kind, data)


