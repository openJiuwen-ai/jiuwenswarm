"""Safe persistence for stable per-session model selections."""

from __future__ import annotations

import json
from pathlib import Path

from jiuwenswarm.common.model_errors import MODEL_GROUP_INVALID, ModelSelectionError
from jiuwenswarm.common.model_selection import ModelSelection
from jiuwenswarm.common.protocol_ids import is_valid_session_id
from jiuwenswarm.common.utils import get_agent_sessions_dir


def _metadata_path(session_id: str) -> Path:
    session_id = str(session_id or "").strip()
    if not is_valid_session_id(session_id):
        raise ValueError("invalid session_id")
    root = get_agent_sessions_dir().resolve()
    path = (root / session_id / "metadata.json").resolve()
    if root not in path.parents:
        raise ValueError("session_id escapes session root")
    return path


def get_session_model_selection(session_id: str) -> ModelSelection | None:
    from jiuwenswarm.server.runtime.session.lifecycle import resource_lock

    session_id = str(session_id or "").strip()
    path = _metadata_path(session_id)
    try:
        # Match the metadata writer's cross-process lock: Windows can deny an
        # unlocked read while the writer atomically replaces metadata.json.
        # Only the byte snapshot needs the lock, never validation/model setup.
        with resource_lock("session", session_id):
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
        metadata = json.loads(text)
        if not isinstance(metadata, dict):
            raise ValueError("session metadata must be a JSON object")
        raw = metadata.get("model_selection")
        return ModelSelection.model_validate(raw) if isinstance(raw, dict) else None
    except (OSError, ValueError) as exc:
        raise ModelSelectionError(MODEL_GROUP_INVALID, "cannot read the session's model selection") from exc


def set_session_model_selection(session_id: str, selection: ModelSelection) -> None:
    from jiuwenswarm.server.runtime.session.session_metadata import update_session_model_selection

    session_id = str(session_id or "").strip()
    _metadata_path(session_id)  # Validate before entering the shared writer.
    update_session_model_selection(session_id, selection.model_dump())
