# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime: schedule via I2a port; mark_due for I2b. Does not write Gateway cron files."""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.agents.harness.common.long_horizon.core import (
    append_event,
    build_kick_query,
    clip,
    get_long_horizon_task,
    upsert_long_horizon_task,
)
from jiuwenswarm.agents.harness.common.long_horizon.models import (
    LongHorizonStage,
    LongHorizonTask,
)
from jiuwenswarm.gateway.long_horizon.cron_backend import CronBackend
from jiuwenswarm.gateway.long_horizon.job_tags import (
    EXEC_SESSION_PREFIX,
    LONG_HORIZON_DESC_PREFIX,
    STAGE_DUE_EVENT,
    is_long_horizon_cron_job,
    parse_long_horizon_description,
    stage_job_id,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CronBackend",
    "EXEC_SESSION_PREFIX",
    "LONG_HORIZON_DESC_PREFIX",
    "STAGE_DUE_EVENT",
    "build_due_payload",
    "build_stage_card_markdown",
    "cancel_long_horizon_crons",
    "compile_long_horizon_crons",
    "deliver_from_cron_job",
    "deliver_stage_reminder",
    "ensure_exec_session_id",
    "is_long_horizon_cron_job",
    "is_long_horizon_exec_session_id",
    "mark_stage_due",
    "parse_long_horizon_description",
    "schedule_stage_jobs_transactional",
    "stage_job_id",
]


async def compile_long_horizon_crons(
    task: LongHorizonTask,
    *,
    cron: CronBackend | None = None,
    targets: str = "web",
    service_id: str = "default",
    agent_id: str = "default",
    transactional: bool = False,
) -> LongHorizonTask:
    """Sync Gateway alarms for a task via SchedulePort (I2a)."""
    from jiuwenswarm.agents.harness.common.long_horizon.schedule_port import (
        sync_task_schedule,
    )

    return await sync_task_schedule(
        task,
        cron=cron,
        op="replace",
        targets=targets,
        transactional=transactional,
        service_id=service_id,
        agent_id=agent_id,
    )


async def schedule_stage_jobs_transactional(
    task: LongHorizonTask,
    *,
    cron: CronBackend | None = None,
    targets: str = "web",
    service_id: str = "default",
    agent_id: str = "default",
) -> LongHorizonTask:
    from jiuwenswarm.agents.harness.common.long_horizon.schedule_port import (
        sync_task_schedule,
    )

    return await sync_task_schedule(
        task,
        cron=cron,
        op="replace",
        targets=targets,
        transactional=True,
        ignore_task_status=True,
        service_id=service_id,
        agent_id=agent_id,
    )


async def cancel_long_horizon_crons(
    task: LongHorizonTask,
    *,
    cron: CronBackend | None = None,
    service_id: str = "default",
    agent_id: str = "default",
) -> None:
    from jiuwenswarm.agents.harness.common.long_horizon.schedule_port import (
        sync_task_schedule,
    )

    await sync_task_schedule(
        task,
        cron=cron,
        op="remove",
        transactional=False,
        service_id=service_id,
        agent_id=agent_id,
    )


def ensure_exec_session_id(task: LongHorizonTask) -> str:
    if task.exec_session_id.strip():
        return task.exec_session_id.strip()
    sid = f"{EXEC_SESSION_PREFIX}{task.id}"
    task.exec_session_id = sid
    return sid


def is_long_horizon_exec_session_id(session_id: str | None) -> bool:
    return str(session_id or "").strip().startswith(EXEC_SESSION_PREFIX)


def build_stage_card_markdown(task: LongHorizonTask, stage: LongHorizonStage) -> str:
    body = clip(str(stage.plan or stage.hint or "请推进本阶段任务。"), 400)
    return (
        f"**{task.title}** · 阶段 **{stage.title}**\n\n"
        f"{body}\n\n"
        f"- 计划时间：`{stage.due_at or '未设置'}`\n"
        f"- 操作：马上做 / 稍后做 / 跳过"
    )


def build_due_payload(
    task: LongHorizonTask,
    stage: LongHorizonStage,
    *,
    kick_query: str = "",
) -> dict[str, Any]:
    return {
        "event_type": STAGE_DUE_EVENT,
        "task_id": task.id,
        "stage_id": stage.id,
        "exec_session_id": task.exec_session_id,
        "title": task.title,
        "stage_title": stage.title,
        "due_at": stage.due_at,
        "hint": stage.hint,
        "plan": stage.plan,
        "content": build_stage_card_markdown(task, stage),
        "kick_query": kick_query or None,
    }


async def _ensure_session_metadata(
    task: LongHorizonTask, *, channel_id: str = "web"
) -> None:
    try:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            init_session_metadata,
            update_session_metadata,
        )

        title = f"长程·{task.title}"[:64]
        meta = get_session_metadata(task.exec_session_id, enable_writeback=False)
        if not meta:
            init_session_metadata(
                session_id=task.exec_session_id,
                channel_id=channel_id,
                mode="agent",
                title=title,
            )
        try:
            update_session_metadata(
                session_id=task.exec_session_id,
                title=title,
                touch_last_message_at=True,
            )
        except Exception as exc:
            logger.debug("[long_horizon] session title touch skipped: %s", exc)
    except Exception as exc:
        logger.warning("[long_horizon] session metadata failed: %s", exc)


async def mark_stage_due(
    workspace: str | Any,
    task_id: str,
    stage_id: str,
    *,
    job_id: str = "",
) -> dict[str, Any]:
    """I2b domain: mark stage due and return Toast fields (no LLM wake)."""
    _ = job_id
    task = get_long_horizon_task(workspace, task_id)
    if task is None:
        return {"success": False, "error": "task_not_found"}
    if task.status != "active":
        return {"success": False, "error": "task_not_active"}

    stage = next((s for s in task.stages if s.id == stage_id), None)
    if stage is None:
        return {"success": False, "error": "stage_not_found"}
    if stage.status in ("done", "skipped", "in_progress"):
        return {"success": False, "error": f"stage_already_{stage.status}"}

    already_due = stage.status == "due"
    stage.status = "due"
    ensure_exec_session_id(task)
    upsert_long_horizon_task(workspace, task)
    if not already_due:
        append_event(
            workspace, task_id=task.id, stage_id=stage.id, action="due"
        )

    kick_query = build_kick_query(task, stage)
    payload = build_due_payload(task, stage, kick_query=kick_query)
    return {
        "success": True,
        **payload,
        "content": build_stage_card_markdown(task, stage),
        "kick_query": kick_query,
    }


async def deliver_stage_reminder(
    workspace: str | Any,
    task_id: str,
    stage_id: str,
    *,
    channel_id: str = "web",
    message_handler: Any | None = None,
    job_id: str = "",
) -> dict[str, Any]:
    """I2b mark_due. Toast broadcast is Gateway I1d after RPC."""
    _ = message_handler
    result = await mark_stage_due(
        workspace, task_id, stage_id, job_id=job_id
    )
    if not result.get("success"):
        return result
    task = get_long_horizon_task(workspace, task_id)
    if task is not None:
        await _ensure_session_metadata(task, channel_id=channel_id)
    await _push_due_toast(result, channel_id=channel_id)
    return result


async def _push_due_toast(payload: dict[str, Any], *, channel_id: str) -> None:
    """I1d via server_push so overdue confirm toasts without waiting for inbox."""
    try:
        from jiuwenswarm.common.e2a.constants import E2A_RESPONSE_KIND_LONG_HORIZON
        from jiuwenswarm.server.gateway_push import WebSocketGatewayPushTransport

        body = {
            **payload,
            "type": STAGE_DUE_EVENT,
            "event_type": STAGE_DUE_EVENT,
        }
        await WebSocketGatewayPushTransport().send_push(
            {
                "request_id": (
                    f"lh_due_{payload.get('task_id')}_{payload.get('stage_id')}"
                ),
                "channel_id": channel_id or "web",
                "session_id": payload.get("exec_session_id") or None,
                "response_kind": E2A_RESPONSE_KIND_LONG_HORIZON,
                "body": body,
            }
        )
    except Exception as exc:
        logger.debug("[long_horizon] due toast push skipped: %s", exc)


async def deliver_from_cron_job(
    job: Any,
    *,
    workspace: str | Any,
    message_handler: Any | None = None,
) -> dict[str, Any]:
    desc = str(getattr(job, "description", "") or "")
    job_id = str(getattr(job, "id", "") or "")
    if isinstance(job, dict):
        desc = str(job.get("description") or desc)
        job_id = str(job.get("id") or job_id)
    parsed = parse_long_horizon_description(desc)
    if not parsed:
        return {"success": False, "error": "not_long_horizon_job"}
    task_id, stage_id = parsed
    targets = str(getattr(job, "targets", "") or "web").strip() or "web"
    if isinstance(job, dict):
        targets = str(job.get("targets") or targets).strip() or "web"
    return await deliver_stage_reminder(
        workspace,
        task_id,
        stage_id,
        channel_id=targets,
        message_handler=message_handler,
        job_id=job_id,
    )
