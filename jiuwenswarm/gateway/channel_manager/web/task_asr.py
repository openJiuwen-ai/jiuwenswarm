# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""OpenAI-compatible ASR used by the regular task chat composer."""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


MAX_TASK_ASR_AUDIO_BYTES = 25 * 1024 * 1024

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
    """Resolve a base URL or full transcription URL to the OpenAI route."""

    value = str(api_base or "").strip().rstrip("/")
    if not value:
        raise TaskAsrError(
            "请先在设置-实验功能中配置任务对话 ASR API 地址", "ASR_NOT_CONFIGURED"
        )

    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise TaskAsrError(
            "任务对话 ASR API 地址必须是有效的 HTTP(S) 地址", "ASR_BAD_CONFIG"
        )

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
    except (binascii.Error, ValueError) as exc:
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
