# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ScheduleIntent builder + SchedulePort (I2a): Agent proposes; Gateway stores."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jiuwenswarm.agents.harness.common.long_horizon.models import LongHorizonTask
from jiuwenswarm.gateway.long_horizon.cron_backend import CronBackend, CronControllerBackend
from jiuwenswarm.gateway.long_horizon.job_tags import stage_job_id
from jiuwenswarm.gateway.long_horizon.schedule_intent import (
    ScheduleIntent,
    ScheduleJob,
    ScheduleOp,
    apply_schedule_intent,
)

logger = logging.getLogger(__name__)

__all__ = [
    "LocalSchedulePort",
    "PushSchedulePort",
    "ScheduleIntent",
    "ScheduleJob",
    "ScheduleOp",
    "SchedulePort",
    "SchedulePushError",
    "apply_schedule_intent_locally",
    "build_schedule_intent",
    "resolve_schedule_port",
    "stamp_cron_job_ids",
    "sync_task_schedule",
]


apply_schedule_intent_locally = apply_schedule_intent


class SchedulePushError(RuntimeError):
    """I2a server_push was not delivered to Gateway."""


def _due_at_in_tz(due_at: str, tz: ZoneInfo) -> datetime | None:
    try:
        due = datetime.fromisoformat(str(due_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if due.tzinfo is None:
        return due.replace(tzinfo=tz)
    return due.astimezone(tz)


def build_schedule_intent(
    task: LongHorizonTask,
    *,
    op: ScheduleOp = "replace",
    targets: str = "web",
    ignore_task_status: bool = False,
    service_id: str = "",
    agent_id: str = "",
    transactional: bool = False,
) -> ScheduleIntent:
    """Pure function: which lh-* jobs should exist for this task."""
    jobs: list[ScheduleJob] = []
    remove: list[str] = []
    tz_name = task.anchor.timezone or "Asia/Shanghai"
    try:
        tz = ZoneInfo(tz_name)
    except (ValueError, ZoneInfoNotFoundError):
        tz = ZoneInfo("Asia/Shanghai")
        tz_name = "Asia/Shanghai"
    now = datetime.now(tz)
    task_schedulable = ignore_task_status or task.status == "active"
    for stage in task.stages:
        job_id = stage_job_id(task.id, stage.id)
        if (
            not task_schedulable
            or stage.status in ("done", "skipped", "in_progress", "due")
        ):
            remove.append(job_id)
            continue
        if not stage.due_at:
            continue
        due = _due_at_in_tz(stage.due_at, tz)
        if due is None or due <= now:
            # Overdue / unparsable: never submit a past one-shot cron.
            remove.append(job_id)
            continue
        summary = str(stage.plan or stage.hint or "").strip() or "请推进本阶段任务。"
        if len(summary) > 120:
            summary = summary[:119].rstrip() + "…"
        jobs.append(
            ScheduleJob(
                job_id=job_id,
                due_at=stage.due_at,
                timezone=tz_name,
                title=task.title,
                stage_title=stage.title,
                tags={"task_id": task.id, "stage_id": stage.id},
                plan_summary=summary,
            )
        )
    if op == "remove":
        return ScheduleIntent(
            op="remove",
            task_id=task.id,
            jobs=[],
            remove_job_ids=sorted({stage_job_id(task.id, s.id) for s in task.stages}),
            targets=targets,
            service_id=service_id,
            agent_id=agent_id,
            transactional=transactional,
        )
    return ScheduleIntent(
        op=op,
        task_id=task.id,
        jobs=jobs,
        remove_job_ids=remove if op == "replace" else [],
        targets=targets,
        service_id=service_id,
        agent_id=agent_id,
        transactional=transactional,
    )


def stamp_cron_job_ids(task: LongHorizonTask, intent: ScheduleIntent) -> None:
    keep = {job.job_id for job in intent.jobs}
    for stage in task.stages:
        jid = stage_job_id(task.id, stage.id)
        if jid in keep:
            stage.cron_job_id = jid
        elif jid in intent.remove_job_ids or intent.op == "remove":
            stage.cron_job_id = ""
        elif intent.op == "replace" and jid not in keep:
            stage.cron_job_id = ""


class SchedulePort(Protocol):
    async def apply(
        self, intent: ScheduleIntent, *, transactional: bool = False
    ) -> None:
        ...


class LocalSchedulePort:
    """Tests / injected FakeCron only. Production Agent uses PushSchedulePort."""

    def __init__(self, backend: CronBackend) -> None:
        self._backend = backend

    async def apply(
        self, intent: ScheduleIntent, *, transactional: bool = False
    ) -> None:
        await apply_schedule_intent(
            intent, self._backend, transactional=transactional
        )


class PushSchedulePort:
    """AgentServer → Gateway I2a via server_push."""

    def __init__(
        self,
        *,
        request_id: str = "",
        channel_id: str = "web",
        session_id: str = "",
        gateway_push: Any | None = None,
        service_id: str = "",
        agent_id: str = "",
    ) -> None:
        self._request_id = request_id
        self._channel_id = channel_id or "web"
        self._session_id = session_id
        self._gateway_push = gateway_push
        self._service_id = service_id
        self._agent_id = agent_id

    async def apply(
        self, intent: ScheduleIntent, *, transactional: bool = False
    ) -> None:
        from jiuwenswarm.common.e2a.constants import E2A_RESPONSE_KIND_LONG_HORIZON
        from jiuwenswarm.server.gateway_push import WebSocketGatewayPushTransport

        if transactional:
            intent.transactional = True
        if self._service_id and not intent.service_id:
            intent.service_id = self._service_id
        if self._agent_id and not intent.agent_id:
            intent.agent_id = self._agent_id
        transport = self._gateway_push or WebSocketGatewayPushTransport()
        payload = {
            "request_id": self._request_id or f"lh_sched_{intent.task_id}",
            "channel_id": self._channel_id,
            "session_id": self._session_id or None,
            "response_kind": E2A_RESPONSE_KIND_LONG_HORIZON,
            "body": intent.to_dict(),
        }
        delivered = await transport.send_push(payload)
        if delivered is not None and int(delivered) < 1:
            raise SchedulePushError(
                f"schedule_intent not delivered (subscribers={delivered}) "
                f"task={intent.task_id} op={intent.op}"
            )


def resolve_schedule_port(
    cron: CronBackend | None = None,
    *,
    service_id: str = "default",
    agent_id: str = "default",
    prefer_push: bool = False,
    request_id: str = "",
    channel_id: str = "web",
    session_id: str = "",
) -> SchedulePort:
    """Injected backend (tests) → Local; otherwise Push. Never open Gateway files."""
    _ = prefer_push
    if cron is not None:
        if all(
            callable(getattr(cron, name, None))
            for name in ("get_job", "create_job", "update_job", "delete_job")
        ) and hasattr(cron, "reload_scheduler"):
            return LocalSchedulePort(CronControllerBackend(cron))
        return LocalSchedulePort(cron)
    return PushSchedulePort(
        request_id=request_id,
        channel_id=channel_id,
        session_id=session_id,
        service_id=service_id,
        agent_id=agent_id,
    )


async def sync_task_schedule(
    task: LongHorizonTask,
    *,
    port: SchedulePort | None = None,
    cron: CronBackend | None = None,
    op: ScheduleOp = "replace",
    targets: str = "web",
    transactional: bool = False,
    ignore_task_status: bool = False,
    service_id: str = "default",
    agent_id: str = "default",
    prefer_push: bool = False,
) -> LongHorizonTask:
    intent = build_schedule_intent(
        task,
        op=op,
        targets=targets,
        ignore_task_status=ignore_task_status,
        service_id=service_id,
        agent_id=agent_id,
        transactional=transactional,
    )
    resolved = port or resolve_schedule_port(
        cron,
        service_id=service_id,
        agent_id=agent_id,
        prefer_push=prefer_push,
    )
    await resolved.apply(intent, transactional=transactional)
    stamp_cron_job_ids(task, intent)
    return task
