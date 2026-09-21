"""在宿主当前事务连接上管理客户记忆的记录、证据、修正和过期处理。

应用服务负责管理连接作用域、事务并校验绑定的会话。存储模块使用
当前连接、场景定义的字段和时钟完成业务读写。
"""

from __future__ import annotations

import json
import uuid

from ..runtime.snapshots import canonical, digest
from .validation import validate_value, value_in_quote


def result(status="success", data=None, evidence=None, retryable=False):
    return {
        "status": status,
        "data": {} if data is None else data,
        "evidence": evidence or [],
        "retryable": retryable,
    }


def scope(context):
    return context.workspace_id, context.customer_id, context.scenario_id


class MemoryStore:
    def __init__(self, service, scenario, clock):
        self.service, self.scenario, self.clock = service, scenario, clock

    @property
    def db(self):
        return self.service.db

    def _connection(self):
        return self.service._connection()

    def _row(self, row):
        return {
            "id": row["id"],
            "field": row["field"],
            "semantic_version": row["semantic_version"],
            "version": row["version"],
            "value": json.loads(row["value"]),
            "status": row["status"] if row["expires"] > self.clock() else "expired",
            "source_kind": row["source_kind"],
            "source_verified": True,
            "message_id": row["message_id"],
            "session_id": row["session"],
            "quote": row["quote"],
            "quote_start": row["quote_start"],
            "recorded_at": row["recorded"],
            "expires_at": row["expires"],
            "supersedes": row["supersedes"],
            "operation_id": row["operation_id"],
        }

    def _allowed_fields(self, tool):
        mapping = next((t for t in self.scenario["tools"] if t["id"] == tool), {})
        return mapping.get("fields", list(self.scenario.fields))

    def read(self, context, field=None, include_history=False):
        with self._connection():
            if field is not None and field not in self.scenario.fields:
                return result("denied", {"reason": "Unknown field"})
            allowed = self._allowed_fields("read_memory")
            if field is not None and field not in allowed:
                return result(
                    "denied", {"reason": "Field is outside the configured read permission"}
                )
            if not allowed:
                return result("no_data", {"fields": []})
            marks = ",".join("%s" for _ in allowed)
            query = f"SELECT * FROM memory WHERE workspace=%s AND customer=%s AND scenario=%s AND field IN ({marks})"
            args = [*scope(context), *allowed]
            if field:
                query += " AND field=%s"
                args.append(field)
            rows = self.db.execute(query + " ORDER BY version DESC", args).fetchall()
            out, seen = [], set()
            for row in rows:
                definition = self.scenario.fields[row["field"]]
                if row["semantic_version"] != definition.get("semantic_version", "1"):
                    continue
                if not include_history and row["field"] in seen:
                    continue
                seen.add(row["field"])
                out.append(self._row(row))
            return result("success" if out else "no_data", {"fields": out}, [x["id"] for x in out])

    def _validate_candidate(self, field, value, status, expected_version):
        if field not in self.scenario.fields or status not in (
            "known",
            "unknown",
            "declined",
            "not_asked",
        ):
            raise ValueError("Invalid field/status/source")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("Invalid expected_version")
        if field not in self._allowed_fields("write_memory"):
            raise ValueError("Field is outside the configured write permission")
        definition = self.scenario.fields[field]
        if status == "known":
            validate_value(definition, value)
        elif value is not None:
            raise ValueError("Unknown/declined/not_asked must use null value")
        return definition

    def _source_message(self, context, message_id, quote, definition, value, status):
        message = self.db.execute(
            "SELECT * FROM messages WHERE id=%s AND workspace=%s AND customer=%s AND scenario=%s AND session=%s",
            (message_id, *scope(context), context.id),
        ).fetchone()
        if (
            not message
            or not message["final"]
            or message["origin"] not in ("customer", "customer_transcript")
            or not quote
            or quote not in message["text"]
        ):
            raise ValueError(
                "Source must be a current visible final customer message with an exact quote"
            )
        if status == "known" and not value_in_quote(definition, value, quote):
            raise ValueError(
                "Boolean memory was not saved: the quote did not meet persistent evidence validation; current dialogue statements remain available"
                if definition["type"] == "boolean"
                else "Candidate value is not explicit in customer quote; clarify before writing verified memory"
            )
        return message

    def write(
        self,
        context,
        field,
        value,
        message_id,
        quote,
        expected_version=0,
        status="known",
        operation_id=None,
    ):
        try:
            definition = self._validate_candidate(field, value, status, expected_version)
        except ValueError as exc:
            return result("denied", {"reason": str(exc)})
        operation_id = operation_id or "memory_op_" + uuid.uuid4().hex
        request_hash = digest([field, value, message_id, quote, expected_version, status])
        existing = self.db.execute(
            "SELECT * FROM memory WHERE workspace=%s AND customer=%s AND scenario=%s AND operation_id=%s",
            (*scope(context), operation_id),
        ).fetchone()
        if existing:
            return result(
                "success" if existing["request_hash"] == request_hash else "conflict",
                self._row(existing),
            )
        try:
            message = self._source_message(context, message_id, quote, definition, value, status)
        except ValueError as exc:
            return result("denied", {"reason": str(exc)})
        previous = self.db.execute(
            "SELECT * FROM memory WHERE workspace=%s AND customer=%s AND scenario=%s AND field=%s ORDER BY version DESC LIMIT 1",
            (*scope(context), field),
        ).fetchone()
        actual = previous["version"] if previous else 0
        if actual != expected_version:
            return result(
                "conflict", {"reason": "Version conflict", "actual_version": actual}, retryable=True
            )
        self._insert(
            context, definition, value, status, message, quote, previous, operation_id, request_hash
        )
        return self.read(context, field)

    def _insert(
        self,
        context,
        definition,
        value,
        status,
        message,
        quote,
        previous,
        operation_id,
        request_hash,
        expires_at=None,
    ):
        memory_id = "memory_" + uuid.uuid4().hex
        now = self.clock()
        version = previous["version"] + 1 if previous else 1
        source_kind = (
            "customer_transcript"
            if message["origin"] == "customer_transcript"
            else "customer_message"
        )
        self.db.execute(
            "INSERT INTO memory(id,workspace,customer,scenario,field,semantic_version,version,value,status,source_kind,message_id,session,quote,quote_start,recorded,expires,supersedes,operation_id,request_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                memory_id,
                *scope(context),
                definition["id"],
                definition.get("semantic_version", "1"),
                version,
                canonical(value),
                status,
                source_kind,
                message["id"],
                context.id,
                quote,
                message["text"].index(quote),
                now,
                expires_at
                if expires_at is not None
                else now + definition.get("ttl_seconds", 2592000),
                previous["id"] if previous else None,
                operation_id,
                request_hash,
            ),
        )
        payload = {
            "memory_id": memory_id,
            "field": definition["id"],
            "version": version,
            "message_id": message["id"],
        }
        self.db.execute(
            "INSERT INTO audit(workspace,customer,scenario,session,kind,payload,created) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (*scope(context), context.id, "memory_written", canonical(payload), now),
        )
