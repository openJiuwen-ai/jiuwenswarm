# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Resolve Web chat video uploads for the SDK's local-file ingest API."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.server.runtime.attachments.upload_storage import safe_session_dirname

_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".mpeg", ".mpg"})


def _session_upload_root(session_id: str | None) -> Path | None:
    """Return the existing upload directory for this session, rejecting links."""
    if not session_id or safe_session_dirname(session_id) != session_id:
        return None
    try:
        root = get_agent_sessions_dir().resolve() / session_id / "uploads"
        if str(root).startswith(("//", "\\\\")):
            return None
        if root.resolve(strict=True) == root and root.is_dir():
            return root
    except (OSError, ValueError, RuntimeError):
        pass
    return None


def _is_uploaded_video(path: Path) -> bool:
    if path.suffix.lower() not in _VIDEO_SUFFIXES:
        return False
    try:
        info = path.lstat()
        return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and path.resolve(strict=True) == path
    except (OSError, ValueError, RuntimeError):
        return False


def session_video_upload_root(session_id: str | None) -> Path | None:
    """Skip adding a Memory import root for sessions with only non-video uploads."""
    root = _session_upload_root(session_id)
    if root is None:
        return None
    try:
        return root if any(_is_uploaded_video(path) for path in root.iterdir()) else None
    except OSError:
        return None


def video_upload_prompt_files(files: dict[str, Any], session_id: str | None) -> dict[str, Any]:
    """Derive video URIs from server-side files in the current session only."""
    # Never accept a caller's precomputed URI or routing hint.
    files = {key: value for key, value in files.items() if key != "video_memory_attachments"}
    documents = files.get("uploaded_documents")
    if not isinstance(documents, list):
        return files
    root = _session_upload_root(session_id)
    if root is None:
        return files

    # Small uploads return the configured path. The Gateway's chunked path
    # returns an absolute, resolved path. Accept only these trusted aliases.
    configured_root = get_agent_sessions_dir() / root.parent.name / "uploads"
    allowed_parents = {root, configured_root, configured_root.absolute()}
    videos = []
    for document in documents:
        raw_path = document.get("path") if isinstance(document, dict) else None
        if not isinstance(raw_path, str) or not raw_path:
            continue
        uploaded_path = Path(raw_path)
        if uploaded_path.parent not in allowed_parents:
            continue
        path = root / uploaded_path.name
        if not _is_uploaded_video(path):
            continue
        videos.append({"filename": path.name, "uri": path.as_uri()})
    if videos:
        files["video_memory_attachments"] = {
            "videos": videos,
            "usage": (
                "When the user asks to remember an attached video, call agent_memory_video_submit "
                "with its uri if the tool is available. A video attachment alone does not request storage."
            ),
        }
    return files
