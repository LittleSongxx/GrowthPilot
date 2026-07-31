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


