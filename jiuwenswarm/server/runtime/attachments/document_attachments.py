# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for normalizing browser document attachments.

Desktop deployments supply a local absolute ``path``, which is validated
against the extension blacklist and returned for the main chat as an
``@path`` reference. Browser / Docker (remote-server) deployments have no
local path on the server, so the client can instead send the document content
as ``base64Data``/``base64_data``; such items are decoded and persisted under
the session uploads directory, and a server-side path is returned.
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

#: 浏览器上传文档的大小上限（base64 膨胀前原始字节）。
_MAX_DOCUMENT_BYTES: int = 10 * 1024 * 1024
#: 单次文档上传的最大条数（与图片 _MAX_IMAGE_COUNT 对齐）。
_MAX_DOCUMENT_COUNT: int = 8

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


def persist_and_parse_documents(
    params: dict[str, Any],
    session_id: str | None = None,
) -> dict[str, Any]:
    """Validate document items by local path; do not write or parse content.

    纯同步校验（路径黑名单/存在性/大小），不落盘、不解析内容；由调用方
    （WorkspaceFileAdapter）放入线程池执行，避免阻塞事件循环。

    Accepts either ``params["documents"]`` or ``params["media_items"]`` entries with
    ``type == "document"``. Each item must provide either an existing local ``path``
    (absolute or expandable) or browser ``base64Data``/``base64_data`` content.
    Items with base64 content are persisted under the session uploads directory;
    local-path items are validated and referenced in place.

    Mutates and returns ``params`` with:
    - ``media_items``: document records pointing at a real server-side path
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
            stored_item = _resolve_document_item(
                item,
                index=index,
                session_id=session_id,
            )
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
        # Drop the browser base64 payloads from the original items so the
        # document.persist response does not echo full file content back over the
        # WebSocket (which would blow past the internal frame-size limit for
        # anything beyond a few MB). The persisted server-side path is enough.
        _strip_document_base64(params)
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


def _strip_document_base64(params: dict[str, Any]) -> None:
    """Remove base64 content from the original document items in-place.

    The caller echoes ``params`` back as the ``document.persist`` response; any
    retained ``base64_data`` would bloat the WebSocket frame past the internal
    size limit. Only the persisted server-side path is needed downstream.
    """
    documents = params.get("documents")
    if isinstance(documents, list):
        for item in documents:
            if not isinstance(item, dict):
                continue
            item.pop("base64Data", None)
            item.pop("base64_data", None)
    media_items = params.get("media_items")
    if isinstance(media_items, list):
        for item in media_items:
            if isinstance(item, dict) and item.get("type") == "document":
                item.pop("base64Data", None)
                item.pop("base64_data", None)


def _resolve_document_item(
    item: dict[str, Any],
    *,
    index: int,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    filename_hint = str(item.get("filename") or item.get("name") or "")
    mime_type = str(item.get("mimeType") or item.get("mime_type") or "").lower().strip()

    # Gateway 侧已经通过受认证 HTTP bridge 落盘到注入目录的大文档（超内部 WS
    # 帧限制的 base64 不压 E2A 链路）：直接透传落盘记录，不重复解码/写盘。
    # 路径必须存在，否则视为无效项丢弃。
    if item.get("_persisted"):
        persisted_path = item.get("path")
        if isinstance(persisted_path, str) and persisted_path.strip():
            try:
                exists = os.path.isfile(persisted_path)
                size = os.path.getsize(persisted_path) if exists else 0
            except OSError:
                exists = False
                size = 0
            if exists:
                return {
                    "type": "document",
                    "filename": Path(persisted_path).name,
                    "mime_type": mime_type or "application/octet-stream",
                    "path": persisted_path,
                    "original_path": persisted_path,
                    "size_bytes": size,
                }
        return None

    # Browser upload carries content as base64 (no server-side local path).
    raw_base64 = item.get("base64Data") or item.get("base64_data")
    if isinstance(raw_base64, str) and raw_base64.strip():
        return _store_document_item(
            item,
            base64_data=raw_base64,
            filename_hint=filename_hint,
            mime_type=mime_type,
            index=index,
            session_id=session_id,
        )

    raw_path = str(
        item.get("path") or item.get("original_path") or item.get("originalPath") or ""
    ).strip()
    if not raw_path:
        raise ValueError("Missing local path or base64 content for document upload")

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


def _store_document_item(
    item: dict[str, Any],
    *,
    base64_data: str,
    filename_hint: str,
    mime_type: str,
    index: int,
    session_id: str | None,
) -> dict[str, Any] | None:
    """Decode a browser base64 document and persist it under the session uploads dir.

    Mirrors ``media_attachments._store_image_item``: validates extension/size,
    writes atomically to ``agent/sessions/<sid>/uploads``, and returns a
    server-side path the downstream chat can reference.
    """
    data: bytes | None = None
    with suppress(binascii.Error):
        data = base64.b64decode(base64_data, validate=True)
    if not data:
        raise ValueError(f"Invalid or empty base64 content for document {filename_hint!r}")
    if len(data) > _MAX_DOCUMENT_BYTES:
        raise ValueError(
            f"Document too large: {len(data)} bytes exceeds {_MAX_DOCUMENT_BYTES} bytes"
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

    safe_name = safe_upload_filename(filename, fallback=f"document-{index + 1}")
    path = atomic_write_unique(upload_dir / safe_name, data)

    return {
        "type": "document",
        "filename": path.name,
        "mime_type": mime_type or "application/octet-stream",
        "path": str(path),
        "original_path": str(path),
        "size_bytes": len(data),
    }
