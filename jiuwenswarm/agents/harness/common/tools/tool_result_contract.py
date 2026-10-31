"""Structured contract for tool results that must not be retried."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

NON_RETRYABLE_CONTRACT_ID = "jiuwenswarm.non_retryable.v1"


def build_non_retryable_tool_result(error: str, suggested_tool: str) -> str:
    """Build the canonical non-retryable tool result payload."""
    return json.dumps(
        {
            "contract": NON_RETRYABLE_CONTRACT_ID,
            "success": False,
            "error": error,
            "retryable": False,
            "suggested_tool": suggested_tool,
        },
        ensure_ascii=False,
    )


def is_non_retryable_tool_result(result: Any) -> bool:
    """Return True only for a structured non-retryable tool result.

    The check accepts structured payloads only, and requires the explicit
    ``contract`` marker emitted by :func:`build_non_retryable_tool_result`.
    The marker keeps a normal tool result (for example ``read_file`` returning
    a JSON file whose content happens to contain ``retryable: false``) from
    being misclassified as a tool failure contract.
    """
    for payload in _iter_structured_payloads(result):
        if _matches_contract(payload):
            return True
    return False


def _matches_contract(payload: dict[str, Any]) -> bool:
    if payload.get("contract") != NON_RETRYABLE_CONTRACT_ID:
        return False
    return payload.get("retryable") is False


def _iter_structured_payloads(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for attr in ("content", "output", "result", "data"):
            nested = value.get(attr)
            if nested is not None:
                yield from _iter_structured_payloads(nested)
        return
    if isinstance(value, str):
        parsed = _parse_json(value)
        if isinstance(parsed, dict):
            yield parsed
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_structured_payloads(item)
        return
    for attr in ("content", "output", "result", "data"):
        nested = getattr(value, attr, None)
        if nested is not None:
            yield from _iter_structured_payloads(nested)


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None
