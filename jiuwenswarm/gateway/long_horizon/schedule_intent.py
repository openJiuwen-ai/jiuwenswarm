# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""I2a schedule intent DTO and Gateway-side apply (owns lh-* storage)."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from jiuwenswarm.gateway.cron.cron_expr import iso_to_seven_field_cron, normalize_cron_expr
from jiuwenswarm.gateway.long_horizon.cron_backend import CronBackend
from jiuwenswarm.gateway.long_horizon.job_tags import LONG_HORIZON_DESC_PREFIX

logger = logging.getLogger(__name__)

ScheduleOp = Literal["replace", "upsert", "remove"]


@dataclass
class ScheduleJob:
    job_id: str
    due_at: str
    timezone: str
    title: str
    stage_title: str
    tags: dict[str, str] = field(default_factory=dict)
    plan_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduleJob:
        raw = data if isinstance(data, dict) else {}
        tags = raw.get("tags") if isinstance(raw.get("tags"), dict) else {}
        return cls(
            job_id=str(raw.get("job_id") or "").strip(),
            due_at=str(raw.get("due_at") or "").strip(),
            timezone=str(raw.get("timezone") or "Asia/Shanghai").strip()
            or "Asia/Shanghai",
            title=str(raw.get("title") or "").strip(),
            stage_title=str(raw.get("stage_title") or "").strip(),
            tags={str(k): str(v) for k, v in tags.items()},
            plan_summary=str(raw.get("plan_summary") or "").strip(),
        )


@dataclass
class ScheduleIntent:
    type: str = "long_horizon.schedule_intent"
    op: ScheduleOp = "replace"
    task_id: str = ""
    jobs: list[ScheduleJob] = field(default_factory=list)
    remove_job_ids: list[str] = field(default_factory=list)
    targets: str = "web"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "op": self.op,
            "task_id": self.task_id,
            "jobs": [job.to_dict() for job in self.jobs],
            "remove_job_ids": list(self.remove_job_ids),
            "targets": self.targets,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduleIntent:
        raw = data if isinstance(data, dict) else {}
        op = str(raw.get("op") or "replace").strip().lower()
        if op not in ("replace", "upsert", "remove"):
            op = "replace"
        jobs_raw = raw.get("jobs") if isinstance(raw.get("jobs"), list) else []
        remove_raw = (
            raw.get("remove_job_ids")
            if isinstance(raw.get("remove_job_ids"), list)
            else []
        )
        return cls(
            type=str(raw.get("type") or "long_horizon.schedule_intent"),
            op=op,  # type: ignore[arg-type]
            task_id=str(raw.get("task_id") or "").strip(),
            jobs=[
                ScheduleJob.from_dict(item)
                for item in jobs_raw
                if isinstance(item, dict)
            ],
            remove_job_ids=[
                str(item).strip() for item in remove_raw if str(item).strip()
            ],
            targets=str(raw.get("targets") or "web").strip() or "web",
        )


def job_cron_payload(job: ScheduleJob, *, targets: str) -> dict[str, Any]:
    tz = job.timezone or "Asia/Shanghai"
    cron_expr = normalize_cron_expr(iso_to_seven_field_cron(job.due_at, timezone=tz))
    task_id = str(job.tags.get("task_id") or "")
    stage_id = str(job.tags.get("stage_id") or "")
    summary = job.plan_summary or "请推进本阶段任务。"
    description = (
        f"{LONG_HORIZON_DESC_PREFIX}{task_id}][stage:{stage_id}] "
        f"长程阶段提醒：{job.title} · {job.stage_title} — {summary}"
    )
    name = f"长程·{job.title[:20]}·{job.stage_title[:16]}"
    return {
        "name": name[:64],
        "cron_expr": cron_expr,
        "timezone": tz,
        "description": description,
        "targets": targets or "web",
        "enabled": True,
        "expired": False,
        "delete_after_run": True,
        "wake_offset_seconds": 0,
        "mode": "agent",
        "session_id": None,
    }


async def apply_schedule_intent(
    intent: ScheduleIntent,
    backend: CronBackend,
    *,
    transactional: bool = False,
) -> None:
    """Write lh-* jobs onto a CronBackend. Caller reloads the live scheduler."""
    created: list[str] = []
    targets = intent.targets or "web"
    try:
        if intent.op in ("replace", "remove", "upsert"):
            for job_id in intent.remove_job_ids:
                try:
                    await backend.delete_job(job_id, force=True)
                except Exception as exc:
                    logger.debug(
                        "[long_horizon] remove job %s skipped: %s", job_id, exc
                    )
        if intent.op == "remove":
            return
        for job in intent.jobs:
            if not job.job_id or not job.due_at:
                continue
            payload = job_cron_payload(job, targets=targets)
            create_payload = {
                key: value for key, value in payload.items() if key != "expired"
            }
            existing = await backend.get_job(job.job_id)
            if existing is None:
                await backend.create_job(job_id=job.job_id, **create_payload)
                created.append(job.job_id)
            else:
                try:
                    await backend.update_job(job.job_id, payload)
                except KeyError:
                    await backend.create_job(job_id=job.job_id, **create_payload)
                    created.append(job.job_id)
    except Exception:
        if transactional:
            for job_id in created:
                try:
                    await backend.delete_job(job_id, force=True)
                except Exception as exc:
                    logger.debug(
                        "[long_horizon] rollback job %s skipped: %s", job_id, exc
                    )
        raise
