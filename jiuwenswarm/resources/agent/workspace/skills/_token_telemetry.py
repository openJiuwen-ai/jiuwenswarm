"""Small, dependency-free token telemetry helpers shared by skill CLIs.

The Jiuwen provider tracker is authoritative when it is populated.  Native
agents, however, can execute beneath a harness that does not propagate its
usage object back to that tracker.  The harness still emits one compact,
machine-readable completion log line per provider response.  This module uses
that line only as a fallback and explicitly marks unobservable usage instead
of relabelling it as zero.
"""
from __future__ import annotations

import logging
import re
from typing import Any


_TOKEN_LINE = re.compile(r"\[LLM\]\s+<<<.*?tokens=\{input=(\d+),\s*output=(\d+)\}")
_KEYS = ("request_count", "prompt_tokens", "completion_tokens", "total_tokens")


def _empty_total() -> dict[str, int]:
    return {key: 0 for key in _KEYS}


class TokenUsageLogHandler(logging.Handler):
    """Collect compact provider token logs for just one CLI invocation."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self._records: list[dict[str, int]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            match = _TOKEN_LINE.search(record.getMessage())
        except Exception:
            return
        if match is None:
            return
        prompt, completion = (int(value) for value in match.groups())
        self._records.append({"prompt_tokens": prompt, "completion_tokens": completion})

    def summary(self) -> dict[str, Any]:
        prompt = sum(item["prompt_tokens"] for item in self._records)
        completion = sum(item["completion_tokens"] for item in self._records)
        return {
            "total": {
                "request_count": len(self._records),
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
            "records": list(self._records),
            "source": "skill_cli_provider_logs",
        }


def resolve_token_usage(
    tracker_usage: Any,
    logged_usage: dict[str, Any],
    *,
    llm_expected: bool,
) -> dict[str, Any]:
    """Prefer provider accounting, then logs; never turn missing data into 0."""
    tracker = tracker_usage if isinstance(tracker_usage, dict) else {}
    tracker_total = tracker.get("total") if isinstance(tracker.get("total"), dict) else {}
    if int(tracker_total.get("request_count") or 0) > 0:
        result = dict(tracker)
        records = tracker.get("records") if isinstance(tracker.get("records"), list) else []
        missing_provider_usage = [
            item for item in records
            if isinstance(item, dict) and item.get("source") == "provider_missing_usage"
        ]
        if missing_provider_usage:
            result["measurement_status"] = (
                "unavailable" if len(missing_provider_usage) == len(records) else "partial"
            )
            result["note"] = "One or more provider responses omitted token usage; totals may be incomplete."
        else:
            result["measurement_status"] = "reported"
        return result
    logged_total = logged_usage.get("total") if isinstance(logged_usage, dict) else {}
    if isinstance(logged_total, dict) and int(logged_total.get("request_count") or 0) > 0:
        result = dict(logged_usage)
        result["measurement_status"] = "reported"
        return result
    return {
        "total": _empty_total(),
        "by_stage": {},
        "by_operation": {},
        "records": [],
        "measurement_status": "unavailable" if llm_expected else "not_applicable",
        "source": "no_provider_usage_observed",
        "note": (
            "LLM calls were expected but the provider did not expose token usage."
            if llm_expected
            else "This invocation did not request an LLM (for example, dry-run or deterministic mode)."
        ),
    }
