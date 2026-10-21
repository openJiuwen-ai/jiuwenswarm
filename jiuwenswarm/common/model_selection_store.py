"""Safe persistence for stable per-session model selections."""

from __future__ import annotations

import json
import time
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
    from jiuwenswarm.common.external_providers import ExternalProviderUnregistered, provider

    session_id = str(session_id or "").strip()
    path = _metadata_path(session_id)
    try:
        try:
            reader = getattr(provider("session_metadata"), "_read_model_selection_snapshot", None)
        except ExternalProviderUnregistered:
            reader = None
        if callable(reader):
            text = reader(session_id)
        else:
            # Standalone common users may have no server provider installed.
            # A transient Windows replacement must still not become a missing selection.
            for attempt in range(100):
                try:
                    text = path.read_text(encoding="utf-8")
                    break
                except FileNotFoundError:
                    return None
                except PermissionError:
                    if attempt == 99:
                        raise
                    time.sleep(0.01)
        if text is None:
            return None
        metadata = json.loads(text)
        if not isinstance(metadata, dict):
            raise ValueError("session metadata must be a JSON object")
        raw = metadata.get("model_selection")
        return ModelSelection.model_validate(raw) if isinstance(raw, dict) else None
    except (OSError, ValueError) as exc:
        raise ModelSelectionError(MODEL_GROUP_INVALID, "cannot read the session's model selection") from exc


def set_session_model_selection(session_id: str, selection: ModelSelection) -> None:
    from jiuwenswarm.common.external_providers import provider

    session_id = str(session_id or "").strip()
    _metadata_path(session_id)  # Validate before entering the shared writer.
    provider("session_metadata").update_session_model_selection(
        session_id, selection.model_dump()
    )
