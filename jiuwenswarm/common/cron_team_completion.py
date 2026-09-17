"""Shared cron team round completion signals for gateway and agent server."""

from __future__ import annotations

import asyncio
from typing import Any

CRON_LEADER_PLACEHOLDER_MARKERS = (
    "最终报告即将生成",
    "Integration 阶段进行中",
    "整合阶段进行中",
)


def is_cron_leader_placeholder_text(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return True
    return any(marker in normalized for marker in CRON_LEADER_PLACEHOLDER_MARKERS)


def _is_cron_leader_final_event(event: dict[str, Any]) -> bool:
    """Only the team leader's chat.final counts toward cron completion."""
    if str(event.get("event_type") or "").strip() != "chat.final":
        return False
    role = str(event.get("role") or "").strip().lower()
    if role == "teammate":
        return False
    member_name = str(event.get("member_name") or "").strip()
    return not member_name


def new_cron_team_round_state() -> dict[str, Any]:
    return {
        "workflow_started": False,
        "workflow_completed": False,
        "leader_final_after_workflow": False,
        "leader_final_seen": False,
        "team_round_completed": False,
        "tasks_ever_created": False,
        "open_team_tasks": {},
        "active_team_members": {},
        "leader_text": "",
        "workflow_text": "",
    }


def cron_team_round_has_open_tasks(state: dict[str, Any]) -> bool:
    open_tasks = state.get("open_team_tasks")
    return bool(open_tasks) if isinstance(open_tasks, dict) else False


def cron_team_round_has_active_members(state: dict[str, Any]) -> bool:
    active_members = state.get("active_team_members")
    return bool(active_members) if isinstance(active_members, dict) else False


def cron_team_round_has_result_text(state: dict[str, Any]) -> bool:
    leader = str(state.get("leader_text") or "").strip()
    workflow = str(state.get("workflow_text") or "").strip()
    return bool(leader or workflow)


def _harness_round_can_end(state: dict[str, Any]) -> bool:
    leader = str(state.get("leader_text") or "").strip()
    return (
        not state.get("workflow_started")
        and not state.get("workflow_completed")
        and not cron_team_round_has_open_tasks(state)
        and not cron_team_round_has_active_members(state)
        and state.get("leader_final_seen")
        and bool(leader)
        and not is_cron_leader_placeholder_text(leader)
    )


def _cron_solo_harness_end_pending(state: dict[str, Any]) -> bool:
    """True when harness-style completion is imminent but tasks may still be delegated."""
    if state.get("workflow_started") or state.get("workflow_completed"):
        return False
    if state.get("tasks_ever_created"):
        return False
    if cron_team_round_has_open_tasks(state) or cron_team_round_has_active_members(state):
        return False
    leader = str(state.get("leader_text") or "").strip()
    return (
        state.get("leader_final_seen")
        and bool(leader)
        and not is_cron_leader_placeholder_text(leader)
    )


async def _drain_cron_delegation_grace_events(
    *,
    request_queue: asyncio.Queue,
    round_state: dict[str, Any],
    grace_seconds: float = 2.0,
) -> list[dict[str, Any]]:
    """Wait briefly after a solo harness final in case task.created events follow."""
    drained: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + grace_seconds
    while asyncio.get_running_loop().time() < deadline:
        timeout = max(0.0, deadline - asyncio.get_running_loop().time())
        try:
            event = await asyncio.wait_for(request_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        if not isinstance(event, dict):
            continue
        drained.append(event)
        apply_cron_team_round_event(round_state, event)
        if round_state.get("tasks_ever_created"):
            break
    return drained


def cron_team_round_should_end(
    state: dict[str, Any],
    *,
    chunk_complete: bool = False,
) -> bool:
    if chunk_complete:
        if state.get("workflow_completed") and state.get("leader_final_after_workflow"):
            return True
        if state.get("team_round_completed") and cron_team_round_has_result_text(state):
            leader = str(state.get("leader_text") or "").strip()
            if leader and is_cron_leader_placeholder_text(leader):
                return False
            return True
        return _harness_round_can_end(state)
    if state.get("workflow_completed") and state.get("leader_final_after_workflow"):
        return True
    if state.get("team_round_completed") and cron_team_round_has_result_text(state):
        leader = str(state.get("leader_text") or "").strip()
        if leader and is_cron_leader_placeholder_text(leader):
            return False
        return True
    return _harness_round_can_end(state)


def _apply_team_task_event(state: dict[str, Any], nested: dict[str, Any]) -> None:
    task_type = str(nested.get("type") or "").strip()
    task_id = str(nested.get("task_id") or "").strip()
    if not task_id:
        return
    open_tasks = state.setdefault("open_team_tasks", {})
    if not isinstance(open_tasks, dict):
        open_tasks = {}
        state["open_team_tasks"] = open_tasks
    if task_type.endswith(".created"):
        state["tasks_ever_created"] = True
        open_tasks[task_id] = True
        # Leader may emit an interim chat.final before task.created events land.
        # Clear the harness completion latch so cron waits for the post-delegation final.
        state["leader_final_seen"] = False
    elif task_type.endswith(".completed"):
        open_tasks.pop(task_id, None)


def _apply_team_member_event(state: dict[str, Any], nested: dict[str, Any]) -> None:
    member_type = str(nested.get("type") or "").strip()
    if member_type != "team.member.status_changed":
        return
    member_id = str(nested.get("member_id") or "").strip()
    if not member_id:
        return
    new_status = str(nested.get("new_status") or "").strip().lower()
    active_members = state.setdefault("active_team_members", {})
    if not isinstance(active_members, dict):
        active_members = {}
        state["active_team_members"] = active_members
    if new_status in {"busy", "working", "starting"}:
        active_members[member_id] = True
    elif new_status in {"ready", "idle", "stopped"}:
        active_members.pop(member_id, None)


def _extract_workflow_summary(event: dict[str, Any]) -> str:
    workflow = event.get("workflow")
    if not isinstance(workflow, dict):
        return ""
    summary = workflow.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    result = workflow.get("result")
    if isinstance(result, str) and result.strip():
        return result.strip()
    return ""


def apply_cron_team_round_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    """Update round completion state from a team stream event."""
    event_type = str(event.get("event_type") or "").strip()
    if event_type == "workflow.updated":
        workflow = event.get("workflow")
        if isinstance(workflow, dict):
            state["workflow_started"] = True
            status = str(workflow.get("status") or "").strip().lower()
            if status == "completed":
                state["workflow_completed"] = True
            workflow_text = _extract_workflow_summary(event)
            if workflow_text:
                state["workflow_text"] = workflow_text
    elif event_type == "chat.final":
        if not _is_cron_leader_final_event(event):
            return
        content = event.get("content")
        if (
            isinstance(content, str)
            and content.strip()
            and not is_cron_leader_placeholder_text(content)
        ):
            state["leader_text"] = content.strip()
            if state.get("workflow_completed"):
                state["leader_final_after_workflow"] = True
            elif (
                not cron_team_round_has_open_tasks(state)
                and not cron_team_round_has_active_members(state)
            ):
                state["leader_final_seen"] = True
    elif event_type == "team.task":
        nested = event.get("event")
        if isinstance(nested, dict):
            _apply_team_task_event(state, nested)
    elif event_type == "team.member":
        nested = event.get("event")
        if isinstance(nested, dict):
            _apply_team_member_event(state, nested)
    elif event_type == "team.completed":
        state["team_round_completed"] = True
    elif event_type == "chat.error":
        error = event.get("error")
        if isinstance(error, str) and error.strip():
            state["leader_text"] = error.strip()
        state["leader_final_seen"] = True
        state["open_team_tasks"] = {}
        state["active_team_members"] = {}
