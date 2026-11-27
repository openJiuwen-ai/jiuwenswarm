from __future__ import annotations

from typing import Any

from jiuwenclaw.agentserver.skilldev.schema import SUSPENSION_POINTS, SkillDevStage, compute_todos
from jiuwenclaw.agentserver.skilldev.session_history.schema import (
    SkillDevSessionEventRecord,
    SkillDevSessionSummary,
)


def build_session_summary(task_id: str, state_dict: dict[str, Any]) -> SkillDevSessionSummary:
    stage = str(state_dict.get("stage") or "init")
    updated_at = str(state_dict.get("updated_at") or "")
    created_at = str(state_dict.get("created_at") or "")
    is_suspended = stage in {s.value for s in SUSPENSION_POINTS}
    return SkillDevSessionSummary(
        task_id=task_id,
        stage=stage,
        updated_at=updated_at,
        created_at=created_at,
        is_suspended=is_suspended,
    )


def build_restore_payload(
    *,
    task_id: str,
    state_dict: dict[str, Any],
    events: list[SkillDevSessionEventRecord],
) -> dict[str, Any]:
    stage_str = str(state_dict.get("stage") or "init")
    try:
        stage_enum = SkillDevStage(stage_str)
    except Exception:  # noqa: BLE001
        stage_enum = SkillDevStage.INIT
    is_suspended = stage_enum in SUSPENSION_POINTS
    is_processing = stage_enum not in {
        SkillDevStage.COMPLETED,
        SkillDevStage.ERROR,
    } and not is_suspended

    timeline_items = normalize_timeline(events)
    pending_confirm = _resolve_pending_confirm(timeline_items)
    snapshot = {
        "task_id": task_id,
        "stage": stage_str,
        "mode": state_dict.get("mode", "create"),
        "iteration": state_dict.get("iteration", 0),
        "is_suspended": is_suspended,
        "is_processing": is_processing,
        "query": _resolve_restore_query(state_dict, events),
        "todos": compute_todos(stage_enum),
        "artifacts": _collect_artifacts_from_events(events),
        "created_at": state_dict.get("created_at"),
        "updated_at": state_dict.get("updated_at"),
        "error": state_dict.get("error"),
        "pending_confirm": pending_confirm if is_suspended else None,
    }
    return {
        "task_id": task_id,
        "snapshot": snapshot,
        "timeline_items": timeline_items,
        "version": "1",
    }


def _resolve_restore_query(
    state_dict: dict[str, Any],
    events: list[SkillDevSessionEventRecord],
) -> str:
    inp = state_dict.get("input")
    if isinstance(inp, dict):
        q = inp.get("query")
        if isinstance(q, str):
            return q
    for event in reversed(events):
        if event.event_type not in ("skilldev.user_start", "skilldev.user_chat"):
            continue
        payload = event.payload or {}
        q = payload.get("query") or payload.get("message")
        if isinstance(q, str):
            return q
    return ""


def _collect_artifacts_from_events(events: list[SkillDevSessionEventRecord]) -> list[dict[str, Any]]:
    latest_by_id: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.event_type != "skilldev.artifact_ready":
            continue
        artifact = event.payload.get("artifact")
        if not isinstance(artifact, dict):
            continue
        aid = str(artifact.get("id") or artifact.get("name") or "")
        if not aid:
            continue
        latest_by_id[aid] = artifact
    return list(latest_by_id.values())


def normalize_timeline(events: list[SkillDevSessionEventRecord]) -> list[dict[str, Any]]:
    """将事件列表规范为 restore 用 timeline_items（Pipeline / Agent / 小艺共用）."""
    return _normalize_timeline(events)


def _normalize_timeline(events: list[SkillDevSessionEventRecord]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    has_explicit_confirm_resolved = any(
        item.event_type == "skilldev.confirm_resolved" for item in events
    )
    pending_confirm_indexes: list[int] = []

    for event in events:
        payload = dict(event.payload or {})
        row = {
            "seq": event.seq,
            "timestamp": event.timestamp,
            "source": event.source,
            "event_type": event.event_type,
            "payload": payload,
        }
        if event.event_type == "skilldev.confirm_request":
            payload.setdefault("interactive", True)
            pending_confirm_indexes.append(len(normalized))
            normalized.append(row)
            continue
        if event.event_type == "skilldev.confirm_resolved":
            _mark_latest_confirm_resolved(normalized, pending_confirm_indexes, payload)
            normalized.append(row)
            continue
        if event.event_type == "skilldev.user_respond" and not has_explicit_confirm_resolved:
            compat_payload = {
                "confirm_type": _peek_latest_confirm_type(normalized, pending_confirm_indexes),
                "action": payload.get("action"),
                "feedback": payload.get("feedback"),
                "answers": payload.get("answers"),
                "legacy_compat": True,
            }
            _mark_latest_confirm_resolved(normalized, pending_confirm_indexes, compat_payload)
            normalized.append(row)
            normalized.append(
                {
                    "seq": event.seq,
                    "timestamp": event.timestamp,
                    "source": "assistant",
                    "event_type": "skilldev.confirm_resolved",
                    "payload": compat_payload,
                }
            )
            continue
        normalized.append(row)
    return normalized


def _peek_latest_confirm_type(
    normalized: list[dict[str, Any]],
    pending_confirm_indexes: list[int],
) -> str | None:
    if not pending_confirm_indexes:
        return None
    idx = pending_confirm_indexes[-1]
    payload = normalized[idx].get("payload") if 0 <= idx < len(normalized) else None
    if isinstance(payload, dict):
        confirm_type = payload.get("confirm_type")
        if isinstance(confirm_type, str) and confirm_type:
            return confirm_type
    return None


def _mark_latest_confirm_resolved(
    normalized: list[dict[str, Any]],
    pending_confirm_indexes: list[int],
    resolved_payload: dict[str, Any],
) -> int | None:
    if not pending_confirm_indexes:
        return None
    idx = pending_confirm_indexes.pop()
    if idx < 0 or idx >= len(normalized):
        return None
    resolved_seq = normalized[idx].get("seq")
    if isinstance(resolved_seq, int):
        resolved_payload.setdefault("confirm_seq", resolved_seq)
    payload = normalized[idx].get("payload")
    if not isinstance(payload, dict):
        return None
    payload["interactive"] = False
    payload["resolved"] = True
    if isinstance(resolved_seq, int):
        payload["confirm_seq"] = resolved_seq
    if resolved_payload.get("action") is not None:
        payload["resolved_action"] = resolved_payload.get("action")
    if resolved_payload.get("feedback") is not None:
        payload["resolved_feedback"] = resolved_payload.get("feedback")
    if resolved_payload.get("answers") is not None:
        payload["resolved_answers"] = resolved_payload.get("answers")
    return resolved_seq


def _resolve_pending_confirm(timeline_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in reversed(timeline_items):
        if item.get("event_type") != "skilldev.confirm_request":
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("interactive") is False:
            continue
        return {
            "confirm_type": payload.get("confirm_type"),
            "title": payload.get("title"),
            "message": payload.get("message"),
            "actions": payload.get("actions"),
            "data": payload.get("data"),
        }
    return None
