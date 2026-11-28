# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Language-agnostic Skip/Cancel sentinels for ask_user answers.

Wire/protocol values are bare sentinels. Localized UI strings are display-only
and must not be newly written into custom_input / tool_result. Legacy zh/en
fixed phrases are still recognized for old clients and persisted history.
"""

from __future__ import annotations

from typing import Literal

SKIPPED = "[ASK_USER_SKIPPED]"
CANCELLED = "[ASK_USER_CANCELLED]"

Disposition = Literal["skipped", "cancelled"]

LEGACY_SKIPPED: frozenset[str] = frozenset(
    {
        "用户已跳过此题，未选择任何选项。",
        "Skipped. No option selected.",
    }
)
LEGACY_CANCELLED: frozenset[str] = frozenset(
    {
        "用户已取消本次问答，未作答。",
        "Cancelled. No answer provided.",
    }
)
LEGACY_MARKERS: frozenset[str] = LEGACY_SKIPPED | LEGACY_CANCELLED

__all__ = [
    "SKIPPED",
    "CANCELLED",
    "Disposition",
    "LEGACY_SKIPPED",
    "LEGACY_CANCELLED",
    "LEGACY_MARKERS",
    "normalize_disposition_text",
    "is_skipped_or_cancelled",
]


def normalize_disposition_text(
    *,
    disposition: str | None = None,
    custom_input: str | None = None,
) -> str | None:
    """Map disposition / legacy phrase to a bare sentinel, else None.

    Returns None when the input is ordinary answer text (caller keeps original).
    """
    kind = (disposition or "").strip().lower()
    if kind == "skipped":
        return SKIPPED
    if kind == "cancelled":
        return CANCELLED

    text = (custom_input or "").strip()
    if not text:
        return None
    if text in {SKIPPED, *LEGACY_SKIPPED}:
        return SKIPPED
    if text in {CANCELLED, *LEGACY_CANCELLED}:
        return CANCELLED
    return None


def is_skipped_or_cancelled(text: str | None) -> bool:
    """True when text is a bare sentinel or a legacy skip/cancel phrase."""
    value = (text or "").strip()
    if not value:
        return False
    if value in {SKIPPED, CANCELLED}:
        return True
    if value in LEGACY_MARKERS:
        return True
    # DesignRail matches markers as substrings inside "question: value" lines.
    if SKIPPED in value or CANCELLED in value:
        return True
    for marker in LEGACY_MARKERS:
        if marker in value:
            return True
    return False
