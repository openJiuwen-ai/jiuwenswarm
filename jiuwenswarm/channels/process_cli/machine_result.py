# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reduce a one-shot Runtime stream without retaining its event history."""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from io import StringIO
from typing import Any

from jiuwenswarm.channels.process_cli.protocol.model import RuntimeErrorInfo
from jiuwenswarm.runtime.events import RuntimeEvent


_RUN_ERROR_EVENTS = frozenset({"chat.error", "runtime.error"})
_USAGE_COUNTERS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_tokens",
        "input_cost",
        "output_cost",
        "total_cost",
    }
)


def _text(payload: Mapping[str, Any]) -> str:
    for key in ("delta", "content", "text", "message", "answer"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _error_code(sources: tuple[Mapping[str, Any], ...]) -> str:
    for source in sources:
        for key in ("code", "error_code"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "RUNTIME_ERROR"


def _error_retryable(sources: tuple[Mapping[str, Any], ...]) -> bool:
    for source in sources:
        value = source.get("retryable")
        if isinstance(value, bool):
            return value
    return False


def _error_details(sources: tuple[Mapping[str, Any], ...]) -> Mapping[str, Any]:
    for source in sources:
        value = source.get("details")
        if isinstance(value, Mapping):
            return value
    return {}


def _error_message(candidates: tuple[object, ...]) -> str:
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value
    return "Runtime execution failed"


def _error_info(event: RuntimeEvent) -> RuntimeErrorInfo:
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    metadata = event.metadata if isinstance(event.metadata, Mapping) else {}
    raw_error = payload.get("error")
    nested = raw_error if isinstance(raw_error, Mapping) else {}
    sources = (nested, payload, metadata)
    code = _error_code(sources)
    message = _error_message(
        (
            nested.get("message"),
            raw_error,
            payload.get("message"),
            payload.get("content"),
            metadata.get("message"),
        )
    )
    retryable = _error_retryable(sources)
    details = _error_details(sources)
    try:
        return RuntimeErrorInfo(
            code=code, message=message, retryable=retryable, details=details
        )
    except (TypeError, ValueError):
        # Optional diagnostics must not mask the execution failure or cause
        # arbitrary Runtime objects to be serialized into the machine result.
        return RuntimeErrorInfo(code=code, message=message, retryable=retryable)


def _valid_counter(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return value >= 0


class RunSummary:
    """Keep only assistant text, a usage snapshot and the first run failure.

    Runtime completion flags are observations, not the command outcome. The
    caller must exhaust/close the stream and clean up Runtime before emitting
    its one terminal result. No event, transport metadata or tool output is
    retained here.
    """

    __slots__ = ("_deltas", "_final", "_usage", "_has_usage_summary", "_error")

    def __init__(self) -> None:
        self._deltas = StringIO()
        self._final = ""
        self._usage: dict[str, Any] = {}
        self._has_usage_summary = False
        self._error: RuntimeErrorInfo | None = None

    @property
    def output(self) -> str | None:
        """Join deltas and the final without dropping an unflushed tail.

        The existing adapter can repeat its whole answer in ``chat.final`` or
        flush only a final tail. Containment matches its run-answer assembly;
        the prefix case also handles a full final after partially sent deltas.
        """
        joined = self._deltas.getvalue()
        if self._final and self._final.startswith(joined):
            return self._final
        if self._final and self._final not in joined:
            return joined + self._final
        return joined or self._final or None

    @property
    def usage(self) -> dict[str, Any]:
        """Return a detached usage snapshot, never context occupancy."""
        return deepcopy(self._usage)

    @property
    def error(self) -> RuntimeErrorInfo | None:
        """Return the first run-level error even if later text looks successful."""
        return self._error

    def observe(self, event: RuntimeEvent) -> None:
        """Consume one event while preserving Runtime-owned payloads."""
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        event_type = event.event_type
        if event_type == "chat.delta":
            self._deltas.write(_text(payload))
        elif event_type == "chat.final":
            final = _text(payload)
            if final:
                self._final = final

        if event_type == "chat.usage_summary":
            usage = payload.get("usage")
            if isinstance(usage, Mapping):
                self._usage = deepcopy(dict(usage))
                self._has_usage_summary = True
        elif event_type == "chat.usage_metadata" and not self._has_usage_summary:
            metadata = payload.get("metadata")
            usage = (
                metadata.get("usage_metadata")
                if isinstance(metadata, Mapping)
                else None
            )
            if isinstance(usage, Mapping):
                for key in _USAGE_COUNTERS:
                    value = usage.get(key)
                    if _valid_counter(value):
                        self._usage[key] = self._usage.get(key, 0) + value

        if self._error is None and (not event.ok or event_type in _RUN_ERROR_EVENTS):
            self._error = _error_info(event)


__all__ = ["RunSummary"]
