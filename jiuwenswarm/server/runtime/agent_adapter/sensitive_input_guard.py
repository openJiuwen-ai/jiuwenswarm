# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Detect content-policy / sensitive-gate failures and roll back poisoned turn context.

When a mid-turn LLM call is rejected by a provider content filter after tool
results are already in the live context, the stream cleanup path used to
checkpoint those tool messages. The next scheduled/manual turn then re-sent
the poisoned tool tail. This module provides a surgical rollback: pop messages
added after a pre-request snapshot, then let the normal checkpoint persist the
cleaned state.

Recognition is vendor-agnostic: ModelArts.81011 is one fingerprint among
several (OpenAI/Azure content_filter, DashScope DataInspectionFailed, etc.).
"""

from __future__ import annotations

import re
from typing import Any

# Exact / near-exact error codes (matched case-insensitively as whole tokens).
_CONTENT_POLICY_ERROR_CODES = (
    "modelarts.81011",
    "content_filter",
    "responsibleaipolicyviolation",
    "datainspectionfailed",
    "data_inspection_failed",
)

# Phrase / regex fingerprints on raw error text. Keep phrases long enough to
# avoid false positives on generic 403/Forbidden or bare "sensitive".
_CONTENT_POLICY_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"modelarts\.81011",
        r"may contain sensitive information",
        r"input text may contain sensitive",
        r"output text may contain sensitive",
        r"\bcontent[_ ]?filter\b",
        r"responsible\s*ai\s*policy\s*violation",
        r"content management policy",
        r"response was filtered",
        r"data[_ ]?inspection[_ ]?failed",
        r"appears to violate.{0,40}usage policy",
        r"violate(?:s|d)? (?:our |the )?usage policy",
        r"触发敏感词",
        r"敏感信息",
        r"内容安全",
        r"不合规内容",
        r"涉及敏感",
    )
)


def is_content_policy_error(text: object) -> bool:
    """Return True when *text* looks like a provider content-policy / sensitive gate."""
    if text is None:
        return False
    normalized = str(text).strip().lower()
    if not normalized:
        return False
    if any(code in normalized for code in _CONTENT_POLICY_ERROR_CODES):
        return True
    return any(pattern.search(normalized) for pattern in _CONTENT_POLICY_TEXT_PATTERNS)


def is_modelarts_sensitive_error(text: object) -> bool:
    """Backward-compatible alias for :func:`is_content_policy_error`."""
    return is_content_policy_error(text)


def rollback_context_to_message_count(
    context: Any,
    *,
    before_count: int,
) -> int:
    """Pop messages added after *before_count*. Return number of messages removed.

    Raises RuntimeError when the live buffer is shorter than the snapshot
    (ownership cannot be proven) — callers should treat that as a soft failure
    and skip the poisoned checkpoint rather than inventing a repair.
    """
    if context is None:
        return 0
    if before_count < 0:
        raise ValueError("before_count must be >= 0")

    messages = list(context.get_messages() or [])
    current_count = len(messages)
    if current_count < before_count:
        raise RuntimeError(
            f"context shrank below snapshot: current={current_count} before={before_count}"
        )
    pop_n = current_count - before_count
    if pop_n <= 0:
        return 0
    context.pop_messages(pop_n, with_history=True)
    after = list(context.get_messages() or [])
    if len(after) != before_count:
        raise RuntimeError(
            f"context rollback cannot be proven: expected={before_count} actual={len(after)}"
        )
    return pop_n


__all__ = [
    "is_content_policy_error",
    "is_modelarts_sensitive_error",
    "rollback_context_to_message_count",
]
