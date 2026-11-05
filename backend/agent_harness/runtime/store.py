"""存储框架检查点与观测记录；会话生命周期以 sessions 表为准。"""

from __future__ import annotations

import json
import time

from backend.database import DatabaseConfig, connect, initialize_database


class RuntimeStore:
    def __init__(self, path: DatabaseConfig):
        self.path = path
        initialize_database(path)

    def connect(self):
        return connect(self.path)

    def create(self, sid, scenario, channel):
        with self.connect() as db:
            db.execute(
                "INSERT INTO runtime_sessions(session_id,scenario_id,snapshot_hash,channel,status,started_at) VALUES(%s,%s,%s,%s,%s,%s)",
                (sid, scenario.id, scenario.snapshot_hash, channel, "active", time.time()),
            )

    def get(self, sid):
        with self.connect() as db:
            row = db.execute(
                "SELECT r.session_id,s.scenario AS scenario_id,s.snapshot AS snapshot_hash,s.channel,"
                "s.workspace AS workspace_id,s.state AS status,s.started AS started_at,r.opening_sent,r.state_json,r.metadata_json "
                "FROM runtime_sessions r JOIN sessions s ON s.id=r.session_id WHERE r.session_id=%s",
                (sid,),
            ).fetchone()
        if row is None:
            raise KeyError("session_not_found")
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        result["state"] = json.loads(result.pop("state_json") or "null")
        return result

    def save(self, sid, **values):
        with self.connect() as db:
            allowed = {
                "scenario_id",
                "snapshot_hash",
                "channel",
                "status",
                "started_at",
                "opening_sent",
                "state_json",
                "metadata_json",
            }
            if not values or not values.keys() <= allowed:
                raise ValueError("Invalid runtime session fields")
            keys = list(values)
            db.execute(
                "UPDATE runtime_sessions SET "
                + ",".join(f"{k}=%s" for k in keys)
                + " WHERE session_id=%s",
                [values[k] for k in keys] + [sid],
            )

    def emit(self, sid, kind, data):
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO runtime_events(session_id,type,at,data_json) VALUES(%s,%s,%s,%s) RETURNING seq",
                (sid, kind, time.time(), json.dumps(data, ensure_ascii=False, default=str)),
            )
            return cursor.fetchone()["seq"]

    def events_page(self, sid, after=0, limit=300):
        """返回一页事件和下一页游标；调用方必须按 has_more 继续拉取。"""
        with self.connect() as db:
            # 多取一条用于判断是否还有下一页，返回时截断。
            rows = db.execute(
                "SELECT * FROM runtime_events WHERE session_id=%s AND seq>%s ORDER BY seq LIMIT %s",
                (sid, after, limit + 1),
            ).fetchall()
        page = [
            {
                "seq": row["seq"],
                "type": row["type"],
                "at": row["at"],
                "data": json.loads(row["data_json"]),
            }
            for row in rows[:limit]
        ]
        return {
            "events": page,
            "last_seq": page[-1]["seq"] if page else after,
            "has_more": len(rows) > limit,
        }

    def events(self, sid, after=0, limit=300):
        return self.events_page(sid, after, limit)["events"]

    def reply_annotations(self, sid, fallback_channel):
        """将生成结果整理为回复标注，不推断声学表现或是否完整播放。"""
        with self.connect() as db:
            rows = db.execute(
                "SELECT type,data_json FROM runtime_events WHERE session_id=%s AND "
                "(type='reply_outcome' OR (type='framework' AND data_json::jsonb->>'type'='REPLY_END')) ORDER BY seq",
                (sid,),
            ).fetchall()
        annotations = {}
        for row in rows:
            data = json.loads(row["data_json"])
            mid = data.get("message_id", f"agent:{sid}:{data.get('reply_id', '')}")
            channel = data.get(
                "channel", annotations.get(mid, {}).get("source_channel", fallback_channel)
            )
            reason = data.get("finished_reason")
            annotations[mid] = {
                "source_channel": channel,
                "reply_id": data.get("reply_id"),
                "finished_reason": reason,
                "interrupted": reason == "interrupted" if reason else None,
                "text_evidence": "generated_text" if channel == "voice" else "text_reply",
                "playback_completed": None,
            }
        return annotations
