# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Heartbeat domain controller owned by the AgentServer Rail runtime.

职责:
  - 业务 API: create/update/delete/toggle/list/get/preview/run_now/cancel。
  - source 审计:controller 创建/更新时强制写入并校验 metadata.source 枚举。
  - 资源限制校验: min_interval / max_active_jobs_per_session / max_active_jobs_global。
  - 禁止字段拦截: mode/model/approval/sandbox/worktree 不可传入、不可修改。

"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import replace
from typing import Any

from jiuwenswarm.agents.harness.code.rails.heartbeat.models import (
    DEFAULT_CONCURRENCY_POLICY,
    DEFAULT_MAX_RUNS,
    DEFAULT_SESSION_DELETED_POLICY,
    DEFAULT_TIMEZONE,
    HEARTBEAT_CONCURRENCY_POLICIES,
    HEARTBEAT_NAME_MAX_LENGTH,
    HEARTBEAT_PROMPT_MAX_LENGTH,
    HEARTBEAT_SCHEDULE_TYPES,
    HEARTBEAT_SESSION_DELETED_POLICIES,
    HEARTBEAT_SOURCES,
    HEARTBEAT_STATUSES,
    HeartbeatJob,
    HeartbeatSchedule,
    MAX_UNIX_TIMESTAMP_SECONDS,
    MIN_INTERVAL_SECONDS,
    SCHEDULE_ONCE,
    SOURCE_WEB_RPC,
    STATUS_RUNNING,
    STATUS_SCHEDULED,
    validate_metadata_source,
)
from jiuwenswarm.agents.harness.code.rails.heartbeat.scheduler import HeartbeatSchedulerService
from jiuwenswarm.agents.harness.code.rails.heartbeat.store import HeartbeatJobStore

logger = logging.getLogger(__name__)

# 禁止传入或修改的运行配置字段。
_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {"mode", "model", "model_name", "approval", "sandbox", "worktree", "work_mode"}
)

_CREATE_FIELDS: frozenset[str] = frozenset(
    {
        "name", "channel_id", "session_id", "prompt", "schedule", "timezone",
        "enabled", "concurrency_policy", "session_deleted_policy", "max_runs",
        "source",
    }
)
_UPDATE_FIELDS: frozenset[str] = frozenset(
    {
        "name", "prompt", "schedule", "timezone", "enabled",
        "concurrency_policy", "session_deleted_policy", "max_runs",
    }
)

# 资源限制默认值，可被 config 覆盖。
_DEFAULT_LIMITS: dict[str, Any] = {
    "min_interval_seconds": MIN_INTERVAL_SECONDS,
    "max_active_jobs_per_session": 5,
    "max_active_jobs_global": 100,
    "default_max_runs": DEFAULT_MAX_RUNS,
    "default_concurrency_policy": DEFAULT_CONCURRENCY_POLICY,
    "default_session_deleted_policy": DEFAULT_SESSION_DELETED_POLICY,
}


class HeartbeatController:
    """High-level API owned by the process-level Heartbeat Rail runtime."""

    def __init__(
        self,
        *,
        store: HeartbeatJobStore,
        scheduler: HeartbeatSchedulerService,
        limits: dict[str, Any] | None = None,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._limits = self._normalize_limits({**_DEFAULT_LIMITS, **(limits or {})})
        self._validate_limits(self._limits)
        self._scheduler.set_limits(self._limits)

    def set_limits(self, limits: dict[str, Any]) -> None:
        merged = self._normalize_limits({**self._limits, **(limits or {})})
        self._validate_limits(merged)
        self._limits = merged
        self._scheduler.set_limits(merged)

    @staticmethod
    def _normalize_limits(limits: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(limits)
        for key in (
            "min_interval_seconds",
            "max_active_jobs_per_session",
            "max_active_jobs_global",
        ):
            try:
                normalized[key] = int(normalized.get(key))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must be integer") from exc
        default_max = normalized.get("default_max_runs")
        if isinstance(default_max, str) and default_max.strip().lower() in {
            "",
            "none",
            "null",
        }:
            default_max = None
        if default_max is not None:
            try:
                default_max = int(default_max)
            except (TypeError, ValueError) as exc:
                raise ValueError("default_max_runs must be null or integer") from exc
        normalized["default_max_runs"] = default_max
        return normalized

    @staticmethod
    def _validate_limits(limits: dict[str, Any]) -> None:
        for key in (
            "min_interval_seconds",
            "max_active_jobs_per_session",
            "max_active_jobs_global",
        ):
            try:
                value = int(limits.get(key))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must be integer") from exc
            minimum = MIN_INTERVAL_SECONDS if key == "min_interval_seconds" else 1
            if value < minimum:
                raise ValueError(f"{key} must be at least {minimum}")
        default_max = limits.get("default_max_runs")
        if default_max is not None and int(default_max) < 1:
            raise ValueError("default_max_runs must be null or at least 1")
        if limits.get("default_concurrency_policy") not in HEARTBEAT_CONCURRENCY_POLICIES:
            raise ValueError("invalid default_concurrency_policy")
        if limits.get("default_session_deleted_policy") not in HEARTBEAT_SESSION_DELETED_POLICIES:
            raise ValueError("invalid default_session_deleted_policy")

    @property
    def limits(self) -> dict[str, Any]:
        return dict(self._limits)

    # ---- 校验辅助 ----

    @staticmethod
    def _check_forbidden(params: dict[str, Any], *, where: str) -> None:
        """禁止字段拦截:mode/model/approval/sandbox/worktree 不可传入。"""
        bad = [k for k in params if k in _FORBIDDEN_FIELDS]
        if bad:
            raise ValueError(
                f"{where}: forbidden runtime config fields {bad}; "
                "heartbeat jobs must not change agent mode/model/approval/sandbox/worktree"
            )

    @staticmethod
    def _check_known_fields(
        params: dict[str, Any], *, allowed: frozenset[str], where: str
    ) -> None:
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise ValueError(f"{where}: unknown fields {unknown}")

    @staticmethod
    def _strict_bool(value: Any, *, field: str) -> bool:
        if not isinstance(value, bool):
            raise ValueError(f"{field} must be boolean")
        return value

    # ---- Web/RPC 业务 API ----

    async def list_jobs(
        self,
        params: dict[str, Any] | None = None,
        *,
        access_session_id: str | None = None,
        allow_all_visible: bool = False,
    ) -> dict[str, Any]:
        params = dict(params or {})
        self._check_forbidden(params, where="list_jobs")
        jobs = await self._store.list_jobs()
        # 过滤
        session_id = str(params.get("session_id") or "").strip()
        channel_id = str(params.get("channel_id") or "").strip()
        status = str(params.get("status") or "").strip()
        scope = str(params.get("scope") or "current").strip()
        if scope not in {"current", "all_visible"}:
            raise ValueError("scope must be current or all_visible")
        if access_session_id:
            if scope == "all_visible":
                if not allow_all_visible:
                    raise PermissionError("heartbeat.jobs.all permission required")
            else:
                session_id = access_session_id
        active_jobs = [
            job
            for job in jobs
            if job.enabled and job.status in {STATUS_SCHEDULED, STATUS_RUNNING}
        ]
        global_active_count = len(active_jobs)
        global_active_limit = int(self._limits.get("max_active_jobs_global", 100))
        if session_id:
            active_count = sum(
                job.session_id == session_id for job in active_jobs
            )
            active_limit = int(
                self._limits.get("max_active_jobs_per_session", 5)
            )
        else:
            active_count = global_active_count
            active_limit = global_active_limit
        out = []
        for j in sorted(jobs, key=lambda item: (item.created_at or 0.0, item.id)):
            if session_id and j.session_id != session_id:
                continue
            if channel_id and j.channel_id != channel_id:
                continue
            if status and j.status != status:
                continue
            out.append(j.to_dict())
        return {
            "jobs": out,
            "active_count": active_count,
            "active_limit": active_limit,
            "can_create": (
                active_count < active_limit
                and global_active_count < global_active_limit
            ),
        }

    async def _owned_job(
        self, job_id: str, access_session_id: str | None
    ) -> HeartbeatJob | None:
        job = await self._store.get_job(str(job_id or "").strip())
        if (
            job is not None
            and access_session_id
            and job.session_id != str(access_session_id).strip()
        ):
            raise PermissionError("job belongs to another session")
        return job

    async def get_job(
        self, job_id: str, *, access_session_id: str | None = None
    ) -> dict[str, Any] | None:
        job = await self._owned_job(job_id, access_session_id)
        return job.to_dict() if job is not None else None

    async def create_job(
        self,
        params: dict[str, Any],
        *,
        identity_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a job after the Rail service has resolved its identity."""
        params = dict(params or {})
        self._check_forbidden(params, where="create_job")
        self._check_known_fields(params, allowed=_CREATE_FIELDS, where="create_job")

        name = str(params.get("name") or "").strip()
        channel_id = str(params.get("channel_id") or "").strip()
        session_id = str(params.get("session_id") or "").strip()
        prompt = str(params.get("prompt") or "").strip()
        if not name:
            raise ValueError("name is required")
        if len(name) > HEARTBEAT_NAME_MAX_LENGTH:
            raise ValueError(f"name must be at most {HEARTBEAT_NAME_MAX_LENGTH} characters")
        if not channel_id:
            raise ValueError("channel_id is required")
        if not session_id:
            raise ValueError("session_id is required")
        if not prompt:
            raise ValueError("prompt is required")
        if len(prompt) > HEARTBEAT_PROMPT_MAX_LENGTH:
            raise ValueError(f"prompt must be at most {HEARTBEAT_PROMPT_MAX_LENGTH} characters")

        schedule = HeartbeatSchedule.from_dict(
            params.get("schedule") or {},
            default_timezone=str(params.get("timezone") or DEFAULT_TIMEZONE),
        )

        source = str(params.get("source") or SOURCE_WEB_RPC).strip()
        source = validate_metadata_source(source)  # controller 强制校验枚举

        max_runs_raw = params.get("max_runs", self._limits.get("default_max_runs", DEFAULT_MAX_RUNS))
        try:
            max_runs = None if max_runs_raw is None else int(max_runs_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_runs must be int or null") from exc
        if max_runs is not None and max_runs < 1:
            raise ValueError("max_runs must be at least 1")

        concurrency_policy = str(
            params.get("concurrency_policy")
            or self._limits.get("default_concurrency_policy", DEFAULT_CONCURRENCY_POLICY)
        )
        session_deleted_policy = str(
            params.get("session_deleted_policy")
            or self._limits.get("default_session_deleted_policy", DEFAULT_SESSION_DELETED_POLICY)
        )
        if concurrency_policy not in HEARTBEAT_CONCURRENCY_POLICIES:
            raise ValueError(f"invalid concurrency_policy {concurrency_policy!r}")
        if session_deleted_policy not in HEARTBEAT_SESSION_DELETED_POLICIES:
            raise ValueError(f"invalid session_deleted_policy {session_deleted_policy!r}")
        enabled = self._strict_bool(params.get("enabled", True), field="enabled")

        # 资源限制
        self._check_resource_limits_sync(session_id=session_id, schedule=schedule)
        await self._check_resource_limits_async(
            session_id=session_id, schedule=schedule, exclude_job_id=None
        )

        if schedule.type == SCHEDULE_ONCE and schedule.run_at is not None:
            if (
                not math.isfinite(schedule.run_at)
                or schedule.run_at > MAX_UNIX_TIMESTAMP_SECONDS
            ):
                raise ValueError(
                    "schedule.run_at must be a finite Unix timestamp in seconds; "
                    "milliseconds are not accepted"
                )
            if schedule.run_at <= time.time():
                raise ValueError("schedule.run_at must be in the future")

        job = await self._store.create_job(
            name=name,
            channel_id=channel_id,
            session_id=session_id,
            prompt=prompt,
            schedule=schedule,
            timezone=str(params.get("timezone") or DEFAULT_TIMEZONE),
            enabled=enabled,
            concurrency_policy=concurrency_policy,
            session_deleted_policy=session_deleted_policy,
            max_runs=max_runs,
            source=source,
            metadata=dict(identity_metadata or {}),
            max_active_jobs_per_session=int(
                self._limits.get("max_active_jobs_per_session", 5)
            ),
            max_active_jobs_global=int(
                self._limits.get("max_active_jobs_global", 100)
            ),
        )
        # 算 next_run_at 并回填
        now = time.time()
        next_run_at = self._scheduler.compute_next_run(job, now)
        if next_run_at is not None and job.enabled:
            job = await self._store.update_job(job.id, {"next_run_at": next_run_at})
        await self._scheduler.reload()
        return job.to_dict()

    async def update_job(
        self,
        job_id: str,
        patch: dict[str, Any],
        *,
        access_session_id: str | None = None,
    ) -> dict[str, Any]:
        patch = dict(patch or {})
        self._check_forbidden(patch, where="update_job")
        self._check_known_fields(patch, allowed=_UPDATE_FIELDS, where="update_job")
        existing = await self._owned_job(job_id, access_session_id)
        if existing is None:
            raise KeyError("job not found")

        if "enabled" in patch:
            patch["enabled"] = self._strict_bool(patch["enabled"], field="enabled")

        if patch.get("enabled") is True:
            target_max_runs = patch.get("max_runs", existing.max_runs)
            if (
                target_max_runs is not None
                and int(existing.run_count) >= int(target_max_runs)
            ):
                raise ValueError(
                    "max_runs already reached; increase or clear max_runs before enabling"
                )

        # schedule 变更要校验
        if "schedule" in patch:
            new_sched = HeartbeatSchedule.from_dict(
                patch["schedule"], default_timezone=existing.timezone
            )
            self._check_resource_limits_sync(
                session_id=existing.session_id, schedule=new_sched
            )

        # 任何影响调度的更新都必须基于当前时间重算，禁止沿用旧 schedule 的 due time。
        now = time.time()
        target_enabled = patch.get("enabled", existing.enabled)
        if target_enabled and not existing.enabled:
            activation_schedule = HeartbeatSchedule.from_dict(
                patch.get("schedule", existing.schedule.to_dict()),
                default_timezone=str(patch.get("timezone") or existing.timezone),
            )
            await self._check_resource_limits_async(
                session_id=existing.session_id,
                schedule=activation_schedule,
                exclude_job_id=existing.id,
            )
        schedule_changed = "schedule" in patch or "timezone" in patch
        terminal_job_reactivated = existing.status in {
            "completed",
            "expired",
            "disabled",
        }
        if target_enabled and (schedule_changed or terminal_job_reactivated):
            merged_sched = HeartbeatSchedule.from_dict(
                patch.get("schedule", existing.schedule.to_dict()),
                default_timezone=str(patch.get("timezone") or existing.timezone),
            )
            next_run_at = self._scheduler.compute_next_run(
                replace(existing, schedule=merged_sched), now
            )
            if next_run_at is None:
                raise ValueError("schedule has no future run; update run_at before enabling")
            patch["next_run_at"] = next_run_at

        updated = await self._store.update_job(str(job_id), patch)
        await self._scheduler.reload()
        return updated.to_dict()

    async def delete_job(
        self, job_id: str, *, access_session_id: str | None = None
    ) -> dict[str, Any]:
        job_id = str(job_id or "").strip()
        # 若有活跃 run,先取消(ghost 清理)
        existing = await self._owned_job(job_id, access_session_id)
        if existing is None:
            raise KeyError("job not found")
        if existing is not None and existing.run_state.current_run_id:
            cancel_result = await self._scheduler.cancel_run(
                job_id, pause_schedule=True
            )
            if cancel_result.get("cancel_status") == "failed":
                raise RuntimeError(
                    "cannot delete heartbeat job while its active run could not be cancelled"
                )
        deleted = await self._store.delete_job(job_id)
        await self._scheduler.reload()
        return {"deleted": bool(deleted)}

    async def toggle_job(
        self,
        job_id: str,
        enabled: bool,
        *,
        access_session_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.update_job(
            job_id,
            {"enabled": self._strict_bool(enabled, field="enabled")},
            access_session_id=access_session_id,
        )

    async def preview_job(
        self,
        job_id: str,
        count: int = 5,
        *,
        access_session_id: str | None = None,
    ) -> dict[str, Any]:
        job = await self._owned_job(job_id, access_session_id)
        if job is None:
            raise KeyError("job not found")
        nxt = self._scheduler.preview_next_runs(job, count=count)
        return {"next": nxt}

    async def run_now(
        self,
        job_id: str,
        *,
        reschedule: bool = False,
        access_session_id: str | None = None,
    ) -> dict[str, Any]:
        job = await self._owned_job(job_id, access_session_id)
        if job is None:
            raise KeyError("job not found")
        result = await self._scheduler.trigger_run_now(
            str(job_id or "").strip(), reschedule=reschedule
        )
        return result

    async def cancel_run(
        self,
        job_id: str,
        *,
        pause_schedule: bool = False,
        access_session_id: str | None = None,
    ) -> dict[str, Any]:
        job = await self._owned_job(job_id, access_session_id)
        if job is None:
            raise KeyError("job not found")
        return await self._scheduler.cancel_run(
            str(job_id or "").strip(), pause_schedule=pause_schedule
        )

    # ---- 资源限制同步校验(min_interval 不需 await) ----

    def _check_resource_limits_sync(
        self, *, session_id: str, schedule: HeartbeatSchedule
    ) -> None:
        min_iv = int(self._limits.get("min_interval_seconds", MIN_INTERVAL_SECONDS))
        if schedule.type == "interval":
            iv = schedule.interval_seconds or 0
            if iv < min_iv:
                raise ValueError(f"interval_seconds must be at least {min_iv}")

    async def _check_resource_limits_async(
        self, *, session_id: str, schedule: HeartbeatSchedule, exclude_job_id: str | None = None
    ) -> None:
        min_iv = int(self._limits.get("min_interval_seconds", MIN_INTERVAL_SECONDS))
        if schedule.type == "interval" and (schedule.interval_seconds or 0) < min_iv:
            raise ValueError(f"interval_seconds must be at least {min_iv}")
        max_per_session = int(self._limits.get("max_active_jobs_per_session", 5))
        cur_session = await self._store.count_active_jobs_for_session(session_id)
        if exclude_job_id:
            existing = await self._store.get_job(exclude_job_id)
            if (
                existing is not None
                and existing.session_id == session_id
                and existing.status == "scheduled"
            ):
                cur_session = max(0, cur_session - 1)
        if cur_session >= max_per_session:
            raise ValueError(
                f"max_active_jobs_per_session ({max_per_session}) exceeded"
            )
        max_global = int(self._limits.get("max_active_jobs_global", 100))
        cur_global = await self._store.count_active_jobs_global()
        if cur_global >= max_global:
            raise ValueError(f"max_active_jobs_global ({max_global}) exceeded")

    # ---- 元数据 ----

    def get_meta(self) -> dict[str, Any]:
        return {
            "limits": dict(self._limits),
            "schedule_types": list(HEARTBEAT_SCHEDULE_TYPES),
            "concurrency_policies": list(HEARTBEAT_CONCURRENCY_POLICIES),
            "session_deleted_policies": list(HEARTBEAT_SESSION_DELETED_POLICIES),
            "statuses": list(HEARTBEAT_STATUSES),
            "sources": list(HEARTBEAT_SOURCES),
            "run_count_semantics": "increments for succeeded and failed attempts only",
            "deprecated_fields": {},
        }
