# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ASR used by the regular task chat composer.

HTTP(S) addresses use the OpenAI-compatible ``/audio/transcriptions`` route.
WS(S) addresses use the Alibaba Cloud Model Studio (DashScope) ``run-task``
streaming recognition protocol; other WebSocket ASR protocols are not supported.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.exceptions import ConnectionClosed, InvalidHandshake, WebSocketException

try:  # websockets >= 13: asyncio client takes ``additional_headers``.
    from websockets.asyncio.client import connect as _ws_connect

    _WS_HEADERS_KWARG = "additional_headers"
except ImportError:  # websockets 12 (pyproject allows >=12): legacy client.
    from websockets.legacy.client import connect as _ws_connect

    _WS_HEADERS_KWARG = "extra_headers"


MAX_TASK_ASR_AUDIO_BYTES = 25 * 1024 * 1024
# Same request budget as the HTTP path (httpx.Timeout(120.0, connect=15.0)).
_TASK_ASR_TIMEOUT_SECONDS = 120.0
_TASK_ASR_CONNECT_TIMEOUT_SECONDS = 15.0
# Default path of the DashScope WebSocket inference endpoint
# (help.aliyun.com/zh/model-studio/qwen-audio-asr-streaming-websocket-api).
_DASHSCOPE_WS_PATH = "/api-ws/v1/inference"
# run-task parameters: 16 kHz mono 16-bit PCM, sent in 100 ms chunks.
_PCM_SAMPLE_RATE = 16000
_PCM_CHUNK_BYTES = _PCM_SAMPLE_RATE * 2 // 10
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_AUDIO_EXTENSIONS = {
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


class TaskAsrError(RuntimeError):
    """A user-facing ASR failure with a stable RPC error code."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def task_asr_endpoint(api_base: str) -> str:
    """Resolve the configured ASR address to the request URL.

    HTTP(S) resolves to the OpenAI transcription route; WS(S) without a path
    resolves to the DashScope inference path.
    """

    value = str(api_base or "").strip().rstrip("/")
    if not value:
        raise TaskAsrError(
            "请先在设置-实验功能中配置任务对话 ASR API 地址", "ASR_NOT_CONFIGURED"
        )

    parts = urlsplit(value)
    if parts.scheme not in {"http", "https", "ws", "wss"} or not parts.netloc:
        raise TaskAsrError(
            "任务对话 ASR API 地址必须是有效的 HTTP(S) 或 WS(S) 地址", "ASR_BAD_CONFIG"
        )

    if parts.scheme in {"ws", "wss"}:
        path = parts.path.rstrip("/") or _DASHSCOPE_WS_PATH
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))

    path = parts.path.rstrip("/")
    if not path.endswith("/audio/transcriptions"):
        path = f"{path}/audio/transcriptions"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _decode_audio(raw: Any) -> bytes:
    encoded = str(raw or "").strip()
    if not encoded:
        raise TaskAsrError("录音内容为空", "BAD_REQUEST")
    if len(encoded) > ((MAX_TASK_ASR_AUDIO_BYTES + 2) // 3) * 4 + 4:
        raise TaskAsrError("录音超过 25 MB 限制", "ASR_AUDIO_TOO_LARGE")
    try:
        audio = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise TaskAsrError("录音数据不是有效的 Base64", "BAD_REQUEST") from exc
    if not audio:
        raise TaskAsrError("录音内容为空", "BAD_REQUEST")
    if len(audio) > MAX_TASK_ASR_AUDIO_BYTES:
        raise TaskAsrError("录音超过 25 MB 限制", "ASR_AUDIO_TOO_LARGE")
    return audio


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


def _decrypt_api_key(value: str) -> str:
    if not value:
        return ""
    try:
        from jiuwenswarm.extensions.registry import ExtensionRegistry

        crypto = ExtensionRegistry.get_instance().get_crypto_provider()
        return str(crypto.decrypt(value)) if crypto else value
    except Exception:  # noqa: BLE001
        return value


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


def _to_pcm16k(audio: bytes, extension: str) -> bytes:
    """Decode a browser recording to 16 kHz mono s16le PCM with ffmpeg."""

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise TaskAsrError("未找到 ffmpeg，无法转换录音格式", "ASR_AUDIO_DECODE_FAILED")
    with tempfile.TemporaryDirectory(prefix="task-asr-") as tmp:
        source = os.path.join(tmp, f"recording{extension}")
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
                timeout=_TASK_ASR_TIMEOUT_SECONDS,
                check=False,
                **kwargs,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TaskAsrError(f"录音格式转换失败: {exc}", "ASR_AUDIO_DECODE_FAILED") from exc
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip()[-300:]
        raise TaskAsrError(f"录音格式转换失败: {detail or 'empty output'}", "ASR_AUDIO_DECODE_FAILED")
    return result.stdout


def _task_failed_message(header: Mapping[str, Any]) -> str:
    code = str(header.get("error_code") or "").strip()
    message = str(header.get("error_message") or "").strip()
    return ": ".join(part for part in (code, message) if part) or "task-failed"


async def _run_dashscope_task(endpoint: str, api_key: str, model: str, pcm: bytes) -> str:
    task_id = uuid.uuid4().hex
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    sentences: list[str] = []

    async def next_event(conn: Any) -> tuple[str, dict[str, Any]]:
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
            header = message.get("header") if isinstance(message.get("header"), dict) else {}
            event = str(header.get("event") or "")
            if event == "task-failed":
                raise TaskAsrError(
                    f"任务对话 ASR 请求失败: {_task_failed_message(header)}",
                    "ASR_UPSTREAM_ERROR",
                )
            return event, message

    async with _ws_connect(
        endpoint,
        **{_WS_HEADERS_KWARG: headers},
        open_timeout=_TASK_ASR_CONNECT_TIMEOUT_SECONDS,
        close_timeout=5,
        max_size=8 * 1024 * 1024,
    ) as conn:
        await conn.send(json.dumps({
            "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": model,
                "parameters": {"format": "pcm", "sample_rate": _PCM_SAMPLE_RATE},
                "input": {},
            },
        }))
        while (await next_event(conn))[0] != "task-started":
            pass
        for offset in range(0, len(pcm), _PCM_CHUNK_BYTES):
            await conn.send(pcm[offset:offset + _PCM_CHUNK_BYTES])
        await conn.send(json.dumps({
            "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {}},
        }))
        while True:
            event, message = await next_event(conn)
            if event == "task-finished":
                break
            if event != "result-generated":
                continue
            payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
            output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
            sentence = output.get("sentence") if isinstance(output.get("sentence"), dict) else {}
            if sentence.get("heartbeat") or not sentence.get("sentence_end"):
                continue
            text = str(sentence.get("text") or "").strip()
            if text:
                sentences.append(text)
    return "".join(sentences)


async def _transcribe_via_dashscope_ws(
    endpoint: str, api_key: str, model: str, audio: bytes, extension: str
) -> str:
    pcm = await asyncio.to_thread(_to_pcm16k, audio, extension)
    try:
        text = await asyncio.wait_for(
            _run_dashscope_task(endpoint, api_key, model, pcm),
            timeout=_TASK_ASR_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise TaskAsrError("任务对话 ASR 请求超时", "ASR_TIMEOUT") from exc
    except InvalidHandshake as exc:
        # InvalidStatus (asyncio client) / InvalidStatusCode (legacy client).
        response = getattr(exc, "response", None)
        status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
        if status is not None:
            raise TaskAsrError(
                f"任务对话 ASR 请求失败 ({status}): 握手被拒绝，请检查 API Key 和地址",
                "ASR_UPSTREAM_ERROR",
            ) from exc
        raise TaskAsrError(
            f"无法连接任务对话 ASR 服务: {exc}", "ASR_CONNECTION_ERROR"
        ) from exc
    except ConnectionClosed as exc:
        raise TaskAsrError(
            f"任务对话 ASR 连接提前关闭: {exc}", "ASR_UPSTREAM_ERROR"
        ) from exc
    except (OSError, WebSocketException) as exc:
        raise TaskAsrError(
            f"无法连接任务对话 ASR 服务: {exc}", "ASR_CONNECTION_ERROR"
        ) from exc
    text = text.strip()
    if not text:
        raise TaskAsrError("任务对话 ASR 未返回转写文本", "ASR_EMPTY_TRANSCRIPT")
    return text


async def transcribe_task_audio(
    params: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Transcribe one browser recording with the task-chat ASR configuration."""

    env = environ if environ is not None else os.environ
    endpoint = task_asr_endpoint(env.get("ASR_API_BASE", ""))
    api_key = _decrypt_api_key(str(env.get("ASR_API_KEY", "") or "").strip())
    model = str(env.get("ASR_MODEL_NAME", "") or "").strip()
    if not model:
        raise TaskAsrError(
            "请先在设置-实验功能中配置任务对话 ASR 模型", "ASR_NOT_CONFIGURED"
        )

    mime_type = (
        str(params.get("mime_type") or "audio/webm").split(";", 1)[0].strip().lower()
    )
    extension = _AUDIO_EXTENSIONS.get(mime_type)
    if extension is None:
        raise TaskAsrError(f"不支持的录音格式: {mime_type}", "ASR_UNSUPPORTED_FORMAT")
    audio = _decode_audio(params.get("audio_base64"))
    if urlsplit(endpoint).scheme in {"ws", "wss"}:
        return await _transcribe_via_dashscope_ws(endpoint, api_key, model, audio, extension)
    filename = f"task-recording{extension}"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    owns_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=15.0),
        follow_redirects=True,
    )
    try:
        response = await active_client.post(
            endpoint,
            headers=headers,
            data={"model": model},
            files={"file": (filename, audio, mime_type)},
        )
    except httpx.TimeoutException as exc:
        raise TaskAsrError("任务对话 ASR 请求超时", "ASR_TIMEOUT") from exc
    except httpx.RequestError as exc:
        raise TaskAsrError(
            f"无法连接任务对话 ASR 服务: {exc}", "ASR_CONNECTION_ERROR"
        ) from exc
    finally:
        if owns_client:
            await active_client.aclose()

    if response.is_error:
        raise TaskAsrError(
            f"任务对话 ASR 请求失败 ({response.status_code}): {_response_error(response)}",
            "ASR_UPSTREAM_ERROR",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise TaskAsrError(
            "任务对话 ASR 返回了无效 JSON", "ASR_INVALID_RESPONSE"
        ) from exc
    text = str(payload.get("text") or "").strip() if isinstance(payload, dict) else ""
    if not text:
        raise TaskAsrError("任务对话 ASR 未返回转写文本", "ASR_EMPTY_TRANSCRIPT")
    return text
