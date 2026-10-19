# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared speech-to-text client used by task chat and video duplex.

``AsrClient`` picks one adapter from the configured endpoint:

* ``ws://`` / ``wss://``: Alibaba Cloud Model Studio (DashScope) ``run-task``
  streaming recognition. Audio is converted to 16 kHz mono PCM first. Other
  WebSocket ASR protocols are not supported.
* ``http(s)://.../audio/transcriptions`` (or a base URL whose model name looks
  like a transcription model): OpenAI-compatible transcription.
* other ``http(s)://`` addresses: OpenAI-compatible chat completions with an
  ``audio_url`` content part.

To support another service, add an adapter with an async
``transcribe(audio, mime_type, filename)`` method and select it in
``AsrClient._select_adapter``.
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
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx
from websockets.exceptions import ConnectionClosed, InvalidHandshake, WebSocketException

try:  # websockets >= 13: asyncio client takes ``additional_headers``.
    from websockets.asyncio.client import connect as _ws_connect

    _WS_HEADERS_KWARG = "additional_headers"
except ImportError:  # websockets 12 (pyproject allows >=12): legacy client.
    from websockets.legacy.client import connect as _ws_connect

    _WS_HEADERS_KWARG = "extra_headers"


# Default path of the DashScope WebSocket inference endpoint
# (help.aliyun.com/zh/model-studio/qwen-audio-asr-streaming-websocket-api).
DASHSCOPE_WS_PATH = "/api-ws/v1/inference"
# 来源: SPEC fun-asr-client-events run-task parameters {"format": "pcm", "sample_rate": 16000}
_PCM_SAMPLE_RATE = 16000
# 100 ms of 16-bit mono PCM per binary frame (来源: 任务单"每块 100ms").
_PCM_CHUNK_BYTES = _PCM_SAMPLE_RATE * 2 // 10
# 来源: 沿用已有配置 task_asr httpx.Timeout(120.0, connect=15.0) 的 connect 值
_WS_CONNECT_TIMEOUT_SECONDS = 15.0
# 来源: 沿用已有配置 qwen_omni_gateway.py websockets.connect(close_timeout=5, max_size=8 MiB)
_WS_CLOSE_TIMEOUT_SECONDS = 5
_WS_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
# Prompt and token budget of the original video-duplex chat ASR request.
_CHAT_ASR_PROMPT = "只转写清晰可辨的用户中文，不要解释；静音、噪声或听不清时返回空字符串，不得补写。"
_CHAT_ASR_MAX_TOKENS = 160
# MIME type -> file suffix for every recording format the callers accept.
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
    """ASR failure with a stable code; callers map it to their own errors."""

    def __init__(
        self, code: str, detail: str, *, status_code: int | None = None
    ) -> None:
        prefix = f"ASR 请求失败 ({status_code})" if status_code else "ASR 请求失败"
        super().__init__(f"{prefix}: {detail}" if detail else prefix)
        self.code = code
        self.detail = detail
        self.status_code = status_code


class AsrAdapter(Protocol):
    async def transcribe(self, audio: bytes, mime_type: str, filename: str) -> str: ...


def decrypt_api_key(value: str) -> str:
    """Decrypt an ``*_API_KEY`` value saved through the config panel."""

    if not value:
        return ""
    try:
        from jiuwenswarm.common.security.base_crypto import get_crypto_provider

        crypto = get_crypto_provider()
        return str(crypto.decrypt(value)) if crypto else value
    except Exception:  # noqa: BLE001
        return value


def normalize_ws_endpoint(endpoint: str) -> str:
    """Add the DashScope inference path to a ws(s) address that has none."""
    parts = urlsplit(endpoint)
    path = parts.path.rstrip("/") or DASHSCOPE_WS_PATH
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _asr_uses_transcription_endpoint(model: str, mode: str | None) -> bool:
    mode = str(mode or "").strip().casefold()
    if mode in {"transcription", "transcriptions", "audio"}:
        return True
    if mode in {"chat", "chat_completion", "chat_completions"}:
        return False
    normalized = model.casefold()
    return any(name in normalized for name in ("sensevoice", "whisper", "telespeechasr"))


def _openai_endpoint_base(endpoint: str, route: str) -> str:
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    if not path.casefold().endswith(route.casefold()):
        return endpoint.rstrip("/")
    base_path = path[: -len(route)].rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))


def resolve_asr_endpoint(
    endpoint: str, model: str, mode: str | None = None
) -> tuple[str, str]:
    """Resolve an HTTP ASR endpoint to its request kind and SDK base URL.

    ``mode`` forces ``transcription``/``chat`` for a base URL; without it the
    model name decides.
    """
    path = urlsplit(endpoint).path.rstrip("/").casefold()
    transcription_route = "/audio/transcriptions"
    chat_route = "/chat/completions"
    if path.endswith(transcription_route):
        return "transcription", _openai_endpoint_base(endpoint, transcription_route)
    if path.endswith(chat_route):
        return "chat", _openai_endpoint_base(endpoint, chat_route)
    kind = "transcription" if _asr_uses_transcription_endpoint(model, mode) else "chat"
    return kind, endpoint.rstrip("/")


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
        if payload.get("message"):
            return str(payload["message"])
        if payload.get("detail"):
            return str(payload["detail"])
    body = response.text.strip()
    return body[:500] if body else f"HTTP {response.status_code}"


def _total_seconds(timeout: float | httpx.Timeout) -> float | None:
    """Overall budget for one WebSocket task: the read timeout of an httpx.Timeout."""
    if isinstance(timeout, httpx.Timeout):
        return timeout.read
    return float(timeout)


class _OpenAICompatibleAdapter:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float | httpx.Timeout,
        max_retries: int,
        http_client: httpx.AsyncClient | None,
        default_query: Mapping[str, str] | None = None,
    ) -> None:
        self._base_url = base_url
        self._default_query = dict(default_query or {})
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self._http_client = http_client

    async def transcribe(self, audio: bytes, mime_type: str, filename: str) -> str:
        import openai

        client_kwargs: dict[str, Any] = {
            "api_key": self._api_key,
            "base_url": self._base_url,
            "timeout": self._timeout,
            "max_retries": self._max_retries,
        }
        if self._http_client is not None:
            client_kwargs["http_client"] = self._http_client
        if self._default_query:
            client_kwargs["default_query"] = self._default_query
        client = openai.AsyncOpenAI(**client_kwargs)
        try:
            return await self._request(client, audio, mime_type, filename)
        except openai.APIResponseValidationError as exc:
            raise AsrError("ASR_INVALID_RESPONSE", f"返回内容无法解析: {exc}") from exc
        except openai.APITimeoutError as exc:
            raise AsrError("ASR_TIMEOUT", "请求超时") from exc
        except openai.APIConnectionError as exc:
            raise AsrError("ASR_CONNECTION_ERROR", str(exc.__cause__ or exc)) from exc
        except openai.APIStatusError as exc:
            raise AsrError(
                "ASR_UPSTREAM_ERROR",
                _response_error(exc.response),
                status_code=exc.status_code,
            ) from exc
        except (ValueError, IndexError, AttributeError, TypeError) as exc:
            # Broken JSON body, or a JSON body without the expected fields.
            raise AsrError("ASR_INVALID_RESPONSE", f"返回内容无法解析: {exc}") from exc
        finally:
            if self._http_client is None:
                await client.close()

    async def _request(self, client: Any, audio: bytes, mime_type: str, filename: str) -> str:
        raise NotImplementedError


def _reject_raw_text(response: Any) -> None:
    # The SDK returns the raw body as ``str`` when it is not JSON (e.g. an HTML login page).
    if isinstance(response, str):
        raise AsrError("ASR_INVALID_RESPONSE", f"返回的不是 JSON: {response.strip()[:200]}")


class OpenAITranscriptionAdapter(_OpenAICompatibleAdapter):
    """POST ``{base}/audio/transcriptions`` with a multipart file."""

    async def _request(self, client: Any, audio: bytes, mime_type: str, filename: str) -> str:
        response = await client.audio.transcriptions.create(
            model=self._model,
            file=(filename, audio, mime_type),
        )
        _reject_raw_text(response)
        return str(getattr(response, "text", "") or "").strip()


class OpenAIChatAudioAdapter(_OpenAICompatibleAdapter):
    """POST ``{base}/chat/completions`` with an ``audio_url`` content part."""

    async def _request(self, client: Any, audio: bytes, mime_type: str, filename: str) -> str:
        del filename
        data_url = f"data:{mime_type};base64,{base64.b64encode(audio).decode('ascii')}"
        response = await client.chat.completions.create(
            model=self._model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "audio_url", "audio_url": {"url": data_url}},
                    {"type": "text", "text": _CHAT_ASR_PROMPT},
                ],
            }],
            max_tokens=_CHAT_ASR_MAX_TOKENS,
            temperature=0,
        )
        _reject_raw_text(response)
        return str(response.choices[0].message.content or "").strip()


def _find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        exe = str(imageio_ffmpeg.get_ffmpeg_exe() or "").strip()
        return exe or None
    except Exception:  # noqa: BLE001
        return None


def to_pcm16k(audio: bytes, suffix: str, timeout: float | None) -> bytes:
    """Decode a browser recording to 16 kHz mono s16le PCM with ffmpeg."""

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise AsrError("ASR_AUDIO_DECODE_FAILED", "未找到 ffmpeg，无法转换录音格式")
    with tempfile.TemporaryDirectory(prefix="asr-") as tmp:
        source = os.path.join(tmp, f"recording{suffix}")
        with open(source, "wb") as handle:
            handle.write(audio)
        kwargs: dict[str, Any] = {}
        # Frozen Windows builds are console=False; hide the ffmpeg console window.
        if os.name == "nt":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        try:
            result = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-i", source,
                    "-f", "s16le", "-acodec", "pcm_s16le",
                    "-ac", "1", "-ar", str(_PCM_SAMPLE_RATE),
                    "pipe:1",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
                **kwargs,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AsrError("ASR_AUDIO_DECODE_FAILED", f"录音格式转换失败: {exc}") from exc
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip()[-300:]
        raise AsrError(
            "ASR_AUDIO_DECODE_FAILED", f"录音格式转换失败: {detail or 'empty output'}"
        )
    return result.stdout


class DashScopeRunTaskAdapter:
    """Alibaba Cloud Model Studio ``run-task`` streaming recognition over WebSocket.

    Protocol: help.aliyun.com/zh/model-studio/fun-asr-client-events and
    fun-asr-server-events.
    """

    def __init__(self, url: str, api_key: str, model: str, *, timeout: float | None) -> None:
        self._url = url
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    async def transcribe(self, audio: bytes, mime_type: str, filename: str) -> str:
        suffix = os.path.splitext(filename)[1] or AUDIO_SUFFIXES.get(mime_type, ".audio")
        pcm = await asyncio.to_thread(to_pcm16k, audio, suffix, self._timeout)
        try:
            return await asyncio.wait_for(self._run_task(pcm), timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            raise AsrError("ASR_TIMEOUT", "请求超时") from exc
        except InvalidHandshake as exc:
            # InvalidStatus (asyncio client) / InvalidStatusCode (legacy client).
            response = getattr(exc, "response", None)
            status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
            if status is not None:
                raise AsrError(
                    "ASR_UPSTREAM_ERROR", "握手被拒绝，请检查 API Key 和地址", status_code=status
                ) from exc
            raise AsrError("ASR_CONNECTION_ERROR", str(exc)) from exc
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
            open_timeout=_WS_CONNECT_TIMEOUT_SECONDS,
            close_timeout=_WS_CLOSE_TIMEOUT_SECONDS,
            max_size=_WS_MAX_MESSAGE_BYTES,
        ) as conn:
            await conn.send(json.dumps({
                "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                "payload": {
                    "task_group": "audio",
                    "task": "asr",
                    "function": "recognition",
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
            while True:
                event, message = await _next_event(conn)
                if event == "task-finished":
                    break
                if event != "result-generated":
                    continue
                sentence = _dig(message, "payload", "output", "sentence")
                if sentence.get("heartbeat") or not sentence.get("sentence_end"):
                    continue
                text = str(sentence.get("text") or "").strip()
                if text:
                    sentences.append(text)
        return "".join(sentences)


def _dig(message: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    current: Any = message
    for key in keys:
        current = current.get(key) if isinstance(current, Mapping) else None
    return current if isinstance(current, Mapping) else {}


async def _next_event(conn: Any) -> tuple[str, dict[str, Any]]:
    while True:
        raw = await conn.recv()
        if isinstance(raw, bytes):
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        header = _dig(message, "header")
        event = str(header.get("event") or "")
        if event == "task-failed":
            code = str(header.get("error_code") or "").strip()
            detail = str(header.get("error_message") or "").strip()
            raise AsrError(
                "ASR_UPSTREAM_ERROR",
                ": ".join(part for part in (code, detail) if part) or "task-failed",
            )
        return event, message


class AsrClient:
    """Speech-to-text for one configured endpoint/key/model."""

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
        """``transcription_mode`` (``transcription``/``chat``) overrides the
        model-name guess for an HTTP base URL; ws(s) ignores it."""
        self._adapter = self._select_adapter(
            str(endpoint or "").strip(),
            str(api_key or ""),
            str(model or "").strip(),
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
            transcription_mode=transcription_mode,
        )

    @staticmethod
    def _select_adapter(
        endpoint: str,
        api_key: str,
        model: str,
        *,
        timeout: float | httpx.Timeout,
        max_retries: int,
        http_client: httpx.AsyncClient | None,
        transcription_mode: str | None,
    ) -> AsrAdapter:
        parts = urlsplit(endpoint)
        if parts.scheme in {"ws", "wss"} and parts.netloc:
            return DashScopeRunTaskAdapter(
                normalize_ws_endpoint(endpoint), api_key, model, timeout=_total_seconds(timeout)
            )
        if parts.scheme in {"http", "https"} and parts.netloc:
            kind, base_url = resolve_asr_endpoint(
                urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
                model,
                transcription_mode,
            )
            adapter_cls = (
                OpenAITranscriptionAdapter if kind == "transcription" else OpenAIChatAudioAdapter
            )
            return adapter_cls(
                base_url,
                api_key,
                model,
                timeout=timeout,
                max_retries=max_retries,
                http_client=http_client,
                # Keep query parameters such as Azure's api-version on every request.
                default_query=dict(parse_qsl(parts.query, keep_blank_values=True)),
            )
        raise AsrError("ASR_BAD_CONFIG", "ASR 地址必须是有效的 HTTP(S) 或 WS(S) 地址")

    async def transcribe(
        self, audio: bytes, mime_type: str, *, filename: str | None = None
    ) -> str:
        """Return the stripped transcript; an empty string when nothing was heard."""

        mime = str(mime_type or "").split(";", 1)[0].strip().lower()
        name = filename or f"recording{AUDIO_SUFFIXES.get(mime, '.audio')}"
        return await self._adapter.transcribe(audio, mime, name)
