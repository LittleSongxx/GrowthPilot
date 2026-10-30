"""复用真实会话，仅从 Agent 的执行预算中扣除模拟客户模型的等待时间。"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, contextmanager

from backend.agent_harness.conversation import Conversation
from backend.agent_harness.memory.worker import ProfileWorker
from backend.agent_harness.runtime.session_manager import SessionManager


class EvaluationClock:
    """每个评测案例独享的时钟；真实执行时间持续前进，客户推理期间暂停。"""

    def __init__(self, *, epoch=None, monotonic=None):
        self._monotonic = monotonic or time.monotonic
        self._epoch = time.time() if epoch is None else epoch
        self._started = self._monotonic()
        self._paused_seconds = 0.0
        self._paused_at = None
        self._pause_depth = 0

    def __call__(self):
        current = self._monotonic() if self._paused_at is None else self._paused_at
        return self._epoch + current - self._started - self._paused_seconds

    @contextmanager
    def pause(self):
        """嵌套暂停只扣除一次等待时长，异常退出也恢复计时。"""
        if self._pause_depth == 0:
            self._paused_at = self._monotonic()
        self._pause_depth += 1
        try:
            yield
        finally:
            self._pause_depth -= 1
            if self._pause_depth == 0:
                self._paused_seconds += self._monotonic() - self._paused_at
                self._paused_at = None


class EvaluationConversation(Conversation):
    """保持真实工具、模型与状态，仅让会话和业务鉴权使用同一评测时钟。"""

    @property
    def remaining_seconds(self):
        limit = min(
            300,
            self.settings.session_max_seconds,
            self.service.scenario["policies"]["session_timeout_seconds"],
        )
        return limit - (self.service.clock() - self.service.session(self.ctx)["started_at"])


class EvaluationSessionManager(SessionManager):
    """每个案例使用独立服务及时钟，避免客户推理触发生产会话的超时任务。"""

    def __init__(self, settings, services):
        super().__init__(settings, services)
        # 一次评测共用数据库；每个会话只处理自己的画像任务，结束时不影响其他案例。
        self.profile_worker = ProfileWorker(settings, session_ids=())

    async def create(self, scenario_id, customer_id, channel="text", run_id=None):
        conversation = await super().create(scenario_id, customer_id, channel, run_id)
        # 测试注入的提取器也沿用会话作用域，不能因替换 worker 恢复为全库消费。
        self.profile_worker.session_ids = (self.profile_worker.session_ids or set()) | {conversation.id}
        return conversation

    def _conversation(self, service, ctx):
        return EvaluationConversation(
            service,
            ctx,
            settings=self.settings,
            store=self.store,
            search_policy=lambda query: self.policy_index.search(query, service.scenario.id),
        )

    @asynccontextmanager
    async def customer_wait(self, conversation):
        """客户模型等待期间撤销计时器，恢复后继续消耗原有剩余预算。"""
        clock = conversation.service.clock
        if not isinstance(clock, EvaluationClock):
            raise TypeError("评测会话必须绑定 EvaluationClock")
        expiry = conversation.expiry_task
        conversation.expiry_task = None
        try:
            with clock.pause():
                if expiry is not None:
                    expiry.cancel()
                    await asyncio.gather(expiry, return_exceptions=True)
                yield
        finally:
            if (
                not conversation.service.read_only
                and not conversation.closing
                and conversation.service.session(conversation.ctx)["state"] == "active"
            ):

                async def expire():
                    await asyncio.sleep(max(0, conversation.remaining_seconds))
                    await conversation.stop("session_timeout")

                conversation.expiry_task = asyncio.create_task(
                    expire(), name=f"expiry-{conversation.id}"
                )
