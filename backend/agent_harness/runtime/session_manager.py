"""管理会话集合：创建、读取已保存会话，以及应用退出时统一关闭。"""

import asyncio
import hashlib
import hmac

from ..conversation import Conversation
from ..memory.worker import ProfileWorker
from .service import BackendService
from .store import RuntimeStore


class SessionManager:
    def __init__(self, settings, services):
        self.settings, self.services = settings, services
        self.store = RuntimeStore(settings.app_db)
        self.sessions = {}
        self.frozen_services = {}
        self._policy_index = None
        self.profile_worker = ProfileWorker(settings)

    @property
    def policy_index(self):
        """所有会话共用检索入口；首次检索才加载，空知识库不会触发模型下载。"""
        if self._policy_index is None:
            from backend.agent_harness.rag.index import PolicyIndex

            self._policy_index = PolicyIndex(
                self.settings.runtime_dir / "knowledge", database=self.settings.app_db
            )
        return self._policy_index

    def token(self, sid):
        return hmac.new(
            self.settings.session_secret.encode(), sid.encode(), hashlib.sha256
        ).hexdigest()

    def _conversation(self, service, ctx):
        return Conversation(
            service,
            ctx,
            settings=self.settings,
            store=self.store,
            search_policy=lambda query: self.policy_index.search(query, service.scenario.id),
        )

    def get(self, sid):
        if sid not in self.sessions:
            saved = self.store.get(sid)
            service = self.services.get(saved["scenario_id"])
            if (
                service is None
                or service.scenario.snapshot_hash != saved["snapshot_hash"]
                or service.workspace_id != saved["workspace_id"]
            ):
                key = (saved["snapshot_hash"], saved["workspace_id"])
                if key not in self.frozen_services:
                    frozen = BackendService.saved_scenario(
                        self.settings.app_db, saved["snapshot_hash"]
                    )
                    self.frozen_services[key] = BackendService(
                        self.settings.app_db,
                        frozen,
                        workspace_id=saved["workspace_id"],
                        read_only=True,
                    )
                service = self.frozen_services[key]
            self.sessions[sid] = self._conversation(service, service.get_session(sid))
            # 读取历史不会自动恢复业务执行。
        conversation = self.sessions[sid]
        conversation.service.get_session(sid)  # 历史清理可能已经撤销此会话。
        return conversation

    async def create(self, scenario_id, customer_id, channel="text", run_id=None):
        self.profile_worker.start()
        service = self.services[scenario_id]
        ctx = service.create_session(customer_id, channel=channel, run_id=run_id)
        self.store.create(ctx.session_id, service.scenario, channel)
        conversation = self._conversation(service, ctx)
        self.sessions[ctx.session_id] = conversation
        conversation.save_metadata(model_config_hash=self.settings.model_config_hash)

        async def expire():
            await asyncio.sleep(max(0, conversation.remaining_seconds))
            await conversation.stop("session_timeout")

        conversation.expiry_task = asyncio.create_task(expire(), name=f"expiry-{conversation.id}")
        conversation.emit(
            "created",
            {
                "scenario_id": scenario_id,
                "channel": channel,
                "snapshot_hash": ctx.snapshot_hash,
                "model_config_hash": self.settings.model_config_hash,
            },
        )
        return conversation

    async def close(self):
        await asyncio.gather(
            *(session.stop("shutdown") for session in self.sessions.values()),
            return_exceptions=True,
        )
        await self.profile_worker.close()
        for service in {*self.services.values(), *self.frozen_services.values()}:
            service.close()
