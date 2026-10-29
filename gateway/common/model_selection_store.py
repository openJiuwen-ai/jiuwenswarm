# Gateway 侧副本，源文件 jiuwenswarm/common/model_selection_store.py；改动请同步两侧。
# 本副本仅保留 gateway 实际引用的符号；同步母仓时请勿整体覆盖。
"""Safe persistence for stable per-session model selections."""

from __future__ import annotations

import json
from pathlib import Path

from gateway.common.model_errors import MODEL_GROUP_INVALID, ModelSelectionError
from gateway.common.model_selection import ModelSelection
from gateway.common.paths import get_agent_sessions_dir
from gateway.common.protocol_ids import is_valid_session_id


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
    path = _metadata_path(session_id)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8")).get("model_selection")
        return ModelSelection.model_validate(raw) if isinstance(raw, dict) else None
    except (OSError, ValueError) as exc:
        raise ModelSelectionError(MODEL_GROUP_INVALID, "cannot read the session's model selection") from exc
