"""Redact credential answers before durable chat-history storage."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

_MASK = "••••••"
_SENSITIVE_QUESTION_MARKERS = (
    "access token",
    "client secret",
    "api key",
    "password",
    "token",
    "secret",
    "密码",
    "口令",
    "令牌",
    "密钥",
    "秘钥",
)


def _is_sensitive_question(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.casefold()
    return any(marker in normalized for marker in _SENSITIVE_QUESTION_MARKERS)


def redact_sensitive_answers(value: Any) -> Any:
    """Return a history-safe copy without changing answers sent to the runtime."""
    if not isinstance(value, list):
        return deepcopy(value)
    redacted = deepcopy(value)
    for answer in redacted:
        if not isinstance(answer, dict) or not _is_sensitive_question(answer.get("question")):
            continue
        if isinstance(answer.get("selected_options"), list) and answer["selected_options"]:
            answer["selected_options"] = [_MASK]
        if isinstance(answer.get("custom_input"), str) and answer["custom_input"]:
            answer["custom_input"] = _MASK
    return redacted
