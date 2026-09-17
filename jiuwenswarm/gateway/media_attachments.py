# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for normalizing browser-uploaded media attachments."""

from __future__ import annotations

import base64
import binascii
from contextlib import suppress
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.gateway.upload_storage import (
    safe_session_dirname,
    safe_upload_filename,
    unique_upload_path,
)

_SUPPORTED_IMAGE_MIME_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_IMAGE_COUNT = 8


def normalize_chat_media_attachments(params: dict[str, Any], session_id: str | None) -> None:
    """Validate browser media_items, persist images, and enrich the chat params.

    The frontend sends images as base64 for cross-platform browser compatibility.
    The gateway stores images under the current session and returns structured
    image file records. Downstream multimodal rails can load images from these
    paths without sending long base64 payloads through normal text context.
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

    raw_base64 = item.get("base64Data") or item.get("base64_data")
    if not isinstance(raw_base64, str) or not raw_base64.strip():
        return None
    data: bytes | None = None
    with suppress(binascii.Error):
        data = base64.b64decode(raw_base64, validate=True)
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
    path = unique_upload_path(upload_dir / filename)
    path.write_bytes(data)

    return {
        "type": "image",
        "filename": path.name,
        "mime_type": mime_type,
        "path": str(path),
        "size_bytes": len(data),
    }
