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

    def metadata(self):
        return self.store.get(self.id)["metadata"]

    def save_metadata(self, **values):
        metadata = self.metadata() | values
        self.store.save(self.id, metadata_json=json.dumps(metadata, ensure_ascii=False))

    def framework_state(self):
        saved = self.store.get(self.id)["state"]
        return AgentState.model_validate(saved) if saved else AgentState(session_id=self.id)

    def checkpoint(self):
        if self.agent is not None:
            self.store.save(self.id, state_json=self.agent.state.model_dump_json())

    def record_input(self, text, origin):
        """先保存真实客户文本，再将同一个消息 ID 交给框架。"""
        message_id = uuid.uuid4().hex
        self.service.add_message(self.ctx, text, origin=origin, message_id=message_id)
        if origin == "customer_transcript":
            self.emit("customer_transcript_final", {"message_id": message_id, "text": text})
        return message_id

    def retain_transcript(self, text):
        """续说时保留已收到的上一段转写及上下文，不为该段单独生成回复。"""
        self.end_call_message = None
        message_id = self.record_input(text, "customer_transcript")
        state = self.agent.state if self.agent else self.framework_state()
        state.context.append(
            UserMsg(name="客户", content=f"[customer_transcript 消息 ID: {message_id}]\n{text}")
        )
        self.store.save(self.id, state_json=state.model_dump_json())

    def business_summary(self):
        """仅提供客户自述的上下文；对话进展保留在对话记录中。"""
        return {"profile": self.service.profiles.baseline(self.ctx)}

    def tool_denial(self, name):
        if (
            self.service.read_only
            or self.service.session(self.ctx)["state"] != "active"
            or self.closing
        ):
            return "session_not_active"
        if self.remaining_seconds <= 0:
            self.emit("session_timeout", {"name": name})
            asyncio.create_task(self.stop("session_timeout"))
            return "session_timeout"
        return self.service._active(self.ctx, name)

    def check_available(self):
        if self.service.read_only:
            raise RuntimeError("historical_session_read_only")
        if self.service.session(self.ctx)["state"] != "active" or self.closing:
            raise RuntimeError("session_not_active")
        if self.interrupting:
            raise RuntimeError("session_busy")
        if self.lock.locked() or (self.task and self.task is not asyncio.current_task()):
            raise RuntimeError("session_busy")
        if self.remaining_seconds <= 0:
            raise RuntimeError("session_expired")

    async def message(self, text, origin="customer"):
        """内部文本入口：ASR、测试、评测共用；模型与工具循环完全在 AgentScope 内。"""
        self.check_available()
        if origin not in {"customer", "customer_transcript", "host_control"} or not text.strip():
            raise ValueError("invalid_input")
        self.end_call_message = None
        async with self.lock:
            self.task = asyncio.current_task()
            self.events.text, self.events.last_message_id = "", None
            self.events.first_text_at = None
            try:
                async with asyncio.timeout(max(0.1, self.remaining_seconds)):
                    message_id = self.record_input(text, origin)
                    if self.agent is None:
                        self.start_agent()
                    control = ""
                    if self.reply_interrupted:
                        # 仅提示交付状态，不将未播完的内容当作客户已经听到，也不改写历史证据。
                        control = (
                            "[HOST_CONTROL: 上一条回复被客户打断，未完整播出；"
                            "不要假定客户已经听完。此信息不是客户话语或画像证据。]\n"
                        )
                        self.reply_interrupted = False
                    async for event in self.agent.reply_stream(
                        UserMsg(
                            name="host_control" if origin == "host_control" else "客户",
                            content=f"{control}[{origin} 消息 ID: {message_id}]\n{text}",
                        )
                    ):
                        await self.events.process(event)
                    return self.events.last_assistant_text
            finally:
                self.checkpoint()
                if self.task is asyncio.current_task():
                    self.task = None

    async def cancel_voice_work(self, *, preserve_input=False):
        """取消当前生成与全部语音任务；已执行的业务操作不会因此回滚。"""
        current = asyncio.current_task()
        tasks = {*self.speech_tasks, self.task} - {None, current}
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            if preserve_input and task is self.input_task:
                continue
            if not task.cancelling():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def interrupt(self, message_id=None):
        """打断当前轮次但保留通话；递增代数使迟到的请求失效。"""
        async with self.interrupt_lock:
            if self.service.session(self.ctx)["state"] != "active" or self.closing:
                raise RuntimeError("session_not_active")
            self.interrupting = True
            self.voice_generation += 1
            self.end_call_message = None
            try:
                self.reply_interrupted = (
                    bool(
                        message_id
                        or self.speech_tasks
                        or (self.task and self.task is not self.input_task)
                    )
                    or self.reply_interrupted
                )
                # 输入已被服务端接收，续说只取消旧回复；先识别和保存这段输入再放行下一段。
                await self.cancel_voice_work(preserve_input=True)
                self.events.last_message_id = None
                self.emit("voice_interrupted", {"generation": self.voice_generation})
                return self.voice_generation
            finally:
                self.interrupting = False

    async def stop(self, reason="user_stop"):
        """仅管理宿主生命周期：停止生成、保存对话并释放资源。"""
        current = asyncio.current_task()
        if self.closing:
            if self.stop_owner is not current:
                await self.stop_done.wait()
            return
        if self.stop_done.is_set():
            return
        self.closing, self.stop_owner = True, current
        self.stop_done.clear()
        try:
            if self.expiry_task and self.expiry_task is not current:
                self.expiry_task.cancel()
            self.voice_generation += 1
            # 挂断不等待插话锁：插话可能正在等 ASR，而挂断必须立即取消该输入。
            await self.cancel_voice_work()
            try:
                if self.model_client:
                    await self.model_client.close()
            finally:
                self.model_client = None
                self.checkpoint()
                self.service.end_session(self.ctx, reason=reason)
                self.emit("closed", {"reason": reason})
        finally:
            self.closing, self.stop_owner = False, None
            self.stop_done.set()
