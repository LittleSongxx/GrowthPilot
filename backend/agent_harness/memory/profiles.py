"""管理通话前画像和通话后的更新任务；所有写操作由宿主事务包裹。"""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..runtime.snapshots import canonical, digest
from .persistence import scope
from .validation import validate_value


class Evidence(BaseModel):
    """提取值必须引用本次通话的客户原话。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    message_id: str = Field(min_length=1)
    quote: str = Field(min_length=1, max_length=4000)


class ProfileUpdate(BaseModel):
    """模型只提出字段变化，不决定客户标识、版本或更新时间。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    field: str
    value: str | int | float | bool | None
    status: Literal["known", "unknown", "declined"] = "known"
    evidence: Evidence


class ProfilePatch(BaseModel):
    """未提到的字段不输出，避免清空旧画像。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    updates: list[ProfileUpdate] = Field(max_length=100)


class ProfileStore:
    def __init__(self, memory):
        self.memory = memory
        self.scenario, self.clock = memory.scenario, memory.clock

    @property
    def db(self):
        return self.memory.db

    def _connection(self):
        return self.memory._connection()

    def begin(self, context):
        """冻结本次通话使用的有效画像，不向对话模型暴露存储元数据。"""
        fields = self.memory.read(context)["data"]["fields"]
        profile = {
            item["field"]: {"value": item["value"], "status": item["status"]}
            for item in fields
            if item["status"] != "expired"
        }
        self.db.execute(
            "INSERT INTO profile_jobs(session_id,status,baseline_json) VALUES(%s,%s,%s)",
            (context.id, "collecting", canonical(profile)),
        )

    def baseline(self, context):
        with self._connection():
            row = self.db.execute(
                "SELECT baseline_json FROM profile_jobs WHERE session_id=%s", (context.id,)
            ).fetchone()
            return json.loads(row["baseline_json"]) if row else {}

    def enqueue(self, session_id):
        """结束通话只提交本地任务；没有客户发言时无需调用提取模型。"""
        has_customer = self.db.execute(
            "SELECT 1 FROM messages WHERE session=%s AND final=1 "
            "AND origin IN ('customer','customer_transcript') AND trim(text)<>'' LIMIT 1",
            (session_id,),
        ).fetchone()
        self.db.execute(
            "UPDATE profile_jobs SET status=%s WHERE session_id=%s AND status='collecting'",
            ("pending" if has_customer else "skipped", session_id),
        )

    def state(self, context):
        with self._connection():
            row = self.db.execute(
                "SELECT status,attempts,error FROM profile_jobs WHERE session_id=%s", (context.id,)
            ).fetchone()
            return dict(row) if row else {"status": "unavailable", "attempts": 0, "error": None}

    def payload(self, context):
        with self._connection():
            messages = self.db.execute(
                "SELECT id,origin,text FROM messages WHERE workspace=%s AND customer=%s "
                "AND scenario=%s AND session=%s AND final=1 "
                "AND origin IN ('customer','customer_transcript','agent') ORDER BY seq",
                (*scope(context), context.id),
            ).fetchall()
            return {
                "fields": list(self.scenario.fields.values()),
                "previous_profile": self.baseline(context),
                "messages": [dict(row) for row in messages],
            }

    def apply(self, context, candidate):
        """先校验整批结果，再原子写入；较早客户消息不能覆盖较晚的画像证据。"""
        self.db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (canonical([context.workspace_id, context.customer_id]),),
        )
        patch = ProfilePatch.model_validate(candidate)
        seen, prepared = set(), []
        for update in patch.updates:
            if update.field in seen or update.field not in self.scenario.fields:
                raise ValueError("画像字段未知或重复")
            seen.add(update.field)
            definition = self.scenario.fields[update.field]
            if update.status == "known":
                validate_value(definition, update.value)
            elif update.value is not None:
                raise ValueError("未知或拒答字段必须为 null")
            message = self.db.execute(
                "SELECT seq AS sequence,* FROM messages WHERE id=%s AND workspace=%s "
                "AND customer=%s AND scenario=%s AND session=%s",
                (update.evidence.message_id, *scope(context), context.id),
            ).fetchone()
            if (
                not message
                or not message["final"]
                or message["origin"] not in {"customer", "customer_transcript"}
                or update.evidence.quote not in message["text"]
            ):
                raise ValueError("画像证据必须是本次通话的最终客户原话")
            prepared.append((update, definition, message))

        applied, skipped = [], []
        for update, definition, message in prepared:
            previous = self.db.execute(
                "SELECT m.*,source.seq AS source_sequence FROM memory m "
                "LEFT JOIN messages source ON source.id=m.message_id "
                "WHERE m.workspace=%s AND m.customer=%s AND m.scenario=%s AND m.field=%s "
                "ORDER BY m.version DESC LIMIT 1",
                (*scope(context), update.field),
            ).fetchone()
            # 比较客户发言的先后，不能使用后台任务完成时间决定谁覆盖谁。
            if previous and (
                previous["source_sequence"] is None
                or previous["source_sequence"] >= message["sequence"]
            ):
                skipped.append(update.field)
                continue
            operation_id = f"profile:{context.id}:{update.field}"
            self.memory._insert(
                context,
                definition,
                update.value,
                update.status,
                message,
                update.evidence.quote,
                previous,
                operation_id,
                digest(update.model_dump()),
                expires_at=message["created"] + definition.get("ttl_seconds", 2592000),
            )
            applied.append(update.field)
        return {"applied": applied, "skipped_stale": skipped}
