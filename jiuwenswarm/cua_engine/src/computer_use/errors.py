"""Stable recovery categories, preserved from src/errors.ts."""
from __future__ import annotations

from typing import Any, Mapping


def format_failure(failure: Mapping[str, Any], method: str | None = None) -> str:
    prefix = f"{method} [{failure['target']}]: " if method else ""
    effect = {"none": "no action dispatched by this command", "partial":
              "input may have partially executed"}.get(failure["effect"], "the command may have changed UI state")
    return prefix + failure["detail"] + f"\nEffect: {effect}\nNext: {failure['next']}"


class ComputerUseError(Exception):
    """Operation failure with structured effects and recovery guidance."""

    def __init__(self, failure: Mapping[str, Any], *, method: str | None = None, cause: BaseException | None = None):
        super().__init__(format_failure(failure, method))
        self.name = "ComputerUseError"
        self.code = failure["code"]
        self.effect = failure["effect"]
        self.target = failure["target"]
        self.detail = failure["detail"]
        self.next = failure["next"]
        self._failure_context = None
        if cause is not None:
            self.__cause__ = cause


def public_error_code(code: str) -> str:
    if code in {
            "stale_target", "snapshot_expired", "ambiguous_target", "blocked_by_modal", "element_not_found",
            "observation_incomplete", "input_unavailable", "mode_unsupported", "desktop_unavailable", "invalid_request",
            "execution_failed", "execution_unknown"
    }:
        return code
    return {
        "mode_mismatch": "mode_unsupported", "session_mismatch": "stale_target", "version_mismatch": "invalid_request",
        "request_conflict": "invalid_request", "busy": "invalid_request", "request_expired": "execution_unknown",
        "in_progress": "execution_unknown"
    }.get(code, "execution_failed")
