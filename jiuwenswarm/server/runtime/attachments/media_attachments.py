# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for normalizing browser-uploaded media attachments."""

from __future__ import annotations

import base64
import binascii
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.server.runtime.attachments.upload_storage import (
    atomic_write_unique,
    safe_session_dirname,
    safe_upload_filename,
)

_SUPPORTED_IMAGE_MIME_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_IMAGE_COUNT = 8


def image_suffix_for_mime(mime_type: str) -> str | None:
    """返回受支持图片 MIME 类型对应的扩展名（含点，如 ``.png``），不支持返回 ``None``。"""
    return _SUPPORTED_IMAGE_MIME_TYPES.get(mime_type)


def supported_image_suffixes() -> frozenset[str]:
    """返回全部受支持图片扩展名集合（含点，如 ``{".png", ".jpg"}``）。"""
    return frozenset(_SUPPORTED_IMAGE_MIME_TYPES.values())


def normalize_chat_media_attachments(params: dict[str, Any], session_id: str | None) -> None:
    """Validate browser media_items, persist images, and enrich the chat params.

    The frontend sends images as base64 for cross-platform browser compatibility.
    Images are persisted under the current session directory and returned as
    structured image file records. Downstream multimodal rails can load images
    from these paths without sending long base64 payloads through normal text
    context.
    """

    raw_items = params.get("media_items")
    if not isinstance(raw_items, list) or not raw_items:
        return

    stored: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items[:_MAX_IMAGE_COUNT]):
        if not isinstance(item, dict):
            continue
        if item.get("type") != "image":
            continue
        stored_item = _store_image_item(item, session_id=session_id, index=index)
        if stored_item:
            stored.append(stored_item)

    if not stored:
        params.pop("media_items", None)
        return

    params["media_items"] = stored
    files = params.get("files")
    if not isinstance(files, dict):
        files = {}
    files["uploaded_images"] = [
        {
            "filename": item.get("filename"),
            "path": item.get("path"),
            "mime_type": item.get("mime_type"),
            "size_bytes": item.get("size_bytes"),
        }
        for item in stored
    ]
    params["files"] = files


def _store_image_item(item: dict[str, Any], *, session_id: str | None, index: int) -> dict[str, Any] | None:
    mime_type = str(item.get("mimeType") or item.get("mime_type") or "").lower().strip()
    suffix = _SUPPORTED_IMAGE_MIME_TYPES.get(mime_type)
    if suffix is None:
        return None

    # Gateway 侧已经通过受认证 HTTP bridge 落盘到注入目录的大图（Phase 2 传输
    # 取舍：超内部 WS 帧限制的 base64 不压 E2A 链路）：直接透传落盘记录，不重复
    # 解码/写盘。路径必须存在，否则视为无效项丢弃。
    if item.get("_persisted"):
        path = item.get("path")
        if isinstance(path, str) and path.strip():
            try:
                exists = os.path.isfile(path)
                size = os.path.getsize(path) if exists else 0
            except OSError:
                exists = False
                size = 0
            if exists:
                return {
                    "type": "image",
                    "filename": Path(path).name,
                    "mime_type": mime_type,
                    "path": path,
                    "size_bytes": size,
                }
        return None

    raw_base64 = item.get("base64Data") or item.get("base64_data")
    if not isinstance(raw_base64, str) or not raw_base64.strip():
        return None
    data: bytes | None = None
    with suppress(binascii.Error):
        # 剥离 data:...;base64, 前缀（react-dropzone/readAsDataURL 等会带）。
        payload = raw_base64.split(",", 1)[-1] if raw_base64.startswith("data:") else raw_base64
        data = base64.b64decode(payload, validate=True)
    if not data or len(data) > _MAX_IMAGE_BYTES:
        return None

    safe_session_id = safe_session_dirname(session_id)
    upload_dir = get_agent_sessions_dir() / safe_session_id / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    filename = safe_upload_filename(
        str(item.get("filename") or f"image-{index + 1}{suffix}"),
        fallback=f"image-{index + 1}{suffix}",
    )
    if Path(filename).suffix.lower() not in set(_SUPPORTED_IMAGE_MIME_TYPES.values()):
        filename = f"{filename}{suffix}"
    path = atomic_write_unique(upload_dir / filename, data)

    return {
        "type": "image",
        "filename": path.name,
        "mime_type": mime_type,
        "path": str(path),
        "size_bytes": len(data),
    }
