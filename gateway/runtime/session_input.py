# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-independent Session input intent (no SDK or channel dependency)."""

from collections.abc import Mapping
from enum import Enum
from typing import Any


MAX_STEER_IMAGE_COUNT = 8
_STEER_IMAGE_FIELDS = frozenset({"type", "path", "filename", "mime_type", "size_bytes"})
_STEER_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})


class SessionInputMode(str, Enum):
    STEER = "steer"
    FOLLOW_UP = "follow_up"


class SessionInputRejectedError(RuntimeError):
    """A known refusal before input reaches the SDK queue."""

    code = "SESSION_INPUT_REJECTED"


class SessionInputTargetError(SessionInputRejectedError):
    """A supplemental input must never become work for a different execution."""

    code = "SESSION_INPUT_TARGET_CHANGED"


class SessionInputQueueRequiredError(SessionInputRejectedError):
    """Cross-session input must wait for ordinary task admission; nothing was sent."""

    code = "SESSION_INPUT_QUEUE_REQUIRED"


def resolve_session_input_mode(params: Any) -> SessionInputMode | None:
    """Normalize the existing mode and its legacy alias in one place.

    Unknown values retain the existing ordinary-send behavior. Interaction
    answers are classified separately, before consulting this intent.
    """
    if not isinstance(params, Mapping):
        return None

    def normalize(raw: Any) -> str:
        return raw.value if isinstance(raw, SessionInputMode) else str(raw).strip().lower()

    primary, alias = params.get("input_mode"), params.get("runtime_mode")
    if primary and alias and normalize(primary) != normalize(alias):
        raise ValueError("input_mode and runtime_mode must agree")
    raw = params.get("input_mode") or params.get("runtime_mode") or ""
    if isinstance(raw, SessionInputMode):
        return raw
    value = normalize(raw)
    try:
        return SessionInputMode(value)
    except ValueError:
        return None


def validate_steer_image_references(params: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Validate the reference-only wire shape without touching the filesystem."""
    for key in ("images", "image_files", "files", "attachments", "audio_files", "video_files"):
        if params.get(key):
            raise ValueError("steering accepts uploaded image references only; send other attachments as a queued task")
    raw_items = params.get("media_items")
    if raw_items is None:
        return []
    if not isinstance(raw_items, list):
        raise ValueError("steer media_items must be a list of uploaded image references")
    if len(raw_items) > MAX_STEER_IMAGE_COUNT:
        raise ValueError(f"steering accepts at most {MAX_STEER_IMAGE_COUNT} images")
    for item in raw_items:
        if not isinstance(item, Mapping) or set(item) - _STEER_IMAGE_FIELDS:
            raise ValueError("steer images require uploaded file references without inline data")
        if item.get("type") != "image":
            raise ValueError("steering accepts uploaded images only")
        for key in ("path", "filename", "mime_type"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"steer image {key} must be a non-empty string")
        if item["mime_type"] not in _STEER_IMAGE_MIME_TYPES:
            raise ValueError("unsupported steer image MIME type")
    return raw_items


def validate_session_input(params: Mapping[str, Any]) -> None:
    mode = resolve_session_input_mode(params)
    if "expected_execution_id" in params:
        target = params["expected_execution_id"]
        if not isinstance(target, str) or not target.strip():
            raise ValueError("expected_execution_id must be a non-empty string")
        if mode is not SessionInputMode.STEER:
            raise ValueError("expected_execution_id requires steer mode")
    image_references = validate_steer_image_references(params) if mode is SessionInputMode.STEER else []
    query = params.get("query")
    if image_references and (query is None or isinstance(query, str)):
        return
    if not isinstance(query, str) or not query.strip():
        raise ValueError("session input requires non-empty text")
