# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Platform defaults for the seven public, event-backed session queries."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import math
from pathlib import Path
from typing import Any

from openjiuwen.core.events import SessionContext

from jiuwenswarm.extensions_v2.event_types.session_queries import SessionQueryError
from jiuwenswarm.server.runtime.session.model_selection_store import (
    SESSION_MODEL_INFO_KEY, get_session_model_info, sanitize_model_info,
)
from jiuwenswarm.server.runtime.session.query_messages import read_session_messages_view
from jiuwenswarm.server.runtime.session.query_repo import read_session_repo_view
from jiuwenswarm.server.runtime.session.session_statistics import (
    SESSION_USAGE_INFO_KEY, read_session_turns, read_session_usage, select_usage_breakdown,
)


def _path(value: object, capability: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{capability} requires a string path or None")
    if not value or not Path(value).is_absolute():
        raise ValueError(f"{capability} requires an absolute path")
    return value


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


async def _model(context: SessionContext) -> str | None:
    snapshot = context.metadata.get(SESSION_MODEL_INFO_KEY)
    info = sanitize_model_info(snapshot) if snapshot is not None else await asyncio.to_thread(
        get_session_model_info, context.session_id,
    )
    return None if info is None else info["model_name"]


async def _usage_snapshot(context: SessionContext) -> dict | None:
    if SESSION_USAGE_INFO_KEY in context.metadata:
        snapshot = context.metadata[SESSION_USAGE_INFO_KEY]
        if snapshot is not None and (
            not isinstance(snapshot, Mapping) or snapshot.get("session_id") != context.session_id
        ):
            raise ValueError("session.usage requires a matching host snapshot")
        return select_usage_breakdown(snapshot, True)
    return await asyncio.to_thread(read_session_usage, context.session_id, True)


def _summary_breakdown(raw: Mapping) -> dict[str, Any] | None:
    """Project observed category estimates; omit facts the host never collected."""
    parts = raw.get("parts")
    if not isinstance(parts, Mapping):
        return None
    categories = [
        {"name": name, "tokens": part["tokens"], "color": "promptBorder", "isDeferred": False, "kind": "used"}
        for name, part in parts.items()
        if isinstance(name, str) and isinstance(part, Mapping) and _finite(part.get("tokens"))
    ]
    if not categories:
        return None
    total = sum(category["tokens"] for category in categories)
    result: dict[str, Any] = {"categories": categories, "totalTokens": total}
    # No fabricated compaction settings, grid, per-tool or memory-file ledger.
    return result


async def _usage(arguments: Mapping[str, object], context: SessionContext) -> dict[str, Any]:
    if arguments.get("breakdown") == "full":
        raise SessionQueryError("FULL_BREAKDOWN_UNAVAILABLE")
    snapshot = await _usage_snapshot(context)
    output: dict[str, Any] = {"context": {}, "rateLimits": []}
    raw = snapshot.get("context") if snapshot is not None else None
    if not isinstance(raw, Mapping):
        return output
    window = raw.get("context_window")
    window = window if isinstance(window, Mapping) else {}
    if any(window[key] is not None and not _finite(window[key]) for key in ("input_tokens", "limit_tokens") if key in window):
        raise ValueError("session.usage requires valid observed token counts")
    public = output["context"]
    limit = window.get("limit_tokens")
    if _finite(limit) and limit > 0:
        public["window"] = limit
    measurement = raw.get("measurement")
    authoritative = isinstance(measurement, Mapping) and measurement.get("authoritative_total") is True
    tokens = window.get("input_tokens")
    if authoritative and _finite(tokens):
        public["tokens"] = tokens
        if "window" in public:
            public["percent"] = min(100, math.floor(tokens / public["window"] * 100 + 0.5))
    if arguments.get("breakdown") == "summary":
        breakdown = _summary_breakdown(raw)
        if breakdown is not None:
            public["breakdown"] = breakdown
    return output


async def read_session_query(event_type: str, arguments: Mapping[str, object], context: SessionContext) -> object:
    """Execute one platform query without reentering public Ops or EventBus."""
    if event_type == "session.cwd":
        return _path(context.cwd, event_type)
    if event_type == "session.root":
        return _path(context.metadata.get("project_root"), event_type)
    if event_type == "session.model":
        return await _model(context)
    if event_type == "session.repo":
        cwd = _path(context.cwd, event_type)
        return None if cwd is None else await asyncio.to_thread(read_session_repo_view, cwd)
    if event_type == "session.messages":
        return await asyncio.to_thread(read_session_messages_view, context.session_id)
    if event_type == "session.turns":
        return await asyncio.to_thread(read_session_turns, context.session_id)
    if event_type == "session.usage":
        return await _usage(arguments, context)
    raise SessionQueryError("UNKNOWN_QUERY")
