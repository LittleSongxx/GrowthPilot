"""验证结束意图与实际播放分开处理，插话撤销结束，重播保留同轮意图。"""

import asyncio

import pytest

from backend.channels.speech import SpeechUnavailable
from tests.runtime.test_conversation import setup as setup  # noqa: PLC0414
from tests.runtime.test_speech_pipeline import send_audio
from tests.runtime.test_speech_pipeline import speech_stack as speech_stack  # noqa: PLC0414


def request_end(rt):
    """仅替代模型决策，API、宿主工具、音频和停止流程均按真实实现执行。"""

    async def finish():
        await rt.tools["end_call"]("这是业务回复。")

    rt.agent.act = finish


@pytest.mark.parametrize("path", ["turn", "start"])
async def test_end_intent_waits_for_current_playback_completion(speech_stack, path):
    host, http, speech, rt, auth = speech_stack
    request_end(rt)
    response = (
        await send_audio(http, rt, auth)
        if path == "turn"
        else await http.post(f"/api/sessions/{rt.id}/voice/start?generation=0", headers=auth)
    )
    result = response.json()
    assert response.status_code == 200 and result["end_after_playback"]
    assert host.store.get(rt.id)["status"] == "active"
    audio = await http.get(
        f"/api/sessions/{rt.id}/voice/audio/{result['reply_message_id']}", headers=auth
    )
    assert audio.status_code == 200 and host.store.get(rt.id)["status"] == "active"
    speech.synthesize.assert_awaited_once_with("这是业务回复。")
    stopped = await http.post(
        f"/api/sessions/{rt.id}/stop",
        headers=auth,
        json={"reason": "agent_completed", "generation": 0},
    )
    assert stopped.status_code == 200 and host.store.get(rt.id)["status"] == "closed"


@pytest.mark.parametrize("new_input", ["interrupt", "message", "input_only"])
async def test_new_input_revokes_old_end_intent(speech_stack, new_input):
    host, http, _, rt, auth = speech_stack
    request_end(rt)
    assert (await send_audio(http, rt, auth)).json()["end_after_playback"]
    rt.agent.act = None
    if new_input == "interrupt":
        await http.post(f"/api/sessions/{rt.id}/voice/interrupt", headers=auth, json={})
    else:
        extra = "&input_only=true" if new_input == "input_only" else ""
        result = await http.post(
            f"/api/sessions/{rt.id}/voice/turn?generation=0{extra}",
            headers={**auth, "Content-Type": "audio/wav"},
            content=b"new-input",
        )
        assert result.status_code == 200 and result.json()["end_after_playback"] is False
    assert not rt.end_after_playback
    stale = await http.post(
        f"/api/sessions/{rt.id}/stop",
        headers=auth,
        json={"reason": "agent_completed", "generation": 0},
    )
    assert stale.status_code == 409 and host.store.get(rt.id)["status"] == "active"


async def test_new_recording_revokes_end_intent_before_asr_returns(speech_stack):
    host, http, speech, rt, auth = speech_stack
    request_end(rt)
    assert (await send_audio(http, rt, auth)).json()["end_after_playback"]
    entered = asyncio.Event()

    async def wait(*args):
        entered.set()
        await asyncio.Event().wait()

    speech.transcribe.side_effect = wait
    pending = asyncio.create_task(send_audio(http, rt, auth))
    await entered.wait()
    stale = await http.post(
        f"/api/sessions/{rt.id}/stop",
        headers=auth,
        json={"reason": "agent_completed", "generation": 0},
    )
    assert stale.status_code == 409 and host.store.get(rt.id)["status"] == "active"
    await http.post(f"/api/sessions/{rt.id}/stop", headers=auth, json={"reason": "user_stop"})
    assert (await pending).status_code == 409


async def test_tts_failure_preserves_end_intent_for_same_reply_retry(speech_stack):
    host, http, speech, rt, auth = speech_stack
    request_end(rt)
    result = (await send_audio(http, rt, auth)).json()
    speech.synthesize.side_effect = SpeechUnavailable("语音暂不可用，请重新播放。")
    path = f"/api/sessions/{rt.id}/voice/audio/{result['reply_message_id']}"
    assert (await http.get(path, headers=auth)).status_code == 502
    assert rt.end_after_playback and host.store.get(rt.id)["status"] == "active"
    speech.synthesize.side_effect = None
    assert (await http.get(path, headers=auth)).status_code == 200
    assert rt.end_after_playback and len(rt.agent.inputs) == 1


async def test_literal_goodbye_without_end_tool_does_not_auto_stop(speech_stack):
    host, http, speech, rt, auth = speech_stack
    speech.transcribe.return_value = ("好的，再见。", None)
    assert (await send_audio(http, rt, auth)).json()["end_after_playback"] is False
    for body in ({"reason": "agent_completed"}, {"reason": "agent_completed", "generation": 0}):
        assert (
            await http.post(f"/api/sessions/{rt.id}/stop", headers=auth, json=body)
        ).status_code == 409
    assert host.store.get(rt.id)["status"] == "active"
