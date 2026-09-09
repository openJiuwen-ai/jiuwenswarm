# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Wire-payload truncation for the AgentWebSocketServer.

Centralizes every byte-budget / shrink-and-collapse helper that prepares team
history records and swarmflow workflow snapshots for the WebSocket wire. Two
phases share one set of low-level tools:

* **History records** — ``_sanitize_history_record_for_wire`` /
  ``_select_history_record_page`` paginate a session's history under a byte
  budget, collapsing oversized records down to a metadata stub.
* **Workflow snapshots** — ``command.workflows`` serves swarmflow runs via a
  four-layer paging ladder: ``_build_workflow_list_payload`` (``list``, run
  summaries without phases), ``_build_workflow_detail_paginated``
  (``get_workflow``, run meta + paged phase summaries), ``_build_phase_detail_paginated``
  (``get_phase``, phase meta + paged agents) and ``_build_agent_detail``
  (``get_agent``, a single agent). Oversized agent string fields are split into
  ``_{field}_parts`` arrays via ``_split_oversized_agent_fields``.

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
# 单条 chat.final record 切片时每片的 content 最大字节数（外层 frame overhead 留足余量）
_HISTORY_WIRE_RECORD_PART_BYTES = 32 * 1024
# 仅这些 event_type 走切片流；其余 event_type 仍走旧 _sanitize_history_record_for_wire
_HISTORY_SPLIT_EVENT_TYPES = frozenset({"chat.final"})
_TEAM_HISTORY_DEFAULT_LIMIT = 500
_TEAM_HISTORY_MAX_LIMIT = 1000
_TEAM_HISTORY_DEFAULT_MAX_BYTES = 2 * 1024 * 1024
_TEAM_HISTORY_MIN_MAX_BYTES = 2048
_TEAM_HISTORY_MAX_MAX_BYTES = 6 * 1024 * 1024
_TEAM_HISTORY_FRAME_OVERHEAD_BYTES = 1024
_WORKFLOW_AGENT_FIELD_PART_BYTES = 32 * 1024
_WORKFLOW_LIST_DEFAULT_LIMIT = 50
_WORKFLOW_LIST_MAX_LIMIT = 200
_WORKFLOW_PHASE_DEFAULT_LIMIT = 20
_WORKFLOW_PHASE_MAX_LIMIT = 100
_WORKFLOW_AGENT_DEFAULT_LIMIT = 50
_WORKFLOW_AGENT_MAX_LIMIT = 200
_SPLITTABLE_AGENT_FIELDS = ("prompt", "outcome", "human_prompt", "human_reply", "activity", "error")

_TRUNCATE_SUFFIX = " [truncated]"

_HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES = frozenset(
    {
        "chat.reasoning",
        "chat.final",
        "chat.tool_call",
        "chat.tool_result",
        "chat.subtask_update",
        "chat.subagent_activity",
        "chat.usage_summary",
        "chat.file",
        "team.message",
        "context.usage",
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
    "agent_template_name",
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
    "budget",
    "workflow_budget",
    "budget_exhausted_scope",
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


def split_history_record_for_stream(
    record: Any,
    *,
    part_bytes: int = _HISTORY_WIRE_RECORD_PART_BYTES,
) -> list[dict[str, Any]]:
    """把单条 record 切成 ``history.get`` 流友好的多个分片帧。

    只有 ``chat.final`` record（白名单 ``_HISTORY_SPLIT_EVENT_TYPES``）才会被切片——
    它是用户最直接看到的回复正文，截断损失最大。其他 event_type
    （``chat.reasoning`` / ``chat.tool_call`` / ``chat.tool_result`` 等）
    仍走 ``_sanitize_history_record_for_wire``，维持原来的 collapse / string-truncate
    行为不变。

    对可切片的 record，顶层 ``content`` 字符串**保留原文**——
    ``_sanitize_history_wire_value`` 会把它砍到 16KB，那切片就没意义了。
    其他元数据字段照常 sanitize（短字符串几乎不会被截）。
    切完后的整体若 ≤ 单条 record wire 预算（64KB），返回单帧、不带 ``_part``
    字段，与旧协议完全兼容。否则把 content 按 ``part_bytes`` 字节切成 N 片，
    每片带完整元数据 + ``_part = {{record_id, part_idx, total_parts}}`` 标记，
    供前端按 record_id 重组。若可切片的 record 没有 string content 字段，
    退化到 sanitize 路径，仍能发出一帧（极少见，防漏）。
    """
    event_type = record.get("event_type") if isinstance(record, dict) else None
    if event_type not in _HISTORY_SPLIT_EVENT_TYPES:
        # 非白名单 event_type（思考、工具调用等）→ 不切片，走旧 sanitize 路径
        return [_sanitize_history_record_for_wire(record)]

    if not isinstance(record, dict):
        return [_sanitize_history_record_for_wire(record)]

    content = record.get("content")
    if not isinstance(content, str) or not content:
        # chat.final 无可用 string content → 走 sanitize（内部会 collapse 到元数据 stub）
        return [_sanitize_history_record_for_wire(record)]

    # 元数据照常 sanitize；content 保留原文
    metadata = {
        key: _sanitize_history_wire_value(value)
        for key, value in record.items()
        if key != "content"
    }
    full = {**metadata, "content": content}

    if _json_wire_size(full) <= _HISTORY_WIRE_RECORD_MAX_BYTES:
        # 不超预算 → 单帧，不带 _part，与旧协议兼容
        return [full]

    # 取 record_id 供前端重组：优先用 id，其次 request_id，最后兜底 hist-<objid>
    record_id = (
        full.get("id")
        or full.get("request_id")
        or f"hist-{id(full)}"
    )
    if not isinstance(record_id, str) or not record_id:
        record_id = f"hist-{id(full)}"
    else:
        record_id = str(record_id)

    # 按字符切而非按字节切：避免多字节 UTF-8（中文 3 字节、emoji 4 字节）
    # 在切片边界被切到一半导致丢字符。Python 字符串切片按字符边界走，
    # 直接 content[i:j] 就是第 i..j-1 个字符拼成的字符串，天然合法 UTF-8。
    # 每片字符数：用 part_bytes / 4 估算（UTF-8 单字符最多 4 字节），
    # 最坏情况（全 4 字节字符）每片仍 ≤ part_bytes ≤ 64KB wire 预算，安全。
    chars_per_part = max(256, part_bytes // 4)
    total = max(1, (len(content) + chars_per_part - 1) // chars_per_part)
    chunks: list[dict[str, Any]] = []
    for idx in range(total):
        slice_chars = content[idx * chars_per_part:(idx + 1) * chars_per_part]
        chunk = {**metadata}
        chunk["content"] = slice_chars
        chunk["_part"] = {
            "record_id": record_id,
            "part_idx": idx,
            "total_parts": total,
        }
        chunks.append(chunk)
    return chunks


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
# Workflow snapshot — agent field part splitting
# ---------------------------------------------------------------------------

def _split_oversized_agent_fields(
    agent: dict[str, Any],
    *,
    part_bytes: int = _WORKFLOW_AGENT_FIELD_PART_BYTES,
) -> dict[str, Any]:
    """Replace oversized string fields with ``_{field}_parts`` arrays.

    A field whose UTF-8 byte length exceeds ``part_bytes`` is sliced by
    **character** boundary (not byte) so every slice is valid UTF-8, mirroring
    ``split_history_record_for_stream``. Small fields are left untouched.
    """
    out = dict(agent)
    for field in _SPLITTABLE_AGENT_FIELDS:
        val = out.get(field)
        if not isinstance(val, str):
            continue
        if len(val.encode("utf-8")) <= part_bytes:
            continue
        chars_per_part = max(256, part_bytes // 4)
        total = max(1, (len(val) + chars_per_part - 1) // chars_per_part)
        out[f"{field}_parts"] = [
            {
                "part_idx": i,
                "total_parts": total,
                "content": val[i * chars_per_part:(i + 1) * chars_per_part],
            }
            for i in range(total)
        ]
        del out[field]
    return out


# ---------------------------------------------------------------------------
# Workflow snapshot — list summary shaping
# ---------------------------------------------------------------------------

def _workflow_list_summary_item(item: dict[str, Any]) -> dict[str, Any]:
    """Compact workflow row for ``action=list`` — no phases, detail_pending.

    Fields are carried in full — pagination bounds the frame, so there is no
    per-string truncation.
    """
    summary: dict[str, Any] = {}
    for key in _WORKFLOW_LIST_SUMMARY_KEEP_KEYS:
        value = item.get(key)
        if value is None:
            continue
        summary[key] = value
    for key in ("summary", "error", "result"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            summary[key] = value
        elif value is not None and key not in summary:
            summary[key] = value
    summary["detail_pending"] = True
    return summary


def _workflow_phase_summary(phase: dict[str, Any]) -> dict[str, Any]:
    """Phase meta for ``action=get_workflow`` — no agents, detail_pending."""
    out: dict[str, Any] = {
        "id": phase.get("id", ""),
        "name": phase.get("name", ""),
        "status": phase.get("status", "running"),
        "agent_count": phase.get("agent_count", 0),
        "completed_agent_count": phase.get("completed_agent_count", 0),
    }
    for opt_key in ("phase_type", "parent_phase", "nested_phase", "iteration"):
        if opt_key in phase:
            out[opt_key] = phase[opt_key]
    out["detail_pending"] = True
    return out


# Fields carried in the agent summary (get_phase). Heavy text fields
# (prompt/outcome/human_prompt/human_reply/activity/error) are omitted —
# get_agent is the universal layer for full agent content. A short preview
# (~200 chars) of outcome/error is carried so the tree row can show a
# one-line stub without a per-agent RPC; the full text is still get_agent.
_WORKFLOW_AGENT_SUMMARY_KEEP_KEYS = (
    "id", "name", "status", "model", "kind", "node_type",
    "started_at", "completed_at", "duration_ms", "token_count",
    "correlation_id",
)
_WORKFLOW_AGENT_SUMMARY_PREVIEW_CHARS = 200


def _preview_text(value: Any, *, limit: int = _WORKFLOW_AGENT_SUMMARY_PREVIEW_CHARS) -> str | None:
    """First ~`limit` chars of a string, whitespace-normalized; None if empty."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[:limit]


def _workflow_agent_summary(agent: dict[str, Any]) -> dict[str, Any]:
    """Agent summary for ``action=get_phase`` — no heavy text fields, detail_pending.

    Carries only the fields the tree/list row needs to render; the full body
    (prompt/outcome/human_prompt/human_reply/activity/error) is fetched on
    demand via ``action=get_agent``. A short ``outcome_preview``/``error_preview``
    (~200 chars) is included so the row can show a stub line without a per-agent
    RPC.
    """
    out: dict[str, Any] = {}
    for key in _WORKFLOW_AGENT_SUMMARY_KEEP_KEYS:
        value = agent.get(key)
        if value is None:
            continue
        out[key] = value
    outcome_preview = _preview_text(agent.get("outcome"))
    if outcome_preview is not None:
        out["outcome_preview"] = outcome_preview
    error_preview = _preview_text(agent.get("error"))
    if error_preview is not None:
        out["error_preview"] = error_preview
    out["detail_pending"] = True
    return out


def _workflow_run_meta(workflow: dict[str, Any]) -> dict[str, Any]:
    """Run-level meta for ``action=get_workflow`` — run fields, no phases.

    Fields are carried in full — ``get_workflow`` returns a single run, so
    there is no per-string truncation.
    """
    meta: dict[str, Any] = {}
    for key in _WORKFLOW_LIST_SUMMARY_KEEP_KEYS:
        value = workflow.get(key)
        if value is None:
            continue
        meta[key] = value
    for key in ("summary", "error", "result"):
        value = workflow.get(key)
        if isinstance(value, str) and value.strip():
            meta[key] = value
        elif value is not None and key not in meta:
            meta[key] = value
    logs = workflow.get("logs")
    if isinstance(logs, list) and logs:
        meta["logs"] = [str(log) for log in logs[-10:]]
        if len(logs) > 10:
            meta["logs_truncated"] = True
    return meta


def _find_phase(workflow: dict[str, Any], phase_id: str) -> dict[str, Any] | None:
    phases = workflow.get("phases")
    if not isinstance(phases, list):
        return None
    for phase in phases:
        if isinstance(phase, dict) and phase.get("id") == phase_id:
            return phase
    return None


def _find_agent(phase: dict[str, Any], agent_id: str) -> dict[str, Any] | None:
    agents = phase.get("agents")
    if not isinstance(agents, list):
        return None
    for agent in agents:
        if isinstance(agent, dict) and agent.get("id") == agent_id:
            return agent
    return None


# ---------------------------------------------------------------------------
# Workflow snapshot — public paging builders
# ---------------------------------------------------------------------------

def _build_workflow_list_payload(
    workflows: Any,
    *,
    session_id: str,
    offset: int = 0,
    limit: int = _WORKFLOW_LIST_DEFAULT_LIMIT,
    total: int | None = None,
) -> dict[str, Any]:
    """``action=list`` — paged workflow summaries, no phases."""
    source = [item for item in (workflows if isinstance(workflows, list) else []) if isinstance(item, dict)]
    real_total = total if total is not None else len(source)
    clamped_limit = max(1, min(limit, _WORKFLOW_LIST_MAX_LIMIT))
    page = source[offset:offset + clamped_limit]
    return {
        "type": "workflow_run_snapshot",
        "action": "list",
        "session_id": session_id,
        "workflows": [_workflow_list_summary_item(item) for item in page],
        "total": real_total,
        "has_more": (offset + clamped_limit) < real_total,
    }


def _build_workflow_detail_paginated(
    workflow: dict[str, Any],
    *,
    session_id: str,
    phase_offset: int = 0,
    phase_limit: int = _WORKFLOW_PHASE_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """``action=get_workflow`` — run meta + paged phase summaries, no agents."""
    phases = workflow.get("phases") if isinstance(workflow.get("phases"), list) else []
    phase_total = len(phases)
    clamped_limit = max(1, min(phase_limit, _WORKFLOW_PHASE_MAX_LIMIT))
    page = phases[phase_offset:phase_offset + clamped_limit]
    wf = _workflow_run_meta(workflow)
    wf["phases"] = [_workflow_phase_summary(p) for p in page if isinstance(p, dict)]
    return {
        "type": "workflow_run_detail",
        "action": "get_workflow",
        "session_id": session_id,
        "workflow": wf,
        "phase_total": phase_total,
        "has_more": (phase_offset + clamped_limit) < phase_total,
    }


def _build_phase_detail_paginated(
    workflow: dict[str, Any],
    *,
    session_id: str,
    phase_id: str,
    agent_offset: int = 0,
    agent_limit: int = _WORKFLOW_AGENT_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """``action=get_phase`` — phase meta + paged agent summaries, no heavy text.

    Each agent is returned as a lightweight summary (id/name/status/model/...),
    marked ``detail_pending:true``; the full body (prompt/outcome/human_prompt/
    human_reply/activity/error) is fetched on demand via ``action=get_agent``.
    """
    phase = _find_phase(workflow, phase_id)
    if phase is None:
        return {
            "type": "workflow_phase_detail",
            "action": "get_phase",
            "session_id": session_id,
            "workflow_id": workflow.get("id"),
            "phase_id": phase_id,
            "ok": False,
            "error": f"phase not found: {phase_id}",
        }
    agents = phase.get("agents") if isinstance(phase.get("agents"), list) else []
    agent_total = len(agents)
    clamped_limit = max(1, min(agent_limit, _WORKFLOW_AGENT_MAX_LIMIT))
    page = agents[agent_offset:agent_offset + clamped_limit]
    phase_meta = _workflow_phase_summary(phase)
    phase_meta.pop("detail_pending", None)
    phase_meta["agents"] = [_workflow_agent_summary(a) for a in page if isinstance(a, dict)]
    return {
        "type": "workflow_phase_detail",
        "action": "get_phase",
        "session_id": session_id,
        "workflow_id": workflow.get("id"),
        "phase": phase_meta,
        "agent_total": agent_total,
        "has_more": (agent_offset + clamped_limit) < agent_total,
    }


def _build_agent_detail(
    workflow: dict[str, Any],
    *,
    session_id: str,
    phase_id: str,
    agent_id: str,
) -> dict[str, Any]:
    """``action=get_agent`` — single full agent (field parts applied)."""
    phase = _find_phase(workflow, phase_id)
    if phase is None:
        return {
            "type": "workflow_agent_detail",
            "action": "get_agent",
            "session_id": session_id,
            "workflow_id": workflow.get("id"),
            "phase_id": phase_id,
            "agent_id": agent_id,
            "ok": False,
            "error": f"phase not found: {phase_id}",
        }
    agent = _find_agent(phase, agent_id)
    if agent is None:
        return {
            "type": "workflow_agent_detail",
            "action": "get_agent",
            "session_id": session_id,
            "workflow_id": workflow.get("id"),
            "phase_id": phase_id,
            "agent_id": agent_id,
            "ok": False,
            "error": f"agent not found: {agent_id}",
        }
    return {
        "type": "workflow_agent_detail",
        "action": "get_agent",
        "session_id": session_id,
        "workflow_id": workflow.get("id"),
        "phase_id": phase_id,
        "agent": _split_oversized_agent_fields(agent),
    }


__all__ = [
    "_HISTORY_PAGE_SIZE",
    "_HISTORY_WIRE_STRING_LIMIT",
    "_HISTORY_WIRE_METADATA_STRING_LIMIT",
    "_HISTORY_WIRE_LIST_LIMIT",
    "_HISTORY_WIRE_DEPTH_LIMIT",
    "_HISTORY_WIRE_RECORD_MAX_BYTES",
    "_HISTORY_WIRE_RECORD_PART_BYTES",
    "_HISTORY_SPLIT_EVENT_TYPES",
    "_TEAM_HISTORY_DEFAULT_LIMIT",
    "_TEAM_HISTORY_MAX_LIMIT",
    "_TEAM_HISTORY_DEFAULT_MAX_BYTES",
    "_TEAM_HISTORY_MIN_MAX_BYTES",
    "_TEAM_HISTORY_MAX_MAX_BYTES",
    "_TEAM_HISTORY_FRAME_OVERHEAD_BYTES",
    "_WORKFLOW_AGENT_FIELD_PART_BYTES",
    "_WORKFLOW_LIST_DEFAULT_LIMIT",
    "_WORKFLOW_LIST_MAX_LIMIT",
    "_WORKFLOW_PHASE_DEFAULT_LIMIT",
    "_WORKFLOW_PHASE_MAX_LIMIT",
    "_WORKFLOW_AGENT_DEFAULT_LIMIT",
    "_WORKFLOW_AGENT_MAX_LIMIT",
    "_SPLITTABLE_AGENT_FIELDS",
    "_HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES",
    "_json_wire_size",
    "_coerce_int",
    "_truncate_string_by_bytes",
    "_compact_wire_metadata_value",
    "_sanitize_history_wire_value",
    "_collapse_oversized_history_record",
    "_minimal_history_record_for_wire",
    "_sanitize_history_record_for_wire",
    "split_history_record_for_stream",
    "_select_history_record_page",
    "_split_oversized_agent_fields",
    "_workflow_list_summary_item",
    "_workflow_phase_summary",
    "_workflow_agent_summary",
    "_preview_text",
    "_WORKFLOW_AGENT_SUMMARY_KEEP_KEYS",
    "_WORKFLOW_AGENT_SUMMARY_PREVIEW_CHARS",
    "_workflow_run_meta",
    "_find_phase",
    "_find_agent",
    "_build_workflow_list_payload",
    "_build_workflow_detail_paginated",
    "_build_phase_detail_paginated",
    "_build_agent_detail",
]
