# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for normalizing browser document attachments.

Documents are not persisted or parsed: the client supplies a local absolute
path, which is validated against the extension blacklist and returned for the
main chat as an ``@path`` reference.
"""

from __future__ import annotations

import base64
import binascii
import logging
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

logger = logging.getLogger(__name__)

# Base64 persist is used only for browser / remote-web uploads; cap it so a
# single payload cannot exhaust memory. Large documents are handled by the
# Gateway-side ``_pre_persist_large_documents`` (HTTP/local persist) instead.
_MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
_MAX_DOCUMENT_COUNT = 8

# Executable / script / package types rejected by document upload.
FORBIDDEN_DOCUMENT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".exe",
        ".dll",
        ".msi",
        ".scr",
        ".bat",
        ".cmd",
        ".ps1",
        ".vbs",
        ".wsf",
        ".hta",
        ".jar",
        ".lnk",
        ".bin",
        ".so",
        ".dylib",
        ".app",
        ".dmg",
        ".pkg",
        ".command",
        ".scpt",
        ".scptd",
        ".workflow",
        ".xpc",
        ".bundle",
        ".framework",
        ".kext",
        ".prefpane",
        ".saver",
        ".component",
    }
)


def forbidden_formats() -> list[str]:
    """Return sorted list of forbidden upload extensions."""
    return sorted(FORBIDDEN_DOCUMENT_EXTENSIONS)


def _document_suffix(filename: str | None) -> str:
    name = Path(str(filename or "")).name
    return Path(name).suffix.lower()


def is_forbidden_document(*, filename: str | None = None, suffix: str | None = None) -> bool:
    """Return True when the file extension is on the upload blacklist."""
    ext = (suffix or _document_suffix(filename) or "").lower()
    if not ext.startswith(".") and ext:
        ext = f".{ext}"
    return ext in FORBIDDEN_DOCUMENT_EXTENSIONS


def is_supported_document(*, filename: str | None = None) -> bool:
    """Return True when the filename is allowed for document upload (not blacklisted)."""
    name = Path(str(filename or "")).name.strip()
    if not name or name in {".", ".."}:
        return False
    return not is_forbidden_document(filename=name)


def persist_and_parse_documents(params: dict[str, Any], session_id: str | None = None) -> dict[str, Any]:
    """Validate document items; persist browser base64 payloads when supplied.

    纯同步校验（路径黑名单/存在性/大小），并支持浏览器 base64 上传：当条目带
    ``base64Data``/``base64_data`` 时解码落盘到会话 uploads 目录；当条目已由
    Gateway 侧落盘（``_persisted``）时直接透传落盘记录。其余情况要求提供已有
    本地 ``path``。由调用方（WorkspaceFileAdapter）放入线程池执行，避免阻塞
    事件循环。

    Accepts either ``params["documents"]`` or ``params["media_items"]`` entries with
    ``type == "document"``. Each item must provide an existing local ``path``
    (absolute or expandable) plus filename/mime metadata.

    Mutates and returns ``params`` with:
    - ``media_items``: document records pointing at the original local path
    - ``files.uploaded_documents``: lightweight path metadata for chat.send
    """
    raw_items = _collect_document_items(params)
    if not raw_items:
        params.pop("documents", None)
        return params

    stored: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for index, item in enumerate(raw_items[:_MAX_DOCUMENT_COUNT]):
        try:
            stored_item = _resolve_document_item(item, index=index, session_id=session_id)
            if stored_item:
                stored.append(stored_item)
        except (ValueError, OSError) as exc:
            # FileNotFoundError is an OSError subclass — do not list both (G.ERR.09).
            logger.warning("[document.persist] rejected item %s: %s", index, exc)
            errors.append(
                {
                    "index": index,
                    "filename": str(item.get("filename") or item.get("name") or ""),
                    "error": str(exc),
                }
            )
        except Exception as exc:
            logger.exception("[document.persist] failed for item %s: %s", index, exc)
            errors.append(
                {
                    "index": index,
                    "filename": str(item.get("filename") or item.get("name") or ""),
                    "error": str(exc),
                }
            )

    if stored:
        # Keep previously persisted images if caller mixed them in; only replace documents.
        existing_media = params.get("media_items")
        kept_images: list[dict[str, Any]] = []
        if isinstance(existing_media, list):
            for entry in existing_media:
                if isinstance(entry, dict) and entry.get("type") == "image" and entry.get("path"):
                    kept_images.append(entry)
        params["media_items"] = kept_images + stored
        files = params.get("files")
        if not isinstance(files, dict):
            files = {}
        files["uploaded_documents"] = [
            {
                "filename": item.get("filename"),
                "path": item.get("path"),
                "original_path": item.get("original_path") or item.get("path"),
                "mime_type": item.get("mime_type"),
                "size_bytes": item.get("size_bytes"),
            }
            for item in stored
        ]
        params["files"] = files
    else:
        params.pop("documents", None)

    if errors:
        params["document_errors"] = errors
    params["forbidden_formats"] = forbidden_formats()
    return params


def _collect_document_items(params: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    documents = params.get("documents")
    if isinstance(documents, list):
        for item in documents:
            if isinstance(item, dict):
                items.append(item)

    media_items = params.get("media_items")
    if isinstance(media_items, list):
        for item in media_items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "document" or (
                item_type is None
                and is_supported_document(
                    filename=str(item.get("filename") or item.get("name") or item.get("path") or ""),
                )
            ):
                items.append(item)
    return items


def _resolve_document_item(
    item: dict[str, Any], *, index: int, session_id: str | None = None
) -> dict[str, Any] | None:
    filename_hint = str(item.get("filename") or item.get("name") or "")
    mime_type = str(item.get("mimeType") or item.get("mime_type") or "").lower().strip()
    raw_path = str(
        item.get("path") or item.get("original_path") or item.get("originalPath") or ""
    ).strip()

    # Gateway 侧已经落盘的大文档（Phase 2 传输取舍：超 E2A 帧预算的 base64 不压
    # 内部链路）：直接透传落盘记录，不重复解码/写盘。路径必须存在，否则视为无效项。
    if item.get("_persisted"):
        if not raw_path:
            raise ValueError("Missing path for persisted document upload")
        path = Path(raw_path).expanduser().resolve(strict=False)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        filename = filename_hint or path.name or f"document-{index + 1}"
        if is_forbidden_document(filename=filename) or is_forbidden_document(filename=path.name):
            raise ValueError(
                f"Forbidden document type: filename={filename!r} path={path.name!r}. "
                f"Forbidden: {forbidden_formats()}"
            )
        return {
            "type": "document",
            "filename": Path(filename).name,
            "mime_type": mime_type or "application/octet-stream",
            "path": str(path),
            "original_path": str(path),
            "size_bytes": path.stat().st_size,
        }

    # 浏览器 base64 上传：解码落盘到会话 uploads 目录（与图片 _store_image_item 同机制）。
    raw_base64 = item.get("base64Data") or item.get("base64_data")
    if isinstance(raw_base64, str) and raw_base64.strip():
        data: bytes | None = None
        with suppress(binascii.Error):
            data = base64.b64decode(raw_base64, validate=True)
        if data:
            if len(data) > _MAX_DOCUMENT_BYTES:
                raise ValueError(
                    f"Document too large: {len(data)} bytes exceeds {_MAX_DOCUMENT_BYTES}"
                )
            filename = filename_hint or f"document-{index + 1}"
            if is_forbidden_document(filename=filename):
                raise ValueError(
                    f"Forbidden document type: filename={filename!r}. "
                    f"Forbidden: {forbidden_formats()}"
                )
            safe_session_id = safe_session_dirname(session_id)
            upload_dir = get_agent_sessions_dir() / safe_session_id / "uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            safe_name = safe_upload_filename(
                str(filename) or f"document-{index + 1}",
                fallback=f"document-{index + 1}",
            )
            path = atomic_write_unique(upload_dir / safe_name, data)
            return {
                "type": "document",
                "filename": path.name,
                "mime_type": mime_type or "application/octet-stream",
                "path": str(path),
                "original_path": str(path),
                "size_bytes": len(data),
            }

    if not raw_path:
        raise ValueError("Missing local path or base64 data for document upload")

    try:
        path = Path(raw_path).expanduser().resolve(strict=False)
    except OSError as exc:
        raise ValueError(f"Invalid path: {raw_path}") from exc

    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    filename = filename_hint or path.name or f"document-{index + 1}"
    if is_forbidden_document(filename=filename) or is_forbidden_document(filename=path.name):
        raise ValueError(
            f"Forbidden document type: filename={filename!r} path={path.name!r}. "
            f"Forbidden: {forbidden_formats()}"
        )

    data_size = path.stat().st_size

    return {
        "type": "document",
        "filename": Path(filename).name,
        "mime_type": mime_type or "application/octet-stream",
        "path": str(path),
        "original_path": str(path),
        "size_bytes": data_size,
    }
