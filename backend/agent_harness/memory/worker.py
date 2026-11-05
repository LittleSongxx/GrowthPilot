"""消费 PostgreSQL 中的画像任务；租约支持崩溃恢复，失败最多自动尝试三次。"""

import asyncio
import json
import logging
import time
from contextlib import contextmanager

from psycopg import Error as DatabaseError

from backend.database import connect

from .extraction import extract_profile

logger = logging.getLogger(__name__)


class ProfileWorker:
    def __init__(self, settings, extractor=None, *, session_ids=None):
        self.settings = settings
        self.extractor = extractor or extract_profile
        # 生产宿主默认消费全部任务；评测可限定本会话，防止并发案例相互取消画像提取。
        self.session_ids = None if session_ids is None else set(session_ids)
        self.task = None

    @contextmanager
    def connection(self):
        with connect(self.settings.app_db) as db, db.transaction():
            yield db

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name="customer-profile-worker")

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def run(self):
        while True:
            # 每次循环之间让出执行权；轮询同时发现其他入口提交的持久任务。
            try:
                worked = await self.run_once()
            except (DatabaseError, OSError):
                worked = False
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("画像任务循环异常，稍后继续")
                worked = False
            await asyncio.sleep(0 if worked else 1)

    def claim(self):
        session_ids = None if self.session_ids is None else tuple(sorted(self.session_ids))
        if session_ids == ():
            return None
        scope = ""
        if session_ids is not None:
            scope = " AND session_id IN (" + ",".join("%s" for _ in session_ids) + ")"
        scope_args = session_ids or ()
        now = time.time()
        with self.connection() as db:
            # 最后一次调用也可能在宿主崩溃时中断，必须给出明确终态。
            db.execute(
                "WITH expired AS (SELECT session_id FROM profile_jobs "
                "WHERE status='running' AND lease_until<=%s AND attempts>=3"
                + scope
                + " FOR UPDATE SKIP LOCKED) "
                "UPDATE profile_jobs p SET status='failed',error='worker_interrupted' "
                "FROM expired WHERE p.session_id=expired.session_id",
                (now, *scope_args),
            )
            row = db.execute(
                "SELECT p.*,s.workspace,s.snapshot FROM profile_jobs p "
                "JOIN sessions s ON s.id=p.session_id WHERE s.state='closed' "
                "AND p.attempts<3 AND ((p.status IN ('pending','retrying') AND p.next_attempt<=%s) "
                "OR (p.status='running' AND p.lease_until<=%s))"
                + scope
                + " ORDER BY s.started,s.id LIMIT 1 FOR UPDATE OF p SKIP LOCKED",
                (now, now, *scope_args),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE profile_jobs SET status='running',attempts=attempts+1,lease_until=%s "
                "WHERE session_id=%s",
                (now + 120, row["session_id"]),
            )
            return dict(row) | {"attempts": row["attempts"] + 1}

    def fail(self, job, error, *, cancelled=False):
        attempts = job["attempts"] - int(cancelled)
        status = "pending" if cancelled else "failed" if attempts >= 3 else "retrying"
        with self.connection() as db:
            db.execute(
                "UPDATE profile_jobs SET status=%s,attempts=%s,error=%s,lease_until=0,next_attempt=%s "
                "WHERE session_id=%s AND status='running' AND attempts=%s",
                (
                    status,
                    attempts,
                    error,
                    0 if cancelled else time.time() + 5 * attempts,
                    job["session_id"],
                    job["attempts"],
                ),
            )

    async def run_once(self):
        """执行一个已就绪任务；测试和离线验证可注入提取器，无需调用网络。"""
        from ..runtime.service import BackendService
        from ..runtime.store import RuntimeStore

        job = self.claim()
        if job is None:
            return False
        service = None
        try:
            scenario = BackendService.saved_scenario(self.settings.app_db, job["snapshot"])
            service = BackendService(self.settings.app_db, scenario, workspace_id=job["workspace"])
            ctx = service.get_session(job["session_id"])
            async with asyncio.timeout(60):
                payload = service.profiles.payload(ctx)
                if self.extractor is extract_profile:
                    diagnostics = {}
                    try:
                        candidate = await self.extractor(
                            self.settings, payload, diagnostics=diagnostics
                        )
                    finally:
                        # 每次实际请求单独记账；失败或重试仍保留用量，不计入实时对话的 usage。
                        if diagnostics.get("request_started"):
                            try:
                                RuntimeStore(self.settings.app_db).emit(
                                    ctx.id,
                                    "profile_usage",
                                    {
                                        "component": "profile_extraction",
                                        "attempt": job["attempts"],
                                        **diagnostics,
                                    },
                                )
                            except (DatabaseError, OSError) as exc:
                                # 观测写入失败不能触发额外模型重试，也不输出数据库异常正文。
                                logger.warning("画像提取用量记录失败：%s", type(exc).__name__)
                else:
                    # 保持已有二参数注入提取器的约定，不推断其是否调用模型或消耗 token。
                    candidate = await self.extractor(self.settings, payload)
            with service._tx(ctx):
                service._check(ctx)
                current = service.db.execute(
                    "SELECT status,attempts FROM profile_jobs WHERE session_id=%s FOR UPDATE",
                    (ctx.id,),
                ).fetchone()
                if (
                    current is None
                    or current["status"] != "running"
                    or current["attempts"] != job["attempts"]
                ):
                    return True
                result = service.profiles.apply(ctx, candidate)
                service.db.execute(
                    "UPDATE profile_jobs SET status='completed',error=NULL,lease_until=0,result_json=%s "
                    "WHERE session_id=%s",
                    (json.dumps(result, ensure_ascii=False), ctx.id),
                )
                service._audit(ctx, "profile_updated", result)
        except asyncio.CancelledError:
            self.fail(job, "worker_interrupted", cancelled=True)
            raise
        except Exception as exc:  # noqa: BLE001 - 单个后台任务失败不能中止其余画像任务
            # 仅记录错误类别，不把供应商响应或客户内容泄露到公开状态。
            self.fail(job, type(exc).__name__)
        finally:
            if service:
                service.close()
        return True
