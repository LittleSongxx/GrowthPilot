"""观察单次会话的框架原生事件，不改变执行过程。"""

from __future__ import annotations

import hashlib
import json
import time


class ConversationEvents:
    """观察生成的回复和工具调用，不调度模型或工具的执行。"""

    def __init__(self, conversation):
        self.conversation = conversation
        self.text = ""
        self.first_text_at = None
        self.last_message_id = None
        self.tool_calls = {}

    @property
    def last_assistant_text(self):
        return self.text

    def record_reply(self, text, reply_id, finished_reason="completed"):
        self.text = text
        self.last_message_id = f"agent:{self.conversation.id}:{reply_id}"
        self.conversation.service.add_message(
            self.conversation.ctx, text, origin="agent", message_id=self.last_message_id
        )
        self.conversation.emit(
            "reply_outcome",
            {
                "message_id": self.last_message_id,
                "reply_id": reply_id,
                "channel": self.conversation.channel,
                "finished_reason": finished_reason,
            },
        )

    def observe_tool_event(self, event):
        """从框架公开事件中提取审计记录，不修改工具输出。"""
        kind, call_id = event.get("type", ""), event.get("tool_call_id")
        if not kind.startswith(("TOOL_CALL_", "TOOL_RESULT_")) or not call_id:
            return
        call = self.tool_calls.setdefault(
            call_id, {"name": "", "arguments_text": "", "result_text": ""}
        )
        if event.get("tool_call_name"):
            call["name"] = event["tool_call_name"]
        if kind == "TOOL_CALL_DELTA":
            call["arguments_text"] += event.get("delta", "")
        elif kind == "TOOL_CALL_END":
            try:
                call["arguments"] = json.loads(call["arguments_text"] or "{}")
            except json.JSONDecodeError:
                call["arguments"] = {"unparsed": call["arguments_text"]}
            self.conversation.emit(
                "tool_start",
                {"name": call["name"], "arguments": call["arguments"], "tool_call_id": call_id},
            )
        elif kind == "TOOL_RESULT_TEXT_DELTA":
            call["result_text"] += event.get("delta", "")
        elif kind == "TOOL_RESULT_END":
            try:
                result = json.loads(call["result_text"])
            except json.JSONDecodeError:
                result = {"text": call["result_text"]}
            self.conversation.emit(
                "tool_result",
                {
                    "name": call["name"],
                    "result": result,
                    "tool_call_id": call_id,
                    "state": event.get("state"),
                },
            )
            if call["name"] == "Skill" and event.get("state") == "success":
                selected = next(
                    (
                        item
                        for item in self.conversation.service.scenario.data["skills"]
                        if item["id"] == call.get("arguments", {}).get("skill")
                    ),
                    None,
                )
                if selected:
                    self.conversation.emit(
                        "skill_loaded",
                        {
                            "skill_id": selected["id"],
                            "version": selected["version"],
                            "ref": selected["ref"],
                            "tool_call_id": call_id,
                            "body_sha256": hashlib.sha256(call["result_text"].encode()).hexdigest(),
                        },
                    )
            self.conversation.emit(
                "tool_end",
                {"name": call["name"], "tool_call_id": call_id, "state": event.get("state")},
            )
            self.tool_calls.pop(call_id, None)

    async def process(self, event):
        data = event.model_dump(mode="json") if hasattr(event, "model_dump") else dict(event)
        kind = data.get("type")
        rid = data.get("reply_id", "")
        if kind in {"MODEL_CALL_START", "MODEL_CALL_END"} and self.conversation.end_after_playback:
            # 框架本地收尾仍产生模型生命周期事件，但没有供应商请求和费用。
            data.update(source="end_call", model_name=None)
        self.observe_tool_event(data)
        if kind in {"REPLY_START", "MODEL_CALL_START"}:
            # 只播报模型的最终回答；工具调用过程中的说明保留在追踪记录中。
            self.text = ""
            self.first_text_at = None
            self.last_message_id = None
        elif kind == "TEXT_BLOCK_DELTA":
            delta = data.get("delta", "")
            # 流式块可能包含多个 token；只记录最终回复首次出现可见文字的单调时钟。
            if self.first_text_at is None and delta.strip():
                self.first_text_at = time.monotonic()
            self.text += delta
        elif kind == "REPLY_END":
            if self.text:
                self.record_reply(self.text, rid, data.get("finished_reason"))
            self.conversation.checkpoint()
        # 音频的 Base64 数据既不持久化，也不返回浏览器调试通道。
        if kind not in {
            "DATA_BLOCK_DELTA",
            "THINKING_BLOCK_DELTA",
            "TOOL_CALL_DELTA",
            "TOOL_RESULT_TEXT_DELTA",
            "TOOL_RESULT_DATA_DELTA",
        }:
            self.conversation.emit("framework", data)
        if kind == "MODEL_CALL_END" and data.get("source") != "end_call":
            self.conversation.emit(
                "usage",
                {
                    key: data.get(key)
                    for key in (
                        "model_name",
                        "reply_id",
                        "input_tokens",
                        "output_tokens",
                        "cache_input_tokens",
                        "cache_creation_input_tokens",
                        "finished_reason",
                        "usage",
                        "duration",
                    )
                },
            )
