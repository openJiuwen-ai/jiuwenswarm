# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Speech-to-text client shared by task chat and video duplex.

``AsrClient`` picks an adapter from the endpoint scheme and path:
ws(s) -> Alibaba Cloud Model Studio ``run-task`` streaming recognition;
http(s) -> OpenAI-compatible ``/audio/transcriptions`` or ``/chat/completions``.
A new service only needs a new adapter with ``transcribe(audio, mime_type, filename)``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx
from websockets.exceptions import ConnectionClosed, InvalidHandshake, WebSocketException

try:  # websockets >= 13
    from websockets.asyncio.client import connect as _ws_connect

    _WS_HEADERS_KWARG = "additional_headers"
except ImportError:  # websockets 12, still allowed by pyproject
    from websockets.legacy.client import connect as _ws_connect

    _WS_HEADERS_KWARG = "extra_headers"

# 来源: SPEC help.aliyun.com/zh/model-studio/qwen-audio-asr-streaming-websocket-api
DASHSCOPE_WS_PATH = "/api-ws/v1/inference"
# 来源: SPEC fun-asr-client-events run-task parameters {"format": "pcm", "sample_rate": 16000}
_PCM_SAMPLE_RATE = 16000
_PCM_CHUNK_BYTES = _PCM_SAMPLE_RATE * 2 // 10  # 100 ms per frame
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_CHAT_ASR_PROMPT = "只转写清晰可辨的用户中文，不要解释；静音、噪声或听不清时返回空字符串，不得补写。"
AUDIO_SUFFIXES = {
    "audio/flac": ".flac",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/webm": ".webm",
    "audio/x-m4a": ".m4a",
}


class AsrError(RuntimeError):
    def __init__(self, code: str, detail: str, *, status_code: int | None = None) -> None:
        super().__init__(f"ASR 请求失败{f' ({status_code})' if status_code else ''}: {detail}")
        self.code = code
        self.detail = detail
        self.status_code = status_code


def decrypt_api_key(value: str) -> str:
    if not value:
        return ""
    try:
        from jiuwenswarm.common.security.base_crypto import get_crypto_provider

        crypto = get_crypto_provider()
        return str(crypto.decrypt(value)) if crypto else value
    except Exception:  # noqa: BLE001
        return value


def normalize_ws_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    path = parts.path.rstrip("/") or DASHSCOPE_WS_PATH
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _strip_route(endpoint: str, route: str) -> str:
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path[: -len(route)].rstrip("/"), "", ""))


def resolve_asr_endpoint(endpoint: str, model: str, mode: str | None = None) -> tuple[str, str]:
    """Return (``transcription``|``chat``, SDK base URL) for an HTTP endpoint."""
    path = urlsplit(endpoint).path.rstrip("/").casefold()
    for kind, route in (("transcription", "/audio/transcriptions"), ("chat", "/chat/completions")):
        if path.endswith(route):
            return kind, _strip_route(endpoint, route)
    mode = str(mode or "").strip().casefold()
    if mode in {"transcription", "transcriptions", "audio"}:
        return "transcription", endpoint.rstrip("/")
    if mode in {"chat", "chat_completion", "chat_completions"}:
        return "chat", endpoint.rstrip("/")
    is_transcription = any(
        name in model.casefold() for name in ("sensevoice", "whisper", "telespeechasr")
    )
    return ("transcription" if is_transcription else "chat"), endpoint.rstrip("/")


def _response_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error.strip():
            return error.strip()
        for key in ("message", "detail"):
            if payload.get(key):
                return str(payload[key])
    body = response.text.strip()
    return body[:500] if body else f"HTTP {response.status_code}"


class _OpenAIAdapter:
    def __init__(self, base_url: str, api_key: str, model: str, **client_kwargs: Any) -> None:
        self._model = model
        self._client_kwargs = {"api_key": api_key, "base_url": base_url, **client_kwargs}

    async def transcribe(self, audio: bytes, mime_type: str, filename: str) -> str:
        import openai

        client = openai.AsyncOpenAI(**self._client_kwargs)
        try:
            response = await self._request(client, audio, mime_type, filename)
            # The SDK returns the raw body as str when it is not JSON (e.g. an HTML login page).
            if isinstance(response, str):
                raise AsrError("ASR_INVALID_RESPONSE", f"返回的不是 JSON: {response.strip()[:200]}")
            return self._text(response).strip()
        except openai.APITimeoutError as exc:
            raise AsrError("ASR_TIMEOUT", "请求超时") from exc
        except openai.APIConnectionError as exc:
            raise AsrError("ASR_CONNECTION_ERROR", str(exc.__cause__ or exc)) from exc
        except openai.APIStatusError as exc:
            raise AsrError(
                "ASR_UPSTREAM_ERROR", _response_error(exc.response), status_code=exc.status_code
            ) from exc
        except (openai.APIResponseValidationError, ValueError, LookupError, AttributeError, TypeError) as exc:
            raise AsrError("ASR_INVALID_RESPONSE", f"返回内容无法解析: {exc}") from exc
        finally:
            if "http_client" not in self._client_kwargs:
                await client.close()


class OpenAITranscriptionAdapter(_OpenAIAdapter):
    async def _request(self, client: Any, audio: bytes, mime_type: str, filename: str) -> Any:
        return await client.audio.transcriptions.create(
            model=self._model, file=(filename, audio, mime_type)
        )

    @staticmethod
    def _text(response: Any) -> str:
        return str(response.text or "")


class OpenAIChatAudioAdapter(_OpenAIAdapter):
    async def _request(self, client: Any, audio: bytes, mime_type: str, filename: str) -> Any:
        data_url = f"data:{mime_type};base64,{base64.b64encode(audio).decode('ascii')}"
        return await client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": [
                {"type": "audio_url", "audio_url": {"url": data_url}},
                {"type": "text", "text": _CHAT_ASR_PROMPT},
            ]}],
            max_tokens=160,  # 来源: 沿用 video_voice 原 chat ASR 请求
            temperature=0,
        )

    @staticmethod
    def _text(response: Any) -> str:
        return str(response.choices[0].message.content or "")


def _find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return str(imageio_ffmpeg.get_ffmpeg_exe() or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def to_pcm16k(audio: bytes, suffix: str, timeout: float | None) -> bytes:
    """Decode a browser recording to 16 kHz mono s16le PCM with ffmpeg."""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise AsrError("ASR_AUDIO_DECODE_FAILED", "未找到 ffmpeg，无法转换录音格式")
    # Frozen Windows builds are console=False; hide the ffmpeg console window.
    extra = {"creationflags": _CREATE_NO_WINDOW} if os.name == "nt" else {}
    with tempfile.TemporaryDirectory(prefix="asr-") as tmp:
        source = os.path.join(tmp, f"recording{suffix}")
        with open(source, "wb") as handle:
            handle.write(audio)
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", source,
                 "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1",
                 "-ar", str(_PCM_SAMPLE_RATE), "pipe:1"],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False, **extra,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AsrError("ASR_AUDIO_DECODE_FAILED", f"录音格式转换失败: {exc}") from exc
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip()[-300:] or "empty output"
        raise AsrError("ASR_AUDIO_DECODE_FAILED", f"录音格式转换失败: {detail}")
    return result.stdout


class DashScopeRunTaskAdapter:
    """Model Studio ``run-task`` recognition (help.aliyun.com/zh/model-studio/fun-asr-client-events)."""

    def __init__(self, url: str, api_key: str, model: str, timeout: float | None) -> None:
        self._url, self._api_key, self._model, self._timeout = url, api_key, model, timeout

    async def transcribe(self, audio: bytes, mime_type: str, filename: str) -> str:
        suffix = os.path.splitext(filename)[1] or AUDIO_SUFFIXES.get(mime_type, ".audio")
        pcm = await asyncio.to_thread(to_pcm16k, audio, suffix, self._timeout)
        try:
            return await asyncio.wait_for(self._run_task(pcm), timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            raise AsrError("ASR_TIMEOUT", "请求超时") from exc
        except InvalidHandshake as exc:
            # InvalidStatus (asyncio client) or InvalidStatusCode (legacy client).
            status = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "response", None), "status_code", None
            )
            if status is None:
                raise AsrError("ASR_CONNECTION_ERROR", str(exc)) from exc
            raise AsrError(
                "ASR_UPSTREAM_ERROR", "握手被拒绝，请检查 API Key 和地址", status_code=status
            ) from exc
        except ConnectionClosed as exc:
            raise AsrError("ASR_UPSTREAM_ERROR", f"连接提前关闭: {exc}") from exc
        except (OSError, WebSocketException) as exc:
            raise AsrError("ASR_CONNECTION_ERROR", str(exc)) from exc

    async def _run_task(self, pcm: bytes) -> str:
        task_id = uuid.uuid4().hex
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        sentences: list[str] = []
        async with _ws_connect(
            self._url,
            **{_WS_HEADERS_KWARG: headers},
            open_timeout=15,  # 来源: 沿用 task_asr 原 httpx.Timeout(connect=15.0)
            close_timeout=5,  # 来源: 沿用 qwen_omni_gateway websockets.connect(close_timeout=5)
            max_size=8 * 1024 * 1024,  # 来源: 沿用 qwen_omni_gateway websockets.connect(max_size=8 MiB)
        ) as conn:
            await conn.send(json.dumps({
                "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                "payload": {
                    "task_group": "audio", "task": "asr", "function": "recognition",
                    "model": self._model,
                    "parameters": {"format": "pcm", "sample_rate": _PCM_SAMPLE_RATE},
                    "input": {},
                },
            }))
            while (await _next_event(conn))[0] != "task-started":
                pass
            for offset in range(0, len(pcm), _PCM_CHUNK_BYTES):
                await conn.send(pcm[offset:offset + _PCM_CHUNK_BYTES])
            await conn.send(json.dumps({
                "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
                "payload": {"input": {}},
            }))
            while (event := await _next_event(conn))[0] != "task-finished":
                sentence = ((event[1].get("payload") or {}).get("output") or {}).get("sentence") or {}
                if event[0] == "result-generated" and sentence.get("sentence_end") and not sentence.get("heartbeat"):
                    sentences.append(str(sentence.get("text") or "").strip())
        return "".join(sentences)


async def _next_event(conn: Any) -> tuple[str, dict[str, Any]]:
    while True:
        raw = await conn.recv()
        try:
            message = json.loads(raw) if isinstance(raw, str) else None
        except ValueError:
            message = None
        if not isinstance(message, dict):
            continue
        header = message.get("header") or {}
        if header.get("event") == "task-failed":
            detail = ": ".join(
                str(header[key]).strip() for key in ("error_code", "error_message") if header.get(key)
            )
            raise AsrError("ASR_UPSTREAM_ERROR", detail or "task-failed")
        return str(header.get("event") or ""), message


class AsrClient:
    """Speech-to-text for one endpoint/key/model; the endpoint picks the adapter."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str,
        *,
        timeout: float | httpx.Timeout,
        max_retries: int,
        http_client: httpx.AsyncClient | None = None,
        transcription_mode: str | None = None,
    ) -> None:
        parts = urlsplit(endpoint.strip())
        if parts.scheme in {"ws", "wss"} and parts.netloc:
            total = timeout.read if isinstance(timeout, httpx.Timeout) else timeout
            self._adapter: Any = DashScopeRunTaskAdapter(
                normalize_ws_endpoint(endpoint.strip()), api_key, model, total
            )
            return
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise AsrError("ASR_BAD_CONFIG", "ASR 地址必须是有效的 HTTP(S) 或 WS(S) 地址")
        kind, base_url = resolve_asr_endpoint(
            urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")), model, transcription_mode
        )
        client_kwargs: dict[str, Any] = {"timeout": timeout, "max_retries": max_retries}
        if http_client is not None:
            client_kwargs["http_client"] = http_client
        if parts.query:  # keep e.g. Azure's api-version
            client_kwargs["default_query"] = dict(parse_qsl(parts.query, keep_blank_values=True))
        adapter = OpenAITranscriptionAdapter if kind == "transcription" else OpenAIChatAudioAdapter
        self._adapter = adapter(base_url, api_key, model, **client_kwargs)

    async def transcribe(self, audio: bytes, mime_type: str, *, filename: str | None = None) -> str:
        mime = str(mime_type or "").split(";", 1)[0].strip().lower()
        return await self._adapter.transcribe(
            audio, mime, filename or f"recording{AUDIO_SUFFIXES.get(mime, '.audio')}"
        )
