"""仅在本地运行的会话应用；服务供应商凭证保留在服务端。"""

from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.agent_harness.agent import FRAMEWORK_COMMIT
from backend.agent_harness.runtime.session_manager import SessionManager


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionInput(StrictBody):
    scenario_id: str
    customer_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    run_id: str | None = None


class StopInput(StrictBody):
    reason: str = Field(default="user_stop", max_length=100)
    generation: int | None = Field(default=None, ge=0)


class InterruptInput(StrictBody):
    message_id: str | None = Field(default=None, max_length=200)


class VoiceInterrupted(Exception):
    """语音任务被取消；由调用入口转换为 HTTP 状态或结束音频流。"""


class TelemetryInput(StrictBody):
    event: str = Field(min_length=1, max_length=80)
    client_ms: float = Field(ge=0)
    reply_id: str | None = None
    data: dict = Field(default_factory=dict)


def create_app(settings=None, sessions=None):
    from backend.paths import install_path_safe_logging, resolve_project_path
    from backend.settings import Settings

    install_path_safe_logging()
    settings = settings or Settings.from_env()
    if sessions is None:
        from backend.agent_harness import BackendService, load_scenario
        from backend.database import initialize_database

        initialize_database(settings.app_db)
        services = {}
        paths = sorted((settings.project_root / "backend/scenarios").glob("*/scenario.yaml"))
        paths += sorted((settings.project_root / "backend/scenarios").glob("*.yaml"))
        selected = os.environ.get("GROWTH_SCENARIO")
        if selected:
            selected_path = resolve_project_path(selected, settings.project_root)
            if selected_path.is_file() and selected_path not in paths:
                paths.append(selected_path)
        for path in paths:
            scenario = load_scenario(path)
            services[scenario.id] = BackendService(settings.app_db, scenario)
            services[scenario.id].recover_host_sessions()
        sessions = SessionManager(settings, services)

    @asynccontextmanager
    async def lifespan(app):
        sessions.profile_worker.start()
        yield
        await sessions.close()

    app = FastAPI(title="通用用增 Agent", lifespan=lifespan)
    app.state.sessions = sessions
    from backend.channels.speech import (
        AUDIO_TYPES,
        MAX_AUDIO_BYTES,
        PCM_SAMPLE_RATE,
        SpeechAPI,
        SpeechUnavailable,
    )

    app.state.speech = SpeechAPI(settings)

    @app.middleware("http")
    async def local_only(request, call_next):
        hostname = request.url.hostname
        origin = request.headers.get("origin")
        if hostname not in {"localhost", "127.0.0.1", "testserver"}:
            return JSONResponse({"detail": "local_host_required"}, status_code=403)
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in {
                "localhost",
                "127.0.0.1",
                "testserver",
            } or parsed.port not in {None, settings.app_port}:
                return JSONResponse({"detail": "same_origin_required"}, status_code=403)
        if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            return JSONResponse({"detail": "loopback_required"}, status_code=403)
        return await call_next(request)

    def runtime(request, sid):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not token or not hmac.compare_digest(token, sessions.token(sid)):
            raise HTTPException(401, "session_token_required")
        try:
            return sessions.get(sid)
        except KeyError:
            raise HTTPException(404, "session_not_found")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    async def public_state(rt):
        state = rt.service.snapshot(rt.ctx)
        state["field_definitions"] = rt.service.scenario.data.get("customer_fields", [])
        state["profile_update"] = rt.service.profiles.state(rt.ctx)
        annotations = sessions.store.reply_annotations(rt.id, rt.channel)
        state["messages"] = [
            message | annotations.get(message.get("id"), {})
            if message.get("origin") == "agent"
            else message
            for message in state.get("messages", [])
        ]
        return state

    @app.get("/health")
    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "framework_commit": FRAMEWORK_COMMIT,
            "agent_available": bool(settings.agent_api_key),
            "voice_available": bool(settings.agent_api_key and settings.dashscope_api_key),
            "speech_pipeline": "asr-agent-tts",
            "business_data_mode": "local_catalog",
            "policy_source": "rag",
        }

    @app.get("/api/scenarios")
    async def scenarios():
        selected = os.environ.get(
            "GROWTH_SCENARIO",
            getattr(settings, "default_scenario_id", next(iter(sessions.services), "")),
        )
        default = next(
            (
                key
                for key in sessions.services
                if key == selected or key == Path(selected).parent.name
            ),
            next(iter(sessions.services), ""),
        )
        return {
            "default_scenario": default,
            "scenarios": [
                {
                    **service.scenario.data["scenario"],
                    "snapshot_hash": service.scenario.snapshot_hash,
                }
                for service in sessions.services.values()
            ],
        }

    @app.post("/api/sessions")
    async def create(body: SessionInput):
        if body.scenario_id not in sessions.services:
            raise HTTPException(404, "scenario_not_found")
        try:
            rt = await sessions.create(**body.model_dump(), channel="voice")
        except PermissionError as exc:
            reason = str(exc)
            if "active executor" in reason:
                detail = "该客户已有活动会话，请先停止原会话。"
            else:
                detail = "暂时无法创建会话，请稍后重试。"
            raise HTTPException(409, detail) from None
        return {
            "session_id": rt.id,
            "generation": rt.voice_generation,
            "token": sessions.token(rt.id),
            "state": await public_state(rt),
        }

    @app.get("/api/sessions/{sid}/state")
    async def state(sid: str, request: Request):
        return await public_state(runtime(request, sid))

    @app.get("/api/sessions/{sid}/events")
    async def events(sid: str, request: Request, after: int = 0):
        runtime(request, sid)
        values = sessions.store.events(sid, max(0, after))
        return {
            "events": [value for value in values if value["type"] != "business_query_audit"],
            "last_seq": values[-1]["seq"] if values else after,
        }

    def check_generation(rt, generation):
        if generation is not None and generation != rt.voice_generation:
            raise HTTPException(409, "voice_generation_stale")

    @asynccontextmanager
    async def voice_turn(rt, generation):
        """从上传、语音识别到 Agent 处理，始终为同一次输入占用处理通道；停止时取消同一任务。"""
        check_generation(rt, generation)
        current_generation = rt.voice_generation
        try:
            rt.check_available()
        except RuntimeError as exc:
            if str(exc) == "session_expired":
                await rt.stop("session_timeout")
            raise HTTPException(409, str(exc)) from None
        rt.task = asyncio.current_task()
        try:
            async with asyncio.timeout(max(0.1, rt.remaining_seconds)):
                yield
                check_generation(rt, current_generation)
        except asyncio.CancelledError:
            raise HTTPException(409, "本轮已停止。") from None
        except TimeoutError:
            await rt.stop("session_timeout")
            raise HTTPException(408, "会话已超时，请开始新通话。") from None
        except SpeechUnavailable as exc:
            rt.emit("speech_error", {"category": type(exc).__name__})
            raise HTTPException(502, str(exc)) from None
        finally:
            if rt.task is asyncio.current_task():
                rt.task = None

    @app.post("/api/sessions/{sid}/voice/start")
    async def start_voice(sid: str, request: Request, generation: int | None = None):
        rt = runtime(request, sid)
        async with voice_turn(rt, generation):
            if sessions.store.get(sid)["opening_sent"]:
                return {"reply_message_id": None, "end_after_playback": False}
            await rt.message(
                "[HOST_CONTROL: 主动开场，非客户话语/非同意/不可作为画像证据] "
                "请简短说明身份与本次联系背景，询问现在是否方便。",
                origin="host_control",
            )
            sessions.store.save(sid, opening_sent=1)
            return {
                "reply_message_id": rt.events.last_message_id,
                "end_after_playback": bool(rt.events.last_message_id and rt.end_after_playback),
            }

    @app.post("/api/sessions/{sid}/voice/turn")
    async def voice(
        sid: str,
        request: Request,
        generation: int | None = None,
        input_only: bool = False,
    ):
        rt = runtime(request, sid)
        async with voice_turn(rt, generation):
            input_generation = rt.voice_generation
            rt.end_call_message = None
            mime = request.headers.get("content-type", "").split(";", 1)[0].lower()
            if mime not in AUDIO_TYPES:
                raise HTTPException(415, "请使用支持 WebM/Opus 录音的浏览器。")
            audio = bytearray()
            async for chunk in request.stream():
                audio.extend(chunk)
                if len(audio) > MAX_AUDIO_BYTES:
                    raise HTTPException(413, "单次录音过大，请缩短发言。")
            if not audio:
                raise HTTPException(400, "未收到录音。")
            rt.input_task = asyncio.current_task()
            try:
                text, usage = await app.state.speech.transcribe(bytes(audio), mime)
                rt.emit(
                    "speech_usage", {"stage": "asr", "model": settings.asr_model, "usage": usage}
                )
                if not text:
                    raise HTTPException(422, "未识别到语音，请重新说一句。")
                if input_only or input_generation != rt.voice_generation:
                    rt.retain_transcript(text)
                    return {
                        "transcript": text,
                        "reply_message_id": None,
                        "end_after_playback": False,
                    }
            finally:
                if rt.input_task is asyncio.current_task():
                    rt.input_task = None
            await rt.message(text, origin="customer_transcript")
            return {
                "transcript": text,
                "reply_message_id": rt.events.last_message_id,
                "end_after_playback": bool(rt.events.last_message_id and rt.end_after_playback),
            }

    @app.post("/api/sessions/{sid}/voice/interrupt")
    async def interrupt_voice(sid: str, request: Request, body: InterruptInput):
        rt = runtime(request, sid)
        if body.message_id:
            message = rt.service.message(rt.ctx, body.message_id)
            if message is None or message["origin"] != "agent":
                raise HTTPException(404, "reply_not_found")
        try:
            generation = await rt.interrupt(body.message_id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"generation": generation}

    async def watch_disconnect(request, task):
        """首个音频片段返回前也响应断开；返回流之后仍由同一任务负责合成。"""
        while not task.done():
            # 音频 GET 没有请求正文，可持续等断开事件，避免中间件下轮询检查漏掉断开。
            message = await request.receive()
            if message["type"] == "http.disconnect":
                if not task.cancelling():
                    task.cancel()
                return

    def speech_task(rt, request, work):
        task = asyncio.create_task(work)
        rt.speech_tasks.add(task)
        task.add_done_callback(rt.speech_tasks.discard)
        watcher = asyncio.create_task(watch_disconnect(request, task))
        return task, watcher

    async def close_speech_task(task, watcher):
        # StreamingResponse 的断开取消域不能再次打断供应商连接的异步关闭。
        with anyio.CancelScope(shield=True):
            if not task.done() and not task.cancelling():
                task.cancel()
            watcher.cancel()
            await asyncio.gather(task, watcher, return_exceptions=True)

    @app.get("/api/sessions/{sid}/voice/audio/{message_id}")
    async def speak(
        sid: str,
        message_id: str,
        request: Request,
        stream: bool = False,
        generation: int | None = None,
    ):
        rt = runtime(request, sid)
        check_generation(rt, generation)
        current_generation = rt.voice_generation
        if (stream or generation is not None) and (
            rt.service.session(rt.ctx)["state"] != "active" or rt.closing
        ):
            raise HTTPException(409, "session_not_active")
        if rt.interrupting:
            raise HTTPException(409, "session_busy")
        message = rt.service.message(rt.ctx, message_id)
        if message is None or message["origin"] != "agent":
            raise HTTPException(404, "reply_not_found")

        def record_usage(usage):
            rt.emit(
                "speech_usage",
                {
                    "stage": "tts",
                    "message_id": message_id,
                    "model": settings.tts_model,
                    "usage": usage,
                },
            )

        if not stream:
            task, watcher = speech_task(rt, request, app.state.speech.synthesize(message["text"]))
            try:
                audio, usage = await task
                check_generation(rt, current_generation)
                record_usage(usage)
                return Response(
                    audio, media_type="audio/wav", headers={"Cache-Control": "no-store"}
                )
            except asyncio.CancelledError:
                raise HTTPException(409, "本轮已停止。") from None
            except SpeechUnavailable as exc:
                rt.emit("speech_error", {"stage": "tts", "message_id": message_id})
                raise HTTPException(502, str(exc)) from None
            finally:
                await close_speech_task(task, watcher)

        # 小队列提供背压；任务始终登记在会话内，插话、挂断和断开均可取消上游请求。
        chunks = asyncio.Queue(maxsize=4)

        async def produce():
            try:
                async with aclosing(
                    app.state.speech.stream_synthesize(message["text"], on_usage=record_usage)
                ) as source:
                    async for pcm in source:
                        await chunks.put(pcm)
                await chunks.put(None)
            except (asyncio.CancelledError, Exception) as exc:
                while not chunks.empty():
                    chunks.get_nowait()
                chunks.put_nowait(
                    VoiceInterrupted() if isinstance(exc, asyncio.CancelledError) else exc
                )
                if isinstance(exc, asyncio.CancelledError):
                    raise

        task, watcher = speech_task(rt, request, produce())

        def producer_done(completed):
            # 新任务可能在首次执行前就被插话取消，此时 produce 内部的异常分支尚未运行。
            if completed.cancelled() and chunks.empty():
                chunks.put_nowait(VoiceInterrupted())

        task.add_done_callback(producer_done)

        async def next_chunk():
            value = await chunks.get()
            if isinstance(value, Exception):
                raise value
            return value

        try:
            first = await next_chunk()
            check_generation(rt, current_generation)
        except BaseException as exc:
            await close_speech_task(task, watcher)
            if isinstance(exc, VoiceInterrupted):
                raise HTTPException(409, "本轮已停止。") from None
            if isinstance(exc, SpeechUnavailable):
                rt.emit("speech_error", {"stage": "tts", "message_id": message_id})
                raise HTTPException(502, str(exc)) from None
            raise

        async def audio_body():
            try:
                pcm = first
                while pcm is not None and current_generation == rt.voice_generation:
                    yield pcm
                    pcm = await next_chunk()
            except VoiceInterrupted:
                return
            except SpeechUnavailable:
                rt.emit("speech_error", {"stage": "tts", "message_id": message_id})
                raise
            finally:
                await close_speech_task(task, watcher)

        return StreamingResponse(
            audio_body(),
            media_type="audio/pcm",
            headers={"Cache-Control": "no-store", "X-Audio-Sample-Rate": str(PCM_SAMPLE_RATE)},
        )

    @app.post("/api/sessions/{sid}/stop")
    async def stop(sid: str, request: Request, body: StopInput):
        rt = runtime(request, sid)
        if body.reason == "agent_completed":
            # 告别所属轮次失效后，迟到的播放完成通知不能结束新一轮输入。
            if body.generation is None or body.generation != rt.voice_generation:
                raise HTTPException(409, "voice_generation_stale")
            if not rt.end_after_playback:
                raise HTTPException(409, "end_call_not_requested")
        await rt.stop(body.reason)
        return {"state": await public_state(rt)}

    @app.post("/api/sessions/{sid}/telemetry")
    async def telemetry(sid: str, request: Request, body: TelemetryInput):
        rt = runtime(request, sid)
        if len(str(body.data)) > 8000:
            raise HTTPException(413, "telemetry_too_large")
        rt.emit("client_telemetry", body.model_dump())
        return {"status": "success"}

    dist = settings.project_root / "frontend" / "dist"

    @app.get("/{path:path}")
    async def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        requested = (dist / path).resolve()
        if not requested.is_relative_to(dist.resolve()):
            raise HTTPException(403)
        if requested.is_file():
            return FileResponse(requested)
        if (dist / "index.html").is_file():
            return FileResponse(dist / "index.html")
        return JSONResponse({"message": "前端尚未构建，请运行 growth up。"}, status_code=503)

    return app
