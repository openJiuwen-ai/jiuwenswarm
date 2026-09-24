# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-independent Session input intent (no SDK or channel dependency)."""

from collections.abc import Mapping
from enum import Enum
from typing import Any


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


def validate_session_input(params: Mapping[str, Any]) -> None:
    if "expected_execution_id" in params:
        target = params["expected_execution_id"]
        if not isinstance(target, str) or not target.strip():
            raise ValueError("expected_execution_id must be a non-empty string")
        if resolve_session_input_mode(params) is not SessionInputMode.STEER:
            raise ValueError("expected_execution_id requires steer mode")
    query = params.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("session input requires non-empty text")
    # The locked SDK's active steering queue carries text only. Never
    # acknowledge an attachment which that queue would silently discard.
    if resolve_session_input_mode(params) is SessionInputMode.STEER:
        for key in ("images", "image_files", "files", "attachments", "audio_files", "video_files"):
            if params.get(key):
                raise ValueError("steering supports text only; send attachments as a queued task")
