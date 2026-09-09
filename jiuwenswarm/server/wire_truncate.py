# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Wire-payload truncation for the AgentWebSocketServer.

Centralizes every byte-budget / shrink-and-collapse helper that prepares team
history records and swarmflow workflow snapshots for the WebSocket wire. Two
phases share one set of low-level tools:

* **History records** — ``_sanitize_history_record_for_wire`` /
  ``_select_history_record_page`` paginate a session's history under a byte
  budget, collapsing oversized records down to a metadata stub.
* **Workflow snapshots** — ``_build_workflow_list_payload`` /
  ``_build_workflow_detail_payload`` shape swarmflow runs for the
  ``command.workflows`` RPC, with a HITL carve-out that always preserves a
  ``waiting_for_human`` node's ``human_prompt``.

Everything is pure: given an input dict and a byte budget, return a wire-safe
dict. The only I/O is the caller's. Sized via ``_json_wire_size`` (UTF-8 bytes
of the JSON encoding) — never character count.
"""
from __future__ import annotations

import json
from typing import Any

# ---------------------------------------------------------------------------
# Wire byte budgets
# ---------------------------------------------------------------------------

_HISTORY_PAGE_SIZE = 50
_HISTORY_WIRE_STRING_LIMIT = 16 * 1024
_HISTORY_WIRE_METADATA_STRING_LIMIT = 256
_HISTORY_WIRE_LIST_LIMIT = 100
_HISTORY_WIRE_DEPTH_LIMIT = 8
_HISTORY_WIRE_RECORD_MAX_BYTES = 64 * 1024
_TEAM_HISTORY_DEFAULT_LIMIT = 500
_TEAM_HISTORY_MAX_LIMIT = 1000
_TEAM_HISTORY_DEFAULT_MAX_BYTES = 2 * 1024 * 1024
_TEAM_HISTORY_MIN_MAX_BYTES = 2048
_TEAM_HISTORY_MAX_MAX_BYTES = 6 * 1024 * 1024
_TEAM_HISTORY_FRAME_OVERHEAD_BYTES = 1024
_WORKFLOW_SNAPSHOT_MAX_BYTES = 6 * 1024 * 1024
_WORKFLOW_SNAPSHOT_FRAME_OVERHEAD_BYTES = 2048
_WORKFLOW_SNAPSHOT_MAX_WORKFLOWS = 1000
_WORKFLOW_LIST_SUMMARY_STRING_LIMIT = 256
_WORKFLOW_COLLAPSED_AGENT_TEXT_LIMIT = 512
_WORKFLOW_WAITING_HUMAN_PROMPT_MAX_BYTES = 512 * 1024

_TRUNCATE_SUFFIX = " [truncated]"

_HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES = frozenset(
    {
        "chat.final",
        # 错误轮留痕：chat.error 落盘记录（模型 401/连接失败等）重启后可恢复，
        # 否则错误轮只剩前端本地台账的「失败」折叠区，错误正文丢失
        "chat.error",
        "chat.tool_call",
        "chat.tool_result",
        "chat.usage_summary",
        "chat.file",
        "chat.html_card",
        "chat.reference",
        "team.message",
        "context.compact_boundary",
        "context.compact_summary",
        "context.rewind_summary",
    }
)

_HISTORY_COLLAPSE_KEEP_KEYS = {
    "id",
    "role",
    "request_id",
    "channel_id",
    "session_id",
    "timestamp",
    "event_type",
    "mode",
    "member_name",
    "member_id",
    "source_member",
    "name",
    "status",
    "goal_id",
    "is_goal_objective_message",
    "is_goal_completed_message",
    "evidence",
}

_WORKFLOW_SNAPSHOT_KEEP_KEYS = {
    "id",
    "name",
    "status",
    "agent_count",
    "completed_agent_count",
    "started_at",
    "completed_at",
    "duration_ms",
    "token_count",
    "estimated_token_count",
}

_WORKFLOW_LIST_SUMMARY_KEEP_KEYS = (
    "id",
    "name",
    "status",
    "agent_count",
    "completed_agent_count",
    "started_at",
    "completed_at",
    "duration_ms",
    "token_count",
    "estimated_token_count",
)


# ---------------------------------------------------------------------------
# Low-level sizing / truncation
# ---------------------------------------------------------------------------

def _json_wire_size(value: Any) -> int:
    """UTF-8 byte length of ``value``'s JSON wire encoding."""
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return len(str(value).encode("utf-8", errors="replace"))


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    """Coerce a request param to a clamped int (default on parse failure)."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _truncate_string_by_bytes(value: str, max_bytes: int) -> str:
    """Truncate ``value`` to at most ``max_bytes`` UTF-8 bytes.

    Appends ``" [truncated]"`` and decodes the byte slice with
    ``errors="ignore"`` so a split multi-byte character is dropped rather than
    producing invalid UTF-8 (which would break the frontend's JSON parse).
    """
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    budget = max(0, max_bytes - len(_TRUNCATE_SUFFIX.encode("utf-8")))
    return raw[:budget].decode("utf-8", errors="ignore") + _TRUNCATE_SUFFIX


def _compact_wire_metadata_value(value: Any) -> Any:
    """Compact a metadata scalar to a short wire-safe string."""
    if isinstance(value, str):
        return _truncate_string_by_bytes(value, _HISTORY_WIRE_METADATA_STRING_LIMIT)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _truncate_string_by_bytes(str(value), _HISTORY_WIRE_METADATA_STRING_LIMIT)


def _sanitize_history_wire_value(value: Any, *, depth: int = 0) -> Any:
    """Recursively bound a value for the wire: strings, lists, depth."""
    if depth > _HISTORY_WIRE_DEPTH_LIMIT:
        return "<truncated>"
    if isinstance(value, str):
        return _truncate_string_by_bytes(value, _HISTORY_WIRE_STRING_LIMIT)
    if isinstance(value, dict):
        return {
            str(key): _sanitize_history_wire_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_history_wire_value(item, depth=depth + 1)
            for item in value[:_HISTORY_WIRE_LIST_LIMIT]
        ]
    if isinstance(value, tuple):
        return [
            _sanitize_history_wire_value(item, depth=depth + 1)
            for item in value[:_HISTORY_WIRE_LIST_LIMIT]
        ]
    return value


# ---------------------------------------------------------------------------
# History record shaping
# ---------------------------------------------------------------------------

def _collapse_oversized_history_record(record: dict[str, Any]) -> dict[str, Any]:
    """Collapse a too-large history record to a metadata stub + short content."""
    collapsed = {
        key: _sanitize_history_wire_value(value)
        for key, value in record.items()
        if key in _HISTORY_COLLAPSE_KEEP_KEYS
    }
    content = record.get("content")
    if isinstance(content, str) and content.strip():
        collapsed["content"] = _truncate_string_by_bytes(content, 512)
    event = record.get("event")
    if isinstance(event, dict):
        collapsed["event"] = {
            key: _sanitize_history_wire_value(event.get(key))
            for key in ("type", "member_id", "task_id", "id", "status", "new_status", "team_id")
            if key in event
        }
    collapsed["truncated"] = True
    return collapsed


def _minimal_history_record_for_wire(record: dict[str, Any]) -> dict[str, Any]:
    """Smallest history record stub: metadata only, content replaced."""
    minimal = {
        key: _compact_wire_metadata_value(value)
        for key, value in record.items()
        if key in _HISTORY_COLLAPSE_KEEP_KEYS
    }
    minimal["content"] = "[truncated]"
    minimal["truncated"] = True
    return minimal


def _sanitize_history_record_for_wire(record: Any) -> dict[str, Any]:
    """Sanitize one history record, collapsing if it exceeds the per-record budget."""
    if not isinstance(record, dict):
        return {"content": _sanitize_history_wire_value(record), "truncated": True}
    sanitized = _sanitize_history_wire_value(record)
    if not isinstance(sanitized, dict):
        return {"content": str(sanitized), "truncated": True}
    if _json_wire_size(sanitized) <= _HISTORY_WIRE_RECORD_MAX_BYTES:
        return sanitized
    return _collapse_oversized_history_record(sanitized)


def _select_history_record_page(
    records: list[dict[str, Any]],
    *,
    cursor: int,
    limit: int,
    max_bytes: int,
    session_id: str,
) -> tuple[list[dict[str, Any]], int]:
    """Select a byte-bounded page of history records from ``cursor``.

    Shrinks records that alone exceed the budget (collapse → minimal → id-only)
    so the page still advances instead of stalling on one huge record.
    """
    total = len(records)
    if cursor >= total:
        return [], total

    budget = max(
        _TEAM_HISTORY_MIN_MAX_BYTES,
        max_bytes - _TEAM_HISTORY_FRAME_OVERHEAD_BYTES,
    )
    base_payload = {
        "records": [],
        "session_id": session_id,
        "cursor": cursor,
        "next_cursor": cursor,
        "has_more": cursor < total,
        "total": total,
    }
    used = _json_wire_size(base_payload)
    page: list[dict[str, Any]] = []
    next_cursor = cursor

    for idx in range(cursor, total):
        if len(page) >= limit:
            break
        record = records[idx]
        record_size = _json_wire_size(record) + 1
        if record_size > budget:
            record = _collapse_oversized_history_record(record)
            record_size = _json_wire_size(record) + 1
        if page and used + record_size > budget:
            break
        if not page and used + record_size > budget:
            record = _collapse_oversized_history_record(record)
            record_size = _json_wire_size(record) + 1
            if used + record_size > budget:
                record = _minimal_history_record_for_wire(record)
                record_size = _json_wire_size(record) + 1
                if used + record_size > budget:
                    record = {"id": _compact_wire_metadata_value(record.get("id")), "truncated": True}
                    record_size = _json_wire_size(record) + 1
        page.append(record)
        used += record_size
        next_cursor = idx + 1

    return page, next_cursor


# ---------------------------------------------------------------------------
# Workflow snapshot — HITL (waiting-for-human) helpers
# ---------------------------------------------------------------------------

def _is_waiting_human_agent(agent: dict[str, Any]) -> bool:
    return agent.get("status") == "waiting_for_human" and agent.get("kind") == "human"


def _extract_waiting_human_prompts(workflow: dict[str, Any]) -> dict[str, str]:
    """Pull every waiting-human node's prompt, keyed by agent id, before shrink."""
    prompts: dict[str, str] = {}
    phases = workflow.get("phases")
    if not isinstance(phases, list):
        return prompts
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        agents = phase.get("agents")
        if not isinstance(agents, list):
            continue
        for agent in agents:
            if not isinstance(agent, dict) or not _is_waiting_human_agent(agent):
                continue
            prompt = agent.get("human_prompt")
            agent_id = agent.get("id")
            has_prompt = isinstance(prompt, str) and bool(prompt.strip())
            has_agent_id = isinstance(agent_id, str) and bool(agent_id)
            if has_prompt and has_agent_id:
                prompts[agent_id] = prompt
    return prompts


def _restore_waiting_human_prompts(item: dict[str, Any], prompts: dict[str, str]) -> None:
    """Re-attach preserved human prompts onto a shrunk item's waiting nodes."""
    if not prompts:
        return
    phases = item.get("phases")
    if not isinstance(phases, list):
        return
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        agents = phase.get("agents")
        if not isinstance(agents, list):
            continue
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            agent_id = agent.get("id")
            if isinstance(agent_id, str) and agent_id in prompts:
                agent["human_prompt"] = prompts[agent_id]


def _workflow_agent_for_collapse(agent: dict[str, Any]) -> dict[str, Any]:
    """Collapse one agent: keep identity + short text fields, bigger human_prompt."""
    collapsed_agent: dict[str, Any] = {
        "id": agent.get("id", ""),
        "name": agent.get("name", ""),
        "status": agent.get("status", "running"),
        "kind": agent.get("kind", "agent"),
    }
    if agent.get("model"):
        collapsed_agent["model"] = agent["model"]
    if agent.get("correlation_id"):
        collapsed_agent["correlation_id"] = agent["correlation_id"]
    for time_key in ("started_at", "completed_at", "duration_ms"):
        if time_key in agent:
            collapsed_agent[time_key] = agent[time_key]

    if _is_waiting_human_agent(agent):
        prompt = agent.get("human_prompt")
        if isinstance(prompt, str) and prompt.strip():
            collapsed_agent["human_prompt"] = _truncate_string_by_bytes(
                prompt,
                _WORKFLOW_WAITING_HUMAN_PROMPT_MAX_BYTES,
            )
        return collapsed_agent

    for text_key in ("prompt", "outcome", "error", "human_prompt", "human_reply"):
        value = agent.get(text_key)
        if isinstance(value, str) and value.strip():
            collapsed_agent[text_key] = _truncate_string_by_bytes(
                value,
                _WORKFLOW_COLLAPSED_AGENT_TEXT_LIMIT,
            )
        elif value is not None:
            collapsed_agent[text_key] = _truncate_string_by_bytes(
                str(value),
                _WORKFLOW_COLLAPSED_AGENT_TEXT_LIMIT,
            )
    return collapsed_agent


# ---------------------------------------------------------------------------
# Workflow snapshot — single-item shrink ladder
# ---------------------------------------------------------------------------

def _collapse_oversized_workflow_snapshot_item(item: dict[str, Any]) -> dict[str, Any]:
    """Collapse a too-large workflow item: keep structure, truncate large text."""
    collapsed = {
        key: _sanitize_history_wire_value(value)
        for key, value in item.items()
        if key in _WORKFLOW_SNAPSHOT_KEEP_KEYS
    }
    for key in ("summary", "description", "error", "result"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            collapsed[key] = _truncate_string_by_bytes(value, 512)
        elif value is not None:
            collapsed[key] = _truncate_string_by_bytes(str(value), 512)

    phases = item.get("phases")
    if isinstance(phases, list):
        collapsed_phases = []
        for phase in phases:
            if not isinstance(phase, dict):
                continue
            collapsed_phase = {
                "id": phase.get("id", ""),
                "name": phase.get("name", ""),
                "status": phase.get("status", "running"),
                "agent_count": phase.get("agent_count", 0),
                "completed_agent_count": phase.get("completed_agent_count", 0),
            }
            agents = phase.get("agents")
            if isinstance(agents, list):
                collapsed_agents = []
                for agent in agents:
                    if not isinstance(agent, dict):
                        continue
                    collapsed_agents.append(_workflow_agent_for_collapse(agent))
                collapsed_phase["agents"] = collapsed_agents
            collapsed_phases.append(collapsed_phase)
        collapsed["phases"] = collapsed_phases

    logs = item.get("logs")
    if isinstance(logs, list) and logs:
        collapsed["logs"] = [
            _truncate_string_by_bytes(str(log), 512)
            for log in logs[-10:]
        ]

    collapsed["truncated"] = True
    return collapsed


def _minimal_workflow_snapshot_item_for_wire(item: dict[str, Any]) -> dict[str, Any]:
    """Bare workflow item: metadata only, summary replaced, no phases."""
    minimal = {
        key: _compact_wire_metadata_value(value)
        for key, value in item.items()
        if key in _WORKFLOW_SNAPSHOT_KEEP_KEYS
    }
    minimal["summary"] = "[truncated]"
    minimal["truncated"] = True
    return minimal


def _minimal_workflow_detail_preserving_waiting_human(item: dict[str, Any]) -> dict[str, Any]:
    """Minimal item that still carries its waiting-human nodes (HITL carve-out)."""
    minimal = _minimal_workflow_snapshot_item_for_wire(item)
    phases = item.get("phases")
    if not isinstance(phases, list):
        return minimal

    preserved_phases: list[dict[str, Any]] = []
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        agents = phase.get("agents")
        if not isinstance(agents, list):
            continue
        waiting_agents = [
            _workflow_agent_for_collapse(agent)
            for agent in agents
            if isinstance(agent, dict) and _is_waiting_human_agent(agent)
        ]
        if not waiting_agents:
            continue
        preserved_phases.append(
            {
                "id": phase.get("id", ""),
                "name": phase.get("name", ""),
                "status": phase.get("status", "running"),
                "agent_count": phase.get("agent_count", len(waiting_agents)),
                "completed_agent_count": phase.get("completed_agent_count", 0),
                "agents": waiting_agents,
            }
        )
    if preserved_phases:
        minimal["phases"] = preserved_phases
    return minimal


def _sanitize_workflow_snapshot_item_for_wire(item: Any) -> dict[str, Any]:
    """Sanitize one workflow item, collapsing if it exceeds the per-record budget."""
    if not isinstance(item, dict):
        return {"summary": _sanitize_history_wire_value(item), "truncated": True}
    sanitized = _sanitize_history_wire_value(item)
    if not isinstance(sanitized, dict):
        return {"summary": str(sanitized), "truncated": True}
    if _json_wire_size(sanitized) <= _HISTORY_WIRE_RECORD_MAX_BYTES:
        return sanitized
    return _collapse_oversized_workflow_snapshot_item(item)


def _fit_workflow_detail_to_budget(
    item: dict[str, Any],
    *,
    budget: int,
    preserved_prompts: dict[str, str],
) -> dict[str, Any]:
    """Shrink a workflow detail item until it fits ``budget`` bytes.

    Drop logs first, then strip to waiting-human-only, then bare metadata.
    Preserved human prompts are re-attached after every shrink so a HITL turn
    is never left without its question.
    """
    if _json_wire_size(item) <= budget:
        return item

    trimmed = dict(item)
    trimmed.pop("logs", None)
    _restore_waiting_human_prompts(trimmed, preserved_prompts)
    if _json_wire_size(trimmed) <= budget:
        trimmed["truncated"] = True
        return trimmed

    if preserved_prompts:
        trimmed = _minimal_workflow_detail_preserving_waiting_human(item)
        _restore_waiting_human_prompts(trimmed, preserved_prompts)
        if _json_wire_size(trimmed) <= budget:
            trimmed["truncated"] = True
            return trimmed

    minimal = _minimal_workflow_snapshot_item_for_wire(item)
    minimal["truncated"] = True
    return minimal


# ---------------------------------------------------------------------------
# Workflow snapshot — list shaping
# ---------------------------------------------------------------------------

def _workflow_list_summary_phase(phase: dict[str, Any]) -> dict[str, Any]:
    """Phase skeleton for list — counts and status only, no agent bodies."""
    return {
        "id": phase.get("id", ""),
        "name": phase.get("name", ""),
        "status": phase.get("status", "running"),
        "agent_count": phase.get("agent_count", 0),
        "completed_agent_count": phase.get("completed_agent_count", 0),
        "agents": [],
    }


def _workflow_list_summary_item(item: dict[str, Any]) -> dict[str, Any]:
    """Compact workflow row for ``command.workflows`` list — omits large text fields."""
    summary: dict[str, Any] = {
        key: _compact_wire_metadata_value(item.get(key))
        for key in _WORKFLOW_LIST_SUMMARY_KEEP_KEYS
        if item.get(key) is not None
    }
    for key in ("summary", "error", "result"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            summary[key] = _truncate_string_by_bytes(value, _WORKFLOW_LIST_SUMMARY_STRING_LIMIT)
        elif value is not None and key not in summary:
            summary[key] = _truncate_string_by_bytes(str(value), _WORKFLOW_LIST_SUMMARY_STRING_LIMIT)

    phases = item.get("phases")
    if isinstance(phases, list):
        summary["phases"] = [
            _workflow_list_summary_phase(phase)
            for phase in phases
            if isinstance(phase, dict)
        ]

    summary["detail_pending"] = True
    return summary


def _minimal_workflow_list_item(item: dict[str, Any]) -> dict[str, Any]:
    """Smallest list row when the full summary still exceeds the wire budget."""
    return {
        "id": _compact_wire_metadata_value(item.get("id")),
        "name": _truncate_string_by_bytes(str(item.get("name") or "workflow"), 128),
        "status": _compact_wire_metadata_value(item.get("status") or "running"),
        "agent_count": item.get("agent_count", 0),
        "completed_agent_count": item.get("completed_agent_count", 0),
        "detail_pending": True,
    }


def _fit_workflow_list_item_for_budget(item: dict[str, Any], budget: int) -> dict[str, Any]:
    """Shrink a list row until it fits the remaining byte budget."""
    candidate = _workflow_list_summary_item(item)
    if _json_wire_size(candidate) + 1 <= budget:
        return candidate
    candidate = _minimal_workflow_list_item(item)
    if _json_wire_size(candidate) + 1 <= budget:
        return candidate
    shrunk = {
        "id": _compact_wire_metadata_value(item.get("id")),
        "name": _truncate_string_by_bytes(str(item.get("name") or "workflow"), 64),
        "status": _compact_wire_metadata_value(item.get("status") or "running"),
        "detail_pending": True,
    }
    if _json_wire_size(shrunk) + 1 <= budget:
        return shrunk
    return {
        "id": _compact_wire_metadata_value(item.get("id")),
        "detail_pending": True,
        "truncated": True,
    }


# ---------------------------------------------------------------------------
# Workflow snapshot — public payload builders
# ---------------------------------------------------------------------------

def _build_workflow_list_payload(workflows: Any, *, session_id: str) -> dict[str, Any]:
    """Return lightweight workflow summaries — every run listed, detail via ``action=get``."""
    source = [item for item in (workflows if isinstance(workflows, list) else []) if isinstance(item, dict)]
    total = len(source)
    payload: dict[str, Any] = {
        "type": "workflow_run_snapshot",
        "action": "list",
        "workflows": [],
        "session_id": session_id,
        "total": total,
        "truncated": False,
    }
    budget = max(
        _TEAM_HISTORY_MIN_MAX_BYTES,
        _WORKFLOW_SNAPSHOT_MAX_BYTES - _WORKFLOW_SNAPSHOT_FRAME_OVERHEAD_BYTES,
    )
    used = _json_wire_size(payload)
    page: list[dict[str, Any]] = []

    for raw in source[:_WORKFLOW_SNAPSHOT_MAX_WORKFLOWS]:
        remaining = max(256, budget - used)
        item = _fit_workflow_list_item_for_budget(raw, remaining)
        item_size = _json_wire_size(item) + 1
        if page and used + item_size > budget:
            payload["truncated"] = True
            item = _fit_workflow_list_item_for_budget(raw, max(128, budget - used))
            item_size = _json_wire_size(item) + 1
        if used + item_size > budget:
            payload["truncated"] = True
            break
        page.append(item)
        used += item_size

    if len(page) < total:
        payload["truncated"] = True
    payload["workflows"] = page
    return payload


def _build_workflow_detail_payload(workflow: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    """Return one workflow with full detail (subject to single-record sanitize/collapse)."""
    preserved_prompts = _extract_waiting_human_prompts(workflow)
    sanitized = _sanitize_workflow_snapshot_item_for_wire(workflow)
    detail_budget = max(
        _TEAM_HISTORY_MIN_MAX_BYTES,
        _WORKFLOW_SNAPSHOT_MAX_BYTES - _WORKFLOW_SNAPSHOT_FRAME_OVERHEAD_BYTES,
    )
    item = sanitized
    truncated = bool(item.get("truncated"))
    if _json_wire_size(item) > detail_budget:
        item = _collapse_oversized_workflow_snapshot_item(workflow)
        truncated = True
        if _json_wire_size(item) > detail_budget:
            item = _minimal_workflow_detail_preserving_waiting_human(workflow)
            truncated = True
            if _json_wire_size(item) > detail_budget:
                item = _minimal_workflow_snapshot_item_for_wire(workflow)
                truncated = True
    _restore_waiting_human_prompts(item, preserved_prompts)
    item = _fit_workflow_detail_to_budget(
        item,
        budget=detail_budget,
        preserved_prompts=preserved_prompts,
    )
    truncated = truncated or bool(item.get("truncated"))
    if truncated:
        item["truncated"] = True
    else:
        item.pop("truncated", None)
    return {
        "type": "workflow_run_detail",
        "action": "get",
        "workflow": item,
        "session_id": session_id,
        "truncated": truncated,
    }


def _find_workflow_agent(
    workflow: dict[str, Any],
    *,
    agent_id: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any] | None:
    """Locate one agent node across a workflow's phases by id / correlation_id."""
    phases = workflow.get("phases")
    if not isinstance(phases, list):
        return None
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        agents = phase.get("agents")
        if not isinstance(agents, list):
            continue
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            if agent_id and agent.get("id") == agent_id:
                return agent
            if correlation_id and agent.get("correlation_id") == correlation_id:
                return agent
    return None


def _build_workflow_human_prompt_payload(
    workflow: dict[str, Any],
    *,
    session_id: str,
    agent_id: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Build the ``workflow_human_prompt`` payload for ``action=get_human_prompt``."""
    agent = _find_workflow_agent(
        workflow,
        agent_id=agent_id,
        correlation_id=correlation_id,
    )
    if agent is None:
        return {
            "type": "workflow_human_prompt",
            "action": "get_human_prompt",
            "session_id": session_id,
            "error": "agent not found",
        }
    prompt = agent.get("human_prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return {
            "type": "workflow_human_prompt",
            "action": "get_human_prompt",
            "session_id": session_id,
            "workflow_id": workflow.get("id"),
            "agent_id": agent.get("id"),
            "correlation_id": agent.get("correlation_id"),
            "human_prompt": "",
        }
    return {
        "type": "workflow_human_prompt",
        "action": "get_human_prompt",
        "session_id": session_id,
        "workflow_id": workflow.get("id"),
        "agent_id": agent.get("id"),
        "correlation_id": agent.get("correlation_id"),
        "human_prompt": prompt,
    }


def _build_workflow_snapshot_payload(workflows: Any, *, session_id: str) -> dict[str, Any]:
    """Backward-compatible alias — defaults to lightweight list summaries."""
    return _build_workflow_list_payload(workflows, session_id=session_id)


__all__ = [
    "_HISTORY_PAGE_SIZE",
    "_HISTORY_WIRE_STRING_LIMIT",
    "_HISTORY_WIRE_METADATA_STRING_LIMIT",
    "_HISTORY_WIRE_LIST_LIMIT",
    "_HISTORY_WIRE_DEPTH_LIMIT",
    "_HISTORY_WIRE_RECORD_MAX_BYTES",
    "_TEAM_HISTORY_DEFAULT_LIMIT",
    "_TEAM_HISTORY_MAX_LIMIT",
    "_TEAM_HISTORY_DEFAULT_MAX_BYTES",
    "_TEAM_HISTORY_MIN_MAX_BYTES",
    "_TEAM_HISTORY_MAX_MAX_BYTES",
    "_TEAM_HISTORY_FRAME_OVERHEAD_BYTES",
    "_WORKFLOW_SNAPSHOT_MAX_BYTES",
    "_WORKFLOW_SNAPSHOT_FRAME_OVERHEAD_BYTES",
    "_WORKFLOW_SNAPSHOT_MAX_WORKFLOWS",
    "_WORKFLOW_LIST_SUMMARY_STRING_LIMIT",
    "_WORKFLOW_COLLAPSED_AGENT_TEXT_LIMIT",
    "_WORKFLOW_WAITING_HUMAN_PROMPT_MAX_BYTES",
    "_HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES",
    "_json_wire_size",
    "_coerce_int",
    "_truncate_string_by_bytes",
    "_compact_wire_metadata_value",
    "_sanitize_history_wire_value",
    "_collapse_oversized_history_record",
    "_minimal_history_record_for_wire",
    "_sanitize_history_record_for_wire",
    "_select_history_record_page",
    "_is_waiting_human_agent",
    "_extract_waiting_human_prompts",
    "_restore_waiting_human_prompts",
    "_workflow_agent_for_collapse",
    "_collapse_oversized_workflow_snapshot_item",
    "_minimal_workflow_snapshot_item_for_wire",
    "_minimal_workflow_detail_preserving_waiting_human",
    "_sanitize_workflow_snapshot_item_for_wire",
    "_fit_workflow_detail_to_budget",
    "_workflow_list_summary_phase",
    "_workflow_list_summary_item",
    "_minimal_workflow_list_item",
    "_fit_workflow_list_item_for_budget",
    "_build_workflow_list_payload",
    "_build_workflow_detail_payload",
    "_find_workflow_agent",
    "_build_workflow_human_prompt_payload",
    "_build_workflow_snapshot_payload",
]
