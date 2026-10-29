# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ASR used by the regular task chat composer (see common.asr_client)."""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from jiuwenswarm.common.asr_client import (
    AUDIO_SUFFIXES,
    AsrClient,
    AsrError,
    decrypt_api_key,
    normalize_ws_endpoint,
)


MAX_TASK_ASR_AUDIO_BYTES = 25 * 1024 * 1024


class TaskAsrError(RuntimeError):
    """A user-facing ASR failure with a stable RPC error code."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def task_asr_endpoint(api_base: str) -> str:
    """Resolve a base URL or full transcription URL to the OpenAI route, or a ws(s) URL."""

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
        return normalize_ws_endpoint(value)

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


def _task_asr_error(exc: AsrError) -> TaskAsrError:
    status = f" ({exc.status_code})" if exc.status_code else ""
    message = {
        "ASR_TIMEOUT": "任务对话 ASR 请求超时",
        "ASR_CONNECTION_ERROR": f"无法连接任务对话 ASR 服务: {exc.detail}",
        "ASR_INVALID_RESPONSE": "任务对话 ASR 返回了无效 JSON",
        "ASR_AUDIO_DECODE_FAILED": exc.detail,
    }.get(exc.code, f"任务对话 ASR 请求失败{status}: {exc.detail}")
    return TaskAsrError(message, exc.code)


async def transcribe_task_audio(
    params: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Transcribe one browser recording with the task-chat ASR configuration."""

    env = environ if environ is not None else os.environ
    endpoint = task_asr_endpoint(env.get("ASR_API_BASE", ""))
    api_key = decrypt_api_key(str(env.get("ASR_API_KEY", "") or "").strip())
    model = str(env.get("ASR_MODEL_NAME", "") or "").strip()
    if not model:
        raise TaskAsrError(
            "请先在设置-实验功能中配置任务对话 ASR 模型", "ASR_NOT_CONFIGURED"
        )

    mime_type = (
        str(params.get("mime_type") or "audio/webm").split(";", 1)[0].strip().lower()
    )
    extension = AUDIO_SUFFIXES.get(mime_type)
    if extension is None:
        raise TaskAsrError(f"不支持的录音格式: {mime_type}", "ASR_UNSUPPORTED_FORMAT")
    audio = _decode_audio(params.get("audio_base64"))

    try:
        text = await AsrClient(
            endpoint,
            api_key,
            model,
            timeout=httpx.Timeout(120.0, connect=15.0),  # 来源: 沿用本文件原 httpx.AsyncClient 超时
            max_retries=0,  # 来源: 沿用本文件原 httpx 直连实现（不重试）
            http_client=client,
        ).transcribe(audio, mime_type, filename=f"task-recording{extension}")
    except AsrError as exc:
        raise _task_asr_error(exc) from exc
    if not text:
        raise TaskAsrError("任务对话 ASR 未返回转写文本", "ASR_EMPTY_TRANSCRIPT")
    return text
