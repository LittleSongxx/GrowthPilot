"""识别整段客户录音，流式合成回复；语音服务不接收业务工具、技能或客户记忆。"""

import base64
import io
import json
import wave
from contextlib import aclosing

import httpx

MAX_AUDIO_BYTES = 2 * 1024 * 1024
AUDIO_TYPES = {"audio/webm", "audio/ogg", "audio/wav"}
PCM_SAMPLE_RATE = 24000


class SpeechUnavailable(RuntimeError):
    """汇总服务供应商错误，不包含密钥、签名 URL 或请求中的音频。"""


class SpeechAPI:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport

    async def _generate(self, path, payload):
        if not self.settings.dashscope_api_key:
            raise SpeechUnavailable("缺少 DASHSCOPE_API_KEY，请配置服务端 .env。")
        try:
            async with httpx.AsyncClient(timeout=60, transport=self.transport) as client:
                response = await client.post(
                    self.settings.dashscope_base_url.rstrip("/") + path,
                    headers={"Authorization": f"Bearer {self.settings.dashscope_api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError:
            raise SpeechUnavailable("云端语音服务暂不可用，请检查配置或稍后重试。") from None
        except (ValueError, KeyError, TypeError):
            raise SpeechUnavailable("云端语音服务返回格式异常，请稍后重试。") from None

    async def transcribe(self, audio, mime_type):
        data = await self._generate(
            "/compatible-mode/v1/chat/completions",
            {
                "model": self.settings.asr_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": f"data:{mime_type};base64,"
                                    + base64.b64encode(audio).decode()
                                },
                            }
                        ],
                    }
                ],
                "asr_options": {"language": "zh", "enable_itn": False},
                "stream": False,
            },
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise SpeechUnavailable("云端语音服务返回格式异常，请稍后重试。") from None
        return (content or "").strip(), data.get("usage")

    async def stream_synthesize(self, text, *, on_usage=None):
        """直接转发供应商的单声道 24 kHz、16 位小端 PCM，不等待完整文件。"""
        if not self.settings.dashscope_api_key:
            raise SpeechUnavailable("缺少 DASHSCOPE_API_KEY，请配置服务端 .env。")
        if not text.strip():
            raise SpeechUnavailable("回复为空，无法合成语音。")
        try:
            async with httpx.AsyncClient(timeout=60, transport=self.transport) as client:
                # 每块不超过供应商的 600 字限制；上一块结束后再合成下一块。
                for start in range(0, len(text), 600):
                    async with client.stream(
                        "POST",
                        self.settings.dashscope_base_url.rstrip("/")
                        + "/api/v1/services/aigc/multimodal-generation/generation",
                        headers={
                            "Authorization": f"Bearer {self.settings.dashscope_api_key}",
                            "X-DashScope-SSE": "enable",
                        },
                        json={
                            "model": self.settings.tts_model,
                            "input": {
                                "text": text[start : start + 600],
                                "voice": self.settings.tts_voice,
                                "language_type": "Chinese",
                            },
                        },
                    ) as response:
                        response.raise_for_status()
                        finished, received, usage, fields = False, False, None, []
                        async for line in response.aiter_lines():
                            if line.startswith("data:"):
                                fields.append(line[5:].lstrip())
                                continue
                            if line or not fields:
                                continue
                            data = json.loads("\n".join(fields))
                            fields = []
                            if data.get("code") or data.get("status_code", 200) != 200:
                                raise SpeechUnavailable("云端语音合成失败，请稍后重试。")
                            output = data.get("output", {})
                            encoded = (output.get("audio") or {}).get("data")
                            if encoded:
                                pcm = base64.b64decode(encoded, validate=True)
                                if len(pcm) % 2:
                                    raise SpeechUnavailable("云端语音片段格式错误，请稍后重试。")
                                received = True
                                yield pcm
                            usage = data.get("usage") or usage
                            if output.get("finish_reason") == "stop":
                                finished = True
                        if not finished or not received:
                            raise SpeechUnavailable("语音流未完整返回，请重新播放。")
                        if on_usage:
                            on_usage(usage)
        except (httpx.HTTPError, ValueError, TypeError):
            raise SpeechUnavailable("云端语音服务暂不可用，请检查配置或稍后重试。") from None

    async def synthesize(self, text):
        """评测和完整文件接口共用流式实现，收齐 PCM 后包装为标准 WAV。"""
        output, usages = io.BytesIO(), []
        with wave.open(output, "wb") as audio:
            audio.setparams((1, 2, PCM_SAMPLE_RATE, 0, "NONE", "not compressed"))
            async with aclosing(self.stream_synthesize(text, on_usage=usages.append)) as stream:
                async for pcm in stream:
                    audio.writeframes(pcm)
        usage = usages[0] if len(usages) == 1 else {"chunks": usages}
        return output.getvalue(), usage
