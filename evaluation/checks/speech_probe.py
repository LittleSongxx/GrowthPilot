"""对真实的语音识别（ASR）→业务 Agent→语音合成（TTS）链路进行有限范围的冒烟检查；使用合成语音和隔离数据库。

uv run python -m evaluation.checks.speech_probe --output .run/checks/speech-check.json
可选的 --webm 参数仅通过本机安装的 ffmpeg 准备浏览器格式的测试输入。
这是集成链路探针，不代表质量评测或人工听测结果。
"""

import argparse
import asyncio
import subprocess
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path

import httpx

from backend.agent_harness import BackendService, load_scenario
from backend.agent_harness.rag.index import PolicyIndex
from backend.agent_harness.runtime.session_manager import SessionManager
from backend.api.app import create_app
from backend.channels.speech import SpeechAPI
from backend.database import temporary_database
from backend.paths import resolve_project_path
from backend.settings import Settings

from ..common.io import dump

INPUTS = {
    "didi_driver": "方便交流。我在厦门，请记住我的城市。请查看适用技能，查清司机注册条件并口头说明下一步。",
    "meituan_rider": "方便交流。我在厦门，想了解如何申请成为美团骑手。请查看适用技能并查询政策，口头说明如何下载注册。",
    "meituan_merchant": "方便交流。我的门店在厦门，我想了解美团商家入驻流程。请查看适用技能并查询政策，口头说明如何下载注册。",
    "financial_consultation": "方便交流。我只想先了解度小满 App，注册是不是就等于借款？请查看适用技能并查询政策后解答。",
}


async def run(output, webm, selected):
    output = resolve_project_path(output)
    settings = Settings.from_env()
    report = {
        "pipeline": "asr-agent-tts",
        "isolated_test_audio": True,
        "human_listening": "not_tested",
        "cases": [],
    }
    policy_index = PolicyIndex(settings.runtime_dir / "knowledge", database=settings.app_db)
    with (
        tempfile.TemporaryDirectory(prefix="growth-speech-") as temporary,
        temporary_database(settings.app_db, prefix="growth_speech") as database,
    ):
        root = Path(temporary)
        settings = replace(
            settings,
            runtime_dir=root,
            app_db=database,
        )
        services = {
            key: BackendService(
                settings.app_db,
                load_scenario(settings.project_root / f"backend/scenarios/{key}/scenario.yaml"),
            )
            for key in INPUTS
        }
        host = SessionManager(settings, services)
        # 语音探针只隔离会话数据，检索继续使用已经构建好的业务知识库。
        host._policy_index = policy_index
        app = create_app(settings, host)
        speech = SpeechAPI(settings)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=f"http://127.0.0.1:{settings.app_port}",
            ) as http:
                for scenario in selected:
                    text = INPUTS[scenario]
                    start = time.monotonic()
                    case = {"scenario": scenario, "input_text": text, "steps": []}
                    report["cases"].append(case)
                    try:
                        audio, _ = await speech.synthesize(text)
                        mime = "audio/wav"
                        if webm:
                            wav, encoded = root / "input.wav", root / "input.webm"
                            wav.write_bytes(audio)
                            await asyncio.to_thread(
                                subprocess.run,
                                [
                                    "ffmpeg",
                                    "-y",
                                    "-loglevel",
                                    "error",
                                    "-i",
                                    str(wav),
                                    "-c:a",
                                    "libopus",
                                    str(encoded),
                                ],
                                check=True,
                            )
                            audio, mime = encoded.read_bytes(), "audio/webm"
                        case["input_format"], case["input_bytes"] = mime, len(audio)
                        created = await http.post(
                            "/api/sessions",
                            json={
                                "scenario_id": scenario,
                                "customer_id": "probe-" + uuid.uuid4().hex[:12],
                            },
                        )
                        created.raise_for_status()
                        sid = created.json()["session_id"]
                        auth = {"Authorization": "Bearer " + created.json()["token"]}
                        rt = host.get(sid)
                        response = await http.post(
                            f"/api/sessions/{sid}/voice/turn",
                            headers={**auth, "Content-Type": mime},
                            content=audio,
                        )
                        case["steps"].append(
                            {
                                "stage": "asr_agent",
                                "status": response.status_code,
                                "response": response.json(),
                            }
                        )
                        if response.status_code == 200:
                            mid = response.json()["reply_message_id"]
                            playback = await http.get(
                                f"/api/sessions/{sid}/voice/audio/{mid}", headers=auth
                            )
                            case["steps"].append(
                                {
                                    "stage": "tts",
                                    "status": playback.status_code,
                                    "audio_bytes": len(playback.content),
                                    "wav_header": playback.content[:4] == b"RIFF",
                                }
                            )
                        state = rt.service.snapshot(rt.ctx)
                        case["state"] = state
                        case["events"] = host.store.events(sid, limit=3000)
                        case["passed"] = (
                            response.status_code == 200
                            and all(step["status"] == 200 for step in case["steps"])
                            and any(e["type"] == "skill_loaded" for e in case["events"])
                        )
                        if scenario == "didi_driver":
                            case["passed"] &= any(
                                m["field"] == "city"
                                and m["value"] == "厦门"
                                and m["source_kind"] == "customer_transcript"
                                for m in state["memory"]
                            )
                        await rt.stop("probe_complete")
                    except Exception as exc:  # noqa: BLE001 - 真实链路探针报告脱敏后的失败信息，并保留每个用例。
                        # 不序列化服务供应商的异常正文、请求头、URL 或原始音频。
                        case.update(passed=False, error_category=type(exc).__name__)
                    case["elapsed_seconds"] = round(time.monotonic() - start, 3)
                    dump(output, report)
                    print(scenario, "passed=" + str(case.get("passed")), flush=True)
        finally:
            await host.close()
            for service in services.values():
                service.close()
    report["passed"] = all(case.get("passed") for case in report["cases"])
    dump(output, report)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=resolve_project_path, required=True)
    parser.add_argument("--webm", action="store_true")
    parser.add_argument("--scenarios", nargs="+", choices=list(INPUTS), default=list(INPUTS))
    arguments = parser.parse_args()
    if arguments.output.exists():
        parser.error("Use a fresh output path; never overwrite a historical probe.")
    raise SystemExit(
        0 if asyncio.run(run(arguments.output, arguments.webm, arguments.scenarios)) else 1
    )
