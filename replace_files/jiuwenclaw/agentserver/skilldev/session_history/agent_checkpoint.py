# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent 模式 SkillDev 任务 checkpoint（与 Pipeline stage 无关）."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from jiuwenclaw.agentserver.skilldev.session_history.schema import (
    SkillDevSessionEventRecord,
    SkillDevSessionSummary,
)

AGENT_RUNNER = "agent"

AGENT_STATUS_LABELS: dict[str, str] = {
    "active": "执行中",
    "idle": "等待继续",
    "pending_interaction": "待确认",
    "completed": "已完成",
    "error": "失败",
    "cancelled": "已取消",
}


def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def is_agent_checkpoint(data: dict[str, Any]) -> bool:
    return str(data.get("runner") or "") == AGENT_RUNNER


def _workspace_has_skill_package(task_workspace: Path) -> bool:
    output_dir = task_workspace / "output"
    if not output_dir.is_dir():
        return False
    return any(
        p.is_file() and p.suffix in (".skill", ".zip") for p in output_dir.iterdir()
    )


def _last_event_of(
    events: list[SkillDevSessionEventRecord], *event_types: str
) -> SkillDevSessionEventRecord | None:
    wanted = set(event_types)
    for event in reversed(events):
        if event.event_type in wanted:
            return event
    return None


def _pending_interaction_from_events(
    events: list[SkillDevSessionEventRecord],
) -> dict[str, Any] | None:
    answered_request_ids: set[str] = set()
    for event in events:
        if event.event_type != "skilldev.user_answer":
            continue
        payload = event.payload or {}
        rid = str(payload.get("request_id") or "").strip()
        if rid:
            answered_request_ids.add(rid)

    last_confirm_seq: int | None = None
    for event in reversed(events):
        if event.event_type == "skilldev.confirm_resolved":
            last_confirm_seq = event.seq
            break

    for event in reversed(events):
        et = event.event_type
        payload = dict(event.payload or {})
        if et == "skilldev.ask_user_question":
            rid = str(payload.get("request_id") or "").strip()
            if rid and rid in answered_request_ids:
                continue
            return {"kind": "ask_user", "request_id": rid, "confirm_type": "question_clarify"}
        if et == "skilldev.confirm_request":
            if payload.get("interactive") is False or payload.get("resolved"):
                continue
            if last_confirm_seq is not None and event.seq <= last_confirm_seq:
                continue
            return {
                "kind": "confirm",
                "confirm_type": str(payload.get("confirm_type") or ""),
                "confirm_seq": event.seq,
            }
    return None


def derive_agent_status(
    events: list[SkillDevSessionEventRecord],
    *,
    task_workspace: Path | None = None,
) -> str:
    """根据事件时间线推导 Agent 任务状态."""
    if not events:
        return "idle"

    last = events[-1]
    if last.event_type == "skilldev.error":
        return "error"
    if any(e.event_type == "skilldev.completed" for e in events):
        return "completed"
    if task_workspace is not None and _workspace_has_skill_package(task_workspace):
        last = events[-1].event_type if events else ""
        if last in ("skilldev.artifact_ready", "skilldev.completed"):
            return "completed"

    if _pending_interaction_from_events(events) is not None:
        return "pending_interaction"

    last_agent = _last_event_of(events, "skilldev.agent_completed")
    if last_agent is not None:
        return "idle"

    last_started = _last_event_of(events, "skilldev.started")
    if last_started is not None and last.event_type not in {
        "skilldev.completed",
        "skilldev.error",
        "skilldev.agent_completed",
    }:
        return "active"

    return "idle"


def _extract_todos_from_events(events: list[SkillDevSessionEventRecord]) -> list[dict[str, Any]]:
    todos: list[dict[str, Any]] = []
    for event in events:
        if event.event_type not in ("skilldev.todos_update", "todo.updated"):
            continue
        payload = event.payload or {}
        raw = payload.get("todos")
        if isinstance(raw, list):
            todos = [dict(x) for x in raw if isinstance(x, dict)]
    return todos


def _collect_artifacts_from_events(events: list[SkillDevSessionEventRecord]) -> list[dict[str, Any]]:
    latest_by_id: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.event_type != "skilldev.artifact_ready":
            continue
        artifact = (event.payload or {}).get("artifact")
        if not isinstance(artifact, dict):
            continue
        aid = str(artifact.get("id") or artifact.get("name") or "")
        if aid:
            latest_by_id[aid] = artifact
    return list(latest_by_id.values())


def _resolve_query(events: list[SkillDevSessionEventRecord], checkpoint: dict[str, Any]) -> str:
    inp = checkpoint.get("input")
    if isinstance(inp, dict):
        q = inp.get("query")
        if isinstance(q, str) and q.strip():
            return q
    for event in events:
        if event.event_type == "skilldev.user_start":
            q = (event.payload or {}).get("query")
            if isinstance(q, str) and q.strip():
                return q
    return ""


def _session_title(checkpoint: dict[str, Any], query: str) -> str:
    inp = checkpoint.get("input")
    if isinstance(inp, dict):
        skill_name = inp.get("skill_name")
        if isinstance(skill_name, str) and skill_name.strip():
            return skill_name.strip()
    q = query.strip()
    if len(q) > 48:
        return q[:48] + "…"
    return q or checkpoint.get("task_id", "")


def build_agent_checkpoint(
    *,
    task_id: str,
    events: list[SkillDevSessionEventRecord],
    task_workspace: Path,
    mode: str = "create",
    conversation_id: str = "",
    round_count: int = 0,
    error: str | None = None,
) -> dict[str, Any]:
    status = derive_agent_status(events, task_workspace=task_workspace)
    if error and status != "completed":
        status = "error"

    existing_input: dict[str, Any] = {}
    todos = _extract_todos_from_events(events)
    artifacts = _collect_artifacts_from_events(events)
    pending = _pending_interaction_from_events(events)
    query = _resolve_query(events, {"input": existing_input})

    now = _utc_now_iso()
    return {
        "runner": AGENT_RUNNER,
        "task_id": task_id,
        "status": status,
        "mode": mode,
        "input": {
            "query": query,
            "skill_name": _skill_name_from_events(events),
        },
        "todos": todos,
        "artifacts": artifacts,
        "pending_interaction": pending,
        "conversation_id": conversation_id,
        "round_count": round_count,
        "created_at": now,
        "updated_at": now,
        "error": error,
    }


def merge_agent_checkpoint(
    existing: dict[str, Any] | None,
    *,
    task_id: str,
    events: list[SkillDevSessionEventRecord],
    task_workspace: Path,
    mode: str = "create",
    conversation_id: str = "",
    round_count: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """合并已有 checkpoint，保留 created_at / input.query."""
    fresh = build_agent_checkpoint(
        task_id=task_id,
        events=events,
        task_workspace=task_workspace,
        mode=mode,
        conversation_id=conversation_id or str((existing or {}).get("conversation_id") or ""),
        round_count=round_count if round_count is not None else int((existing or {}).get("round_count") or 0),
        error=error,
    )
    if existing:
        fresh["created_at"] = existing.get("created_at") or fresh["created_at"]
        old_inp = existing.get("input")
        if isinstance(old_inp, dict):
            new_inp = fresh.get("input")
            if isinstance(new_inp, dict):
                if not new_inp.get("query") and old_inp.get("query"):
                    new_inp["query"] = old_inp["query"]
                if not new_inp.get("skill_name") and old_inp.get("skill_name"):
                    new_inp["skill_name"] = old_inp["skill_name"]
    fresh["updated_at"] = _utc_now_iso()
    return fresh


def _skill_name_from_events(events: list[SkillDevSessionEventRecord]) -> str:
    for event in reversed(events):
        if event.event_type != "skilldev.skill_name_ready":
            continue
        name = (event.payload or {}).get("skill_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


def build_agent_session_summary(task_id: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
    status = str(checkpoint.get("status") or "idle")
    updated_at = str(checkpoint.get("updated_at") or "")
    created_at = str(checkpoint.get("created_at") or "")
    is_suspended = status == "pending_interaction"
    inp = checkpoint.get("input") if isinstance(checkpoint.get("input"), dict) else {}
    query = str(inp.get("query") or "")
    summary = SkillDevSessionSummary(
        task_id=task_id,
        stage=status,
        updated_at=updated_at,
        created_at=created_at,
        is_suspended=is_suspended,
    )
    data = summary.to_dict()
    data["runner"] = AGENT_RUNNER
    data["status"] = status
    data["status_label"] = AGENT_STATUS_LABELS.get(status, status)
    data["title"] = _session_title(checkpoint, query)
    todos = checkpoint.get("todos")
    if isinstance(todos, list) and todos:
        done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
        data["todo_progress"] = f"{done}/{len(todos)}"
    return data


def build_agent_restore_payload(
    *,
    task_id: str,
    checkpoint: dict[str, Any],
    events: list[SkillDevSessionEventRecord],
    timeline_items: list[dict[str, Any]],
) -> dict[str, Any]:
    from jiuwenclaw.agentserver.skilldev.session_history.assembler import (
        _resolve_pending_confirm,
    )

    status = str(checkpoint.get("status") or "idle")
    is_suspended = status == "pending_interaction"
    is_processing = status == "active"
    pending_confirm = _resolve_pending_confirm(timeline_items)
    todos = checkpoint.get("todos")
    if not isinstance(todos, list) or not todos:
        todos = _extract_todos_from_events(events)
    artifacts = checkpoint.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        artifacts = _collect_artifacts_from_events(events)

    snapshot = {
        "runner": AGENT_RUNNER,
        "task_id": task_id,
        "stage": "agent",
        "status": status,
        "mode": checkpoint.get("mode", "create"),
        "iteration": int(checkpoint.get("iteration") or 0),
        "is_suspended": is_suspended,
        "is_processing": is_processing,
        "query": _resolve_query(events, checkpoint),
        "todos": todos,
        "artifacts": artifacts,
        "created_at": checkpoint.get("created_at"),
        "updated_at": checkpoint.get("updated_at"),
        "error": checkpoint.get("error"),
        "pending_confirm": pending_confirm if is_suspended else None,
        "round_count": int(checkpoint.get("round_count") or 0),
    }
    return {
        "task_id": task_id,
        "runner": AGENT_RUNNER,
        "snapshot": snapshot,
        "timeline_items": timeline_items,
        "version": "2",
    }
