# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared container-file contracts for Gateway transport adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from gateway.common.attachment_safety import is_forbidden_document, safe_upload_filename

_AGENT_FILE_PATH_ROOT = "/home/agentos"


@dataclass(frozen=True)
class AgentFileDownloadChunk:
    """One chunk returned by a container file download."""

    data: bytes
    path: str
    offset: int
    chunk_size: int
    size: int
    content_type: str
    eof: bool


class AgentOSFileTransferError(RuntimeError):
    """Raised when AgentOS container file transfer cannot be completed."""

    def __init__(self, message: str, *, code: str = "INTERNAL_ERROR") -> None:
        super().__init__(message)
        self.code = str(code)


# Upload: relative path (+ optional dir prefix) → ``/home/agentos/<relative>``.
# Download / list / mkdir: absolute path under ``/home/agentos``.


def _normalize_upload_dir_prefix(dir_prefix: str) -> str:
    """Return a clean relative directory prefix, or empty string for default root."""
    text = str(dir_prefix or "").strip().replace("\\", "/")
    if not text or text == ".":
        return ""
    if text.startswith("/"):
        raise AgentOSFileTransferError(
            "upload dir must be relative (AgentOS prefixes /home/agentos)",
            code="BAD_REQUEST",
        )
    text = text.strip("/")
    if not text or text == ".":
        return ""
    posix = PurePosixPath(text)
    if ".." in posix.parts or posix.is_absolute():
        raise AgentOSFileTransferError("path must not contain '..'", code="BAD_REQUEST")
    return posix.as_posix()


def normalize_agent_file_upload_path(
    path: str,
    *,
    dir_prefix: str = "",
    user_id: str = "",
) -> str:
    """Normalize upload path: relative (+ optional ``dir_prefix``) → ``/home/agentos/...``.

    - ``dir_prefix`` empty: land under ``/home/agentos`` (e.g. ``a.pdf`` → ``/home/agentos/a.pdf``)
    - ``dir_prefix`` set: prefix directory (e.g. ``docs`` + ``a.pdf`` → ``/home/agentos/docs/a.pdf``)
    """
    del user_id  # reserved for future per-user upload roots
    text = str(path or "").strip().replace("\\", "/")
    if not text or text in {".", "/"}:
        raise AgentOSFileTransferError("path is required", code="BAD_REQUEST")
    if text.startswith("/"):
        raise AgentOSFileTransferError(
            "upload path must be relative (AgentOS prefixes /home/agentos)",
            code="BAD_REQUEST",
        )
    posix = PurePosixPath(text)
    if ".." in posix.parts or posix.is_absolute():
        raise AgentOSFileTransferError("path must not contain '..'", code="BAD_REQUEST")

    raw_name = posix.name
    if not raw_name or raw_name in {".", ".."}:
        raise AgentOSFileTransferError("path is required", code="BAD_REQUEST")
    safe_name = safe_upload_filename(raw_name, fallback="upload.bin")
    if is_forbidden_document(filename=safe_name):
        raise AgentOSFileTransferError("forbidden extension", code="BAD_REQUEST")

    path_parent = posix.parent.as_posix().strip(".")
    prefix = _normalize_upload_dir_prefix(dir_prefix)
    parts: list[str] = []
    if prefix:
        parts.append(prefix)
    if path_parent and path_parent != ".":
        parts.append(path_parent)
    parts.append(safe_name)
    return f"{_AGENT_FILE_PATH_ROOT}/{'/'.join(parts)}"


def normalize_agent_file_download_path(path: str) -> str:
    """Normalize download/list path: absolute and under ``/home/agentos``."""
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        raise AgentOSFileTransferError("path is required", code="BAD_REQUEST")
    if not text.startswith("/"):
        raise AgentOSFileTransferError("download path must be absolute", code="BAD_REQUEST")
    posix = PurePosixPath(text)
    if ".." in posix.parts:
        raise AgentOSFileTransferError("path must not contain '..'", code="BAD_REQUEST")
    root = _AGENT_FILE_PATH_ROOT
    if text != root and not text.startswith(f"{root}/"):
        raise AgentOSFileTransferError(
            f"path must be under {root}",
            code="BAD_REQUEST",
        )
    return text


