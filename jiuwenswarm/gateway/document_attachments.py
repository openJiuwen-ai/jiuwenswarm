# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for normalizing browser document attachments.

Documents are not persisted or parsed: the client supplies a local absolute
path, which is validated against the extension blacklist and returned for the
main chat as an ``@path`` reference.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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


async def persist_and_parse_documents(params: dict[str, Any]) -> dict[str, Any]:
    """Validate document items by local path; do not write or parse content.

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

    for index, item in enumerate(raw_items):
        try:
            stored_item = _resolve_document_item(item, index=index)
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


def _resolve_document_item(item: dict[str, Any], *, index: int) -> dict[str, Any] | None:
    filename_hint = str(item.get("filename") or item.get("name") or "")
    mime_type = str(item.get("mimeType") or item.get("mime_type") or "").lower().strip()
    raw_path = str(
        item.get("path") or item.get("original_path") or item.get("originalPath") or ""
    ).strip()
    if not raw_path:
        raise ValueError(
            "Missing local path for document upload; base64 persist is no longer supported"
        )

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
