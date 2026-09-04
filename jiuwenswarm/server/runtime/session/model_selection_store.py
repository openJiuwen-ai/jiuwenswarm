"""Safe persistence for stable per-session model selections."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path

from jiuwenswarm.common.model_selection import ModelSelection
from jiuwenswarm.common.utils import get_agent_sessions_dir

_LOCK = threading.Lock()
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")


def _metadata_path(session_id: str) -> Path:
    session_id = str(session_id or "").strip()
    if not _SESSION_ID.fullmatch(session_id) or ".." in session_id:
        raise ValueError("invalid session_id")
    root = get_agent_sessions_dir().resolve()
    path = (root / session_id / "metadata.json").resolve()
    if root not in path.parents:
        raise ValueError("session_id escapes session root")
    return path


def get_session_model_selection(session_id: str) -> ModelSelection | None:
    path = _metadata_path(session_id)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8")).get("model_selection")
        return ModelSelection.model_validate(raw) if isinstance(raw, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def set_session_model_selection(session_id: str, selection: ModelSelection) -> None:
    path = _metadata_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        data = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("session metadata must be an object")
        data["model_selection"] = selection.model_dump()
        temporary = path.with_name(f"metadata.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

