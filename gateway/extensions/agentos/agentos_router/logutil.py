# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Stable AgentOS log lines for grep / future RuntimeLogFormatter columns."""

from __future__ import annotations

import logging
from typing import Any

_PRIMARY_KEYS = (
    "user_id",
    "session_id",
    "request_id",
    "trace_id",
    "sandbox_id",
    "agent_type",
    "instance",
    "channel",
    "method",
    "path",
    "status",
    "latency_ms",
    "created",
    "attempt",
    "error",
)
_REDACT_KEYS = frozenset(
    {
        "token",
        "authorization",
        "payload",
        "query",
        "password",
        "api_key",
        "ssh_private_key",
    }
)


def agentos_extra(*, session_id: str = "", sandbox_id: str = "") -> dict[str, str]:
    extra: dict[str, str] = {}
    sid = str(session_id or "").strip()
    sbx = str(sandbox_id or "").strip()
    if sid:
        extra["session_id"] = sid
    if sbx:
        extra["sandbox_id"] = sbx
    return extra


def _clean_field(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    return (
        text.replace("|", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("\t", " ")
        .strip()
    )


def format_agentos(event: str, **fields: Any) -> str:
    """Stable ``[AgentOS] event key=value`` line. Empty values are omitted."""
    parts = [f"[AgentOS] {_clean_field(event)}"]
    seen: set[str] = set()
    for key in _PRIMARY_KEYS:
        if key not in fields:
            continue
        seen.add(key)
        rendered = _render_field(key, fields[key])
        if rendered is not None:
            parts.append(rendered)
    for key, value in fields.items():
        if key in seen or key in _REDACT_KEYS:
            continue
        rendered = _render_field(key, value)
        if rendered is not None:
            parts.append(rendered)
    return " ".join(parts)


def _render_field(key: str, value: Any) -> str | None:
    if key in _REDACT_KEYS:
        return None
    if value is None:
        return None
    if isinstance(value, str) and not value:
        return None
    return f"{key}={_clean_field(value)}"


def log_agentos(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: Any,
) -> None:
    """Emit ``format_agentos`` and pre-seed ``extra`` session/sandbox ids."""
    extra = agentos_extra(
        session_id=str(fields.get("session_id") or ""),
        sandbox_id=str(fields.get("sandbox_id") or ""),
    )
    logger.log(level, format_agentos(event, **fields), extra=extra)
