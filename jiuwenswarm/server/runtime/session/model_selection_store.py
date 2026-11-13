# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""兼容别名：会话级模型选择存储已下沉 ``jiuwenswarm.common.model_selection_store``。"""

<<<<<<< HEAD
import json
from collections.abc import Mapping
from pathlib import Path
||||||| e487e9a30
import json
from pathlib import Path
=======
from jiuwenswarm.common.model_selection_store import (  # noqa: F401
    get_session_model_selection,
    set_session_model_selection,
)
>>>>>>> origin/develop

<<<<<<< HEAD
from jiuwenswarm.common.model_errors import MODEL_GROUP_INVALID, ModelSelectionError
from jiuwenswarm.common.model_selection import ModelSelection
from jiuwenswarm.common.protocol_ids import is_valid_session_id
from jiuwenswarm.common.utils import get_agent_sessions_dir

SESSION_MODEL_INFO_KEY = "session_model_info"


def _metadata_path(session_id: str) -> Path:
    session_id = str(session_id or "").strip()
    if not is_valid_session_id(session_id):
        raise ValueError("invalid session_id")
    root = get_agent_sessions_dir().resolve()
    path = (root / session_id / "metadata.json").resolve()
    if root not in path.parents:
        raise ValueError("session_id escapes session root")
    return path


def _read_model_metadata(session_id: str) -> dict[str, object] | None:
    path = _metadata_path(session_id)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("session metadata must be an object")
        return raw
    except (OSError, ValueError) as exc:
        raise ModelSelectionError(MODEL_GROUP_INVALID, "cannot read the session's model selection") from exc


def get_session_model_selection(session_id: str) -> ModelSelection | None:
    metadata = _read_model_metadata(session_id)
    raw = metadata.get("model_selection") if metadata is not None else None
    try:
        return ModelSelection.model_validate(raw) if isinstance(raw, dict) else None
    except ValueError as exc:
        raise ModelSelectionError(MODEL_GROUP_INVALID, "cannot read the session's model selection") from exc


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"session.model {field} must be a string or None")
    return value.strip() or None


def sanitize_model_info(value: object) -> dict[str, object]:
    """Copy only public model facts, rejecting an invalid host snapshot."""
    if not isinstance(value, Mapping):
        raise TypeError("session.model requires a model-info mapping")
    source = value.get("source")
    activated = value.get("activated")
    if source not in ("runtime", "session"):
        raise ValueError("session.model source must be runtime or session")
    if (
        (source == "runtime" and activated is not True)
        or (source == "session" and activated is not None)
    ):
        raise ValueError("session.model activation must agree with its fact source")
    raw_selection = value.get("selection")
    selection = None
    if raw_selection is not None:
        if not isinstance(raw_selection, Mapping):
            raise TypeError("session.model selection must be a mapping or None")
        selection = ModelSelection.model_validate({
            key: raw_selection[key] for key in ("type", "id", "route_id") if key in raw_selection
        }).model_dump(exclude_none=True)
    model_name = _optional_text(value.get("model_name"), "model_name")
    if source == "runtime" and (selection is not None or model_name is None):
        raise ValueError("runtime model facts require a name and must not mix a saved selection")
    return {
        "selection": selection,
        "model_name": model_name,
        "provider": _optional_text(value.get("provider"), "provider"),
        "source": source,
        "activated": activated,
    }


def capture_runtime_model_info(model: object) -> dict[str, object] | None:
    """Snapshot the already-bound primary model without creating a client."""
    name = getattr(getattr(model, "model_config", None), "model_name", None)
    if not isinstance(name, str) or not name.strip():
        return None
    from jiuwenswarm.common.kv_cache_affinity_config import model_provider

    return sanitize_model_info({
        "selection": None,
        "model_name": name,
        "provider": model_provider(model) or None,
        "source": "runtime",
        "activated": True,
    })


def get_session_model_info(session_id: str) -> dict[str, object] | None:
    """Read saved selection facts without activating or resolving a model.

    Saved metadata alone cannot establish runtime activation. The returned
    activation is therefore None, and stable IDs are never resolved by a
    global default or a model factory.
    """
    metadata = _read_model_metadata(session_id)
    if metadata is None:
        return None
    try:
        raw = metadata.get("model_selection")
        if raw is not None and not isinstance(raw, dict):
            raise ValueError("model_selection must be an object")
        selection = (
            ModelSelection.model_validate(raw).model_dump(exclude_none=True)
            if raw is not None else None
        )
        name = None if selection is not None else _optional_text(metadata.get("model"), "model_name")
        if selection is None and name is None:
            return None
        return sanitize_model_info({
            "selection": selection,
            "model_name": name,
            "provider": None,
            "source": "session",
            "activated": None,
        })
    except (TypeError, ValueError) as exc:
        raise ModelSelectionError(MODEL_GROUP_INVALID, "cannot read the session's model selection") from exc


def set_session_model_selection(session_id: str, selection: ModelSelection) -> None:
    from jiuwenswarm.server.runtime.session.session_metadata import update_session_model_selection

    _metadata_path(session_id)  # Validate before entering the shared writer.
    update_session_model_selection(session_id, selection.model_dump())
||||||| e487e9a30
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
    path = _metadata_path(session_id)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8")).get("model_selection")
        return ModelSelection.model_validate(raw) if isinstance(raw, dict) else None
    except (OSError, ValueError) as exc:
        raise ModelSelectionError(MODEL_GROUP_INVALID, "cannot read the session's model selection") from exc


def set_session_model_selection(session_id: str, selection: ModelSelection) -> None:
    from jiuwenswarm.server.runtime.session.session_metadata import update_session_model_selection

    _metadata_path(session_id)  # Validate before entering the shared writer.
    update_session_model_selection(session_id, selection.model_dump())
=======
__all__ = ["get_session_model_selection", "set_session_model_selection"]
>>>>>>> origin/develop
