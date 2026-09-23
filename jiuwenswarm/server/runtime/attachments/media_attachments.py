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
# ``.jpeg`` / ``.jfif`` 与 ``image/jpeg`` 的规范后缀 ``.jpg`` 等价。已有这些后缀时
# 保留原文件名，避免 ``sample.jpeg`` 被再拼成 ``sample.jpeg.jpg``。
_IMAGE_FILENAME_SUFFIX_ALIASES = frozenset({".jpeg", ".jfif"})
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_IMAGE_COUNT = 8


def image_suffix_for_mime(mime_type: str) -> str | None:
    """返回受支持图片 MIME 类型对应的规范扩展名（含点，如 ``.jpg``），不支持返回 ``None``。"""
    return _SUPPORTED_IMAGE_MIME_TYPES.get(mime_type)


def supported_image_suffixes() -> frozenset[str]:
    """返回已有合法图片扩展名（含点，如 ``.jpg``、``.jpeg``）。

    含 MIME 规范后缀，以及 ``.jpeg``、``.jfif`` 这类等价别名。
    """
    return frozenset(_SUPPORTED_IMAGE_MIME_TYPES.values()) | _IMAGE_FILENAME_SUFFIX_ALIASES


def ensure_image_upload_filename(filename: str, canonical_suffix: str) -> str:
    """已有合法图片后缀时保留原名，否则在末尾补上规范后缀。"""
    if Path(filename).suffix.lower() in supported_image_suffixes():
        return filename
    return f"{filename}{canonical_suffix}"


def discard_session_upload(session_id: str | None, raw_path: str) -> dict[str, bool]:
    """删除当前会话 ``uploads`` 目录里的一个普通文件。

    只接受该目录的直接子文件。符号链接、目录、其他会话和用户原图路径一律拒绝。
    文件已经不存在时返回 ``{"deleted": False}``，重复删除是成功。
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("path is required")
    raw = Path(raw_path).expanduser()
    if not raw.is_absolute():
        raise ValueError("path must be absolute")
    if raw.name in {"", ".", ".."}:
        raise ValueError("invalid filename")

    upload_dir = (
        get_agent_sessions_dir() / safe_session_dirname(session_id) / "uploads"
    ).resolve(strict=False)
    try:
        parent = raw.parent.resolve(strict=False)
    except OSError as exc:
        raise ValueError("invalid path") from exc
    if not _same_directory(parent, upload_dir):
        raise ValueError("path is outside session uploads")

    target = upload_dir / raw.name
    if target.is_symlink():
        raise ValueError("refusing to delete symlink")
    if not target.exists():
        return {"deleted": False}
    if not target.is_file():
        raise ValueError("path is not a file")
    try:
        target.unlink()
    except FileNotFoundError:
        return {"deleted": False}
    return {"deleted": True}


def _same_directory(left: Path, right: Path) -> bool:
    if os.name == "nt":
        return os.path.normcase(str(left)) == os.path.normcase(str(right))
    return left == right


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

    filename = ensure_image_upload_filename(
        safe_upload_filename(
            str(item.get("filename") or f"image-{index + 1}{suffix}"),
            fallback=f"image-{index + 1}{suffix}",
        ),
        suffix,
    )
    path = atomic_write_unique(upload_dir / filename, data)

    return {
        "type": "image",
        "filename": path.name,
        "mime_type": mime_type,
        "path": str(path),
        "size_bytes": len(data),
    }
