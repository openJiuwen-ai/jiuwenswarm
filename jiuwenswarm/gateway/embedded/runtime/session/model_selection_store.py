"""Safe persistence for stable per-session model selections."""

from __future__ import annotations

import json
import time
from pathlib import Path

from jiuwenswarm.common.model_errors import MODEL_GROUP_INVALID, ModelSelectionError
from jiuwenswarm.common.model_selection import ModelSelection
from jiuwenswarm.common.protocol_ids import is_valid_session_id
from jiuwenswarm.common.utils import get_agent_sessions_dir

# The metadata writer publishes via tmp+rename and retries Windows sharing
# violations up to this budget; a reader landing in that window gets Errno 13
# on a healthy file.
_SHARING_RETRY_ATTEMPTS = 100


def _metadata_path(session_id: str) -> Path:
    session_id = str(session_id or "").strip()
    if not is_valid_session_id(session_id):
        raise ValueError("invalid session_id")
    root = get_agent_sessions_dir().resolve()
    path = (root / session_id / "metadata.json").resolve()
    if root not in path.parents:
        raise ValueError("session_id escapes session root")
    return path


def _read_text_ride_out_sharing(path: Path) -> str:
    attempt = 0
    while True:
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            attempt += 1
            if attempt >= _SHARING_RETRY_ATTEMPTS:
                raise
            time.sleep(min(0.001 * attempt, 0.01))


def get_session_model_selection(session_id: str) -> ModelSelection | None:
    path = _metadata_path(session_id)
    if not path.is_file():
        return None
    try:
        raw = json.loads(_read_text_ride_out_sharing(path)).get("model_selection")
        return ModelSelection.model_validate(raw) if isinstance(raw, dict) else None
    except (OSError, ValueError) as exc:
        raise ModelSelectionError(MODEL_GROUP_INVALID, "cannot read the session's model selection") from exc


def set_session_model_selection(session_id: str, selection: ModelSelection) -> None:
    from jiuwenswarm.gateway.embedded.runtime.session.session_metadata import update_session_model_selection

    _metadata_path(session_id)  # Validate before entering the shared writer.
    update_session_model_selection(session_id, selection.model_dump())
