"""Deterministic contract for a committed rewrite's HTML follow-up."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any


PENDING_HTML_EXPORT_STATE_KEY = "deepresearch_pending_html_export"

_STATE_KEYS = {"schema_version", "report_path", "revision_id"}
_STATE_SCHEMA_VERSION = 1
_REVISION_RE = re.compile(r"\Arev_[A-Za-z0-9_-]{1,128}\Z")
_TERMINAL_PUNCTUATION_RE = re.compile(r"[。.!！?？]+\Z")
_HTML_FOLLOWUP_PHRASES = {
    "生成html",
    "请生成html",
    "生成最终美化版html",
    "请生成最终美化版html",
}
_SUCCESS_MESSAGE = "已生成美化后的 HTML。"
_FAILURE_MESSAGE = "HTML 生成失败，但 Markdown 改写版本仍然成功保留。"
_HTML_FOLLOWUP_MAX_CHARS = 64
_HTML_RESULT_MAX_BYTES = 64 * 1024
_HTML_RESULT_MAX_DEPTH = 64
_HTML_RESULT_MAX_NODES = 1024


@dataclass(frozen=True)
class RewriteHtmlTarget:
    """Trusted committed revision used by the HTML export tool."""

    report_path: str
    revision_id: str

    def to_state(self) -> dict[str, Any]:
        return {
            "schema_version": _STATE_SCHEMA_VERSION,
            "report_path": self.report_path,
            "revision_id": self.revision_id,
        }


@dataclass(frozen=True)
class RewriteHtmlFollowupResult:
    """Safe user-facing outcome from the HTML export tool."""

    status: str
    error_code: str | None
    message: str


def is_html_followup_request(query: object) -> bool:
    """Return true only for a small allowlist of explicit outer requests."""
    if not isinstance(query, str) or len(query) > _HTML_FOLLOWUP_MAX_CHARS:
        return False
    normalized = "".join(query.split()).casefold()
    normalized = _TERMINAL_PUNCTUATION_RE.sub("", normalized)
    return normalized in _HTML_FOLLOWUP_PHRASES


def _validated_target(report_path: object, revision_id: object) -> RewriteHtmlTarget | None:
    if not isinstance(report_path, str) or not report_path.strip():
        return None
    if (
        not isinstance(revision_id, str)
        or _REVISION_RE.fullmatch(revision_id) is None
    ):
        return None
    return RewriteHtmlTarget(report_path=report_path, revision_id=revision_id)


def target_from_commit_result(payload: object) -> RewriteHtmlTarget | None:
    """Extract a target only from a completed trusted commit result."""
    if not isinstance(payload, dict) or payload.get("status") != "completed":
        return None
    return _validated_target(payload.get("report_path"), payload.get("revision_id"))


def target_from_state(payload: object) -> RewriteHtmlTarget | None:
    """Validate an exact versioned target restored from checkpoint state."""
    if (
        not isinstance(payload, dict)
        or set(payload) != _STATE_KEYS
        or payload.get("schema_version") != _STATE_SCHEMA_VERSION
    ):
        return None
    return _validated_target(payload.get("report_path"), payload.get("revision_id"))


def decode_html_tool_result(payload: object) -> RewriteHtmlFollowupResult:
    """Convert a raw tool result into a fixed, non-leaking response."""
    try:
        if not isinstance(payload, str):
            raise ValueError("HTML tool result is not JSON text")
        if (
            len(payload) > _HTML_RESULT_MAX_BYTES
            or len(payload.encode("utf-8")) > _HTML_RESULT_MAX_BYTES
        ):
            raise ValueError("HTML tool result is too large")
        depth = 0
        in_string = False
        escaped = False
        for character in payload:
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character in "[{":
                depth += 1
                if depth > _HTML_RESULT_MAX_DEPTH:
                    raise ValueError("HTML tool result is too deep")
            elif character in "]}":
                depth -= 1
        decoded = json.loads(payload)
        stack = [decoded]
        nodes = 0
        while stack:
            current = stack.pop()
            nodes += 1
            if nodes > _HTML_RESULT_MAX_NODES:
                raise ValueError("HTML tool result has too many nodes")
            if isinstance(current, dict):
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
            elif isinstance(current, float) and not math.isfinite(current):
                raise ValueError("HTML tool result has a non-finite number")
    except (RecursionError, ValueError):
        decoded = None
    if (
        isinstance(decoded, dict)
        and decoded.get("status") == "completed"
        and decoded.get("html_delivered") is True
    ):
        return RewriteHtmlFollowupResult(
            status="completed",
            error_code=None,
            message=_SUCCESS_MESSAGE,
        )
    error_code = decoded.get("error_code") if isinstance(decoded, dict) else None
    if not isinstance(error_code, str) or not error_code:
        error_code = "INTERNAL_ERROR"
    return RewriteHtmlFollowupResult(
        status="error",
        error_code=error_code,
        message=_FAILURE_MESSAGE,
    )


__all__ = [
    "PENDING_HTML_EXPORT_STATE_KEY",
    "RewriteHtmlFollowupResult",
    "RewriteHtmlTarget",
    "decode_html_tool_result",
    "is_html_followup_request",
    "target_from_commit_result",
    "target_from_state",
]
