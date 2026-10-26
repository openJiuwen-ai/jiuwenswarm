# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Calendar data source for the proactive recommendation engine.

Pulls upcoming calendar events from a user-configured MCP calendar server
(declared under ``mcp.servers`` in config.yaml with a name containing
"calendar").  The engine calls :func:`fetch_calendar_events` each tick to
enrich the situation report with real schedule context.

Design notes:
- Pure data source; never raises. Any failure (no calendar server, MCP
  server not registered, transport error, unexpected payload) logs at debug
  and returns ``[]`` so the tick continues with conversation-only context.
- Does not hardcode a specific MCP calendar server's tool schema: it probes
  the server's tools for a known event-listing tool name and parses the
  result loosely.
- Runs against ``Runner.resource_mgr`` (the same singleton the AgentServer
  initializes at startup), so MCP servers registered from config are
  available here without extra wiring.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# Candidate MCP tool names that list calendar events, ordered by preference.
# Different calendar MCP servers name this tool differently; we probe each.
_EVENT_TOOL_CANDIDATES: tuple[str, ...] = (
    "list_events",
    "get_events",
    "search_events",
    "list_calendars",   # some servers expose per-calendar listing
)

# Fields we try to extract from each event object (best-effort).
_EVENT_TITLE_KEYS = ("title", "summary", "subject", "name")
_EVENT_START_KEYS = ("start", "start_time", "dtStart", "startTime", "begin")
_EVENT_END_KEYS = ("end", "end_time", "dtEnd", "endTime", "finish")
_EVENT_LOCATION_KEYS = ("location", "place", "where")


def _find_calendar_server_entry() -> dict[str, Any] | None:
    """Return the first enabled ``mcp.servers`` entry whose name contains 'calendar'."""
    from jiuwenswarm.common.config import get_mcp_servers

    for entry in get_mcp_servers():
        name = str(entry.get("name", "")).lower()
        if "calendar" in name and bool(entry.get("enabled", True)):
            return entry
    return None


async def fetch_calendar_events(max_events: int = 10, lookahead_hours: int = 24) -> list[dict[str, Any]]:
    """Fetch upcoming calendar events from the configured MCP calendar server.

    Args:
        max_events: Cap on the number of events returned.
        lookahead_hours: Time window from now into the future to query.

    Returns:
        A list of normalized event dicts (keys: title, start, end, location).
        Empty list on any failure — never raises.
    """
    try:
        entry = _find_calendar_server_entry()
        if entry is None:
            logger.debug("[CalendarSource] no enabled calendar MCP server configured")
            return []
        server_name = str(entry.get("name", "")).strip()
        if not server_name:
            return []

        from openjiuwen.core.runner import Runner

        # Resolve the MCP tool: probe candidate names until one resolves.
        tool = await _resolve_event_tool(server_name)
        if tool is None:
            logger.debug(
                "[CalendarSource] no event-listing tool found on server '%s' "
                "(tried %s)", server_name, _EVENT_TOOL_CANDIDATES,
            )
            return []

        # Build the time window. Many calendar MCP servers accept ISO 8601.
        now = datetime.now(timezone.utc)
        start_iso = now.isoformat()
        end_iso = (now + timedelta(hours=lookahead_hours)).isoformat()

        out = await tool.invoke({"start": start_iso, "end": end_iso})
        events = _extract_events(out)
        if not events:
            logger.debug(
                "[CalendarSource] server '%s' returned no events for %s..%s",
                server_name, start_iso, end_iso,
            )
            return []

        normalized = [_normalize_event(ev) for ev in events]
        normalized = [e for e in normalized if e.get("title")]
        return normalized[:max_events]

    except Exception as exc:
        # Silent skip — the tick must not break because of a calendar hiccup.
        logger.debug("[CalendarSource] fetch failed (skipping): %s", exc, exc_info=True)
        return []


async def _resolve_event_tool(server_name: str) -> Any | None:
    """Probe candidate tool names on the named MCP server; return the first MCPTool found."""
    from openjiuwen.core.runner import Runner

    resource_mgr = Runner.resource_mgr
    for candidate in _EVENT_TOOL_CANDIDATES:
        try:
            tool = await resource_mgr.get_mcp_tool(name=candidate, server_name=server_name)
        except Exception as exc:
            logger.debug("[CalendarSource] get_mcp_tool('%s') raised: %s", candidate, exc)
            continue
        # get_mcp_tool with string args returns a list[Tool]; take the first.
        if isinstance(tool, list):
            tool = tool[0] if tool else None
        if tool is not None:
            return tool
    return None


def _extract_events(out: Any) -> list[Any]:
    """Loosely extract an events list from an MCP tool invoke() result.

    MCPTool.invoke returns ``{"result": <...>}`` where ``<...>`` may be a
    JSON string, a dict, or a list depending on the server. We normalize to
    a list of event dicts.
    """
    if not isinstance(out, dict):
        return []

    result = out.get("result", out)

    # result is often a JSON string (FastMCP returns content as text).
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            return []
        if isinstance(result, str):  # double-encoded
            try:
                result = json.loads(result)
            except (ValueError, TypeError):
                return []

    # Unwrap common envelopes: {"events": [...]} / {"items": [...]} / raw list.
    if isinstance(result, dict):
        for key in ("events", "items", "data", "result"):
            val = result.get(key)
            if isinstance(val, list):
                return val
        # Single event object?
        if any(k in result for k in _EVENT_TITLE_KEYS):
            return [result]
        return []
    if isinstance(result, list):
        return result
    return []


def _normalize_event(ev: Any) -> dict[str, Any]:
    """Pick the first present key from each candidate set into a flat dict."""
    if not isinstance(ev, dict):
        return {}
    return {
        "title": _first_present(ev, _EVENT_TITLE_KEYS) or "",
        "start": _first_present(ev, _EVENT_START_KEYS) or "",
        "end": _first_present(ev, _EVENT_END_KEYS) or "",
        "location": _first_present(ev, _EVENT_LOCATION_KEYS) or "",
    }


def _first_present(d: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for k in keys:
        val = d.get(k)
        if val is None:
            continue
        # datetime objects / nested {dateTime: ...} envelopes
        if isinstance(val, dict):
            for sub in ("dateTime", "date", "value"):
                if val.get(sub):
                    return str(val[sub])
        if isinstance(val, (list, tuple)) and val:
            return str(val[0])
        if val != "":
            return str(val)
    return None
