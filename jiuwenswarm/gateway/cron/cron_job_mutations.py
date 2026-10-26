"""CronJob create/update mutations shared by file, Redis and Gateway DB stores."""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import Any

from jiuwenswarm.gateway.cron.enterprise_gate import strip_sticky_identity_fields
from jiuwenswarm.gateway.cron.models import (
    CRON_JOB_DEFAULT_MODE,
    CronJob,
    normalize_cron_job_mode,
    normalize_cron_job_timeout_seconds,
)
from jiuwenswarm.common.work_mode import DEFAULT_WEB_WORK_MODE, normalize_work_mode


def sort_cron_jobs(jobs: list[CronJob]) -> list[CronJob]:
    jobs.sort(key=lambda j: (j.updated_at or 0.0, j.created_at or 0.0), reverse=True)
    return jobs


def _opt_str(value: Any) -> str | None:
    return str(value).strip() if isinstance(value, str) and str(value).strip() else None


def build_new_cron_job(
    *,
    job_id: str | None = None,
    name: str,
    cron_expr: str,
    timezone: str,
    description: str,
    targets: str,
    enabled: bool = True,
    wake_offset_seconds: int | None = None,
    session_id: str | None = None,
    chat_type: str | None = None,
    mode: str | None = None,
    delete_after_run: bool | None = None,
    group_id: str | None = None,
    bot_id: str | None = None,
    user_id: str | None = None,
    timeout_seconds: int | None = None,
    project_id: str = "",
    model_name: str | None = None,
    app_id: str = "",
    work_mode: str = DEFAULT_WEB_WORK_MODE,
) -> CronJob:
    now = time.time()
    sid = _opt_str(session_id)
    ct = _opt_str(chat_type)
    m = (
        normalize_cron_job_mode(mode)
        if mode is not None and str(mode).strip()
        else CRON_JOB_DEFAULT_MODE
    )
    dar = bool(delete_after_run) if delete_after_run is not None else False
    timeout = (
        normalize_cron_job_timeout_seconds(timeout_seconds)
        if timeout_seconds is not None
        else None
    )
    pid = str(project_id).strip() if isinstance(project_id, str) and project_id.strip() else ""
    model_name_val = _opt_str(model_name)
    job = CronJob(
        id=str(job_id or "").strip() or uuid.uuid4().hex,
        name=str(name or "").strip(),
        enabled=bool(enabled),
        cron_expr=str(cron_expr or "").strip(),
        timezone=str(timezone or "").strip(),
        wake_offset_seconds=int(wake_offset_seconds) if wake_offset_seconds is not None else 0,
        description=str(description or ""),
        targets=str(targets or "").strip(),
        session_id=sid,
        created_at=now,
        updated_at=now,
        chat_type=ct,
        mode=m,
        delete_after_run=dar,
        group_id=_opt_str(group_id),
        bot_id=_opt_str(bot_id),
        user_id=_opt_str(user_id),
        timeout_seconds=timeout,
        project_id=pid,
        model_name=model_name_val,
        app_id=str(app_id or "").strip(),
        work_mode=normalize_work_mode(work_mode, default=DEFAULT_WEB_WORK_MODE),
    )
    CronJob.from_dict(job.to_dict())
    return job


def apply_cron_job_patch(existing: CronJob, patch: dict[str, Any]) -> CronJob:
    patch = strip_sticky_identity_fields(dict(patch or {}))
    updated = existing
    if "name" in patch:
        updated = replace(updated, name=str(patch.get("name") or "").strip())
    if "enabled" in patch:
        enabled_val = bool(patch.get("enabled"))
        updated = replace(updated, enabled=enabled_val)
        if enabled_val and "expired" not in patch:
            updated = replace(updated, expired=False)
    if "cron_expr" in patch:
        updated = replace(updated, cron_expr=str(patch.get("cron_expr") or "").strip())
        if "expired" not in patch:
            updated = replace(updated, expired=False)
    if "timezone" in patch:
        updated = replace(updated, timezone=str(patch.get("timezone") or "").strip())
    if "wake_offset_seconds" in patch:
        raw = patch.get("wake_offset_seconds")
        try:
            wos = int(raw)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("wake_offset_seconds must be int") from exc
        updated = replace(updated, wake_offset_seconds=max(0, wos))
    if "description" in patch:
        updated = replace(updated, description=str(patch.get("description") or ""))
    if "targets" in patch:
        updated = replace(updated, targets=str(patch.get("targets") or "").strip())
    if "session_id" in patch:
        raw_sid = patch.get("session_id")
        new_sid = str(raw_sid).strip() if isinstance(raw_sid, str) and str(raw_sid).strip() else None
        updated = replace(updated, session_id=new_sid)
    if "chat_type" in patch:
        raw_ct = patch.get("chat_type")
        new_ct = str(raw_ct).strip() if isinstance(raw_ct, str) and str(raw_ct).strip() else None
        updated = replace(updated, chat_type=new_ct)
    if "expired" in patch:
        updated = replace(updated, expired=bool(patch.get("expired")))
    if "mode" in patch:
        updated = replace(updated, mode=normalize_cron_job_mode(patch.get("mode")))
    if "delete_after_run" in patch:
        updated = replace(updated, delete_after_run=bool(patch.get("delete_after_run")))
    if "timeout_seconds" in patch:
        raw_timeout = patch.get("timeout_seconds")
        if raw_timeout is None:
            updated = replace(updated, timeout_seconds=None)
        else:
            updated = replace(
                updated,
                timeout_seconds=normalize_cron_job_timeout_seconds(raw_timeout),
            )
    if "project_id" in patch:
        raw_pid = patch.get("project_id")
        new_pid = str(raw_pid).strip() if isinstance(raw_pid, str) and raw_pid.strip() else ""
        updated = replace(updated, project_id=new_pid)
    if "last_session_id" in patch:
        raw_lsid = patch.get("last_session_id")
        updated = replace(updated, last_session_id=_opt_str(raw_lsid))
    if "model_name" in patch:
        updated = replace(updated, model_name=_opt_str(patch.get("model_name")))
    if "app_id" in patch:
        updated = replace(updated, app_id=str(patch.get("app_id") or "").strip())
    if "work_mode" in patch:
        updated = replace(
            updated,
            work_mode=normalize_work_mode(patch.get("work_mode"), default=DEFAULT_WEB_WORK_MODE),
        )
    if "service_id" in patch:
        updated = replace(
            updated,
            service_id=str(patch.get("service_id") or "default").strip() or "default",
        )
    if "agent_id" in patch:
        updated = replace(
            updated,
            agent_id=str(patch.get("agent_id") or "default").strip() or "default",
        )
    updated.updated_at = time.time()
    CronJob.from_dict(updated.to_dict())
    return updated
