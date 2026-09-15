# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""HeartbeatJobStore — 读写 heartbeat_jobs.json.

并发安全模式镜像 ``CronJobStore``:
  - ``asyncio.Lock``：同进程协程互斥；
  - ``portalocker`` 伴生 ``heartbeat_jobs.json.lock``：跨进程互斥。
  整个 read-modify-write 在双层锁内完成,避免 lost update。

store 层负责同步维护 ``status / enabled / next_run_at`` 三者状态机不变量，
禁止只改其中一个字段。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

import portalocker

from jiuwenswarm.common.utils import get_heartbeat_jobs_path
from jiuwenswarm.agents.harness.code.rails.heartbeat.models import (
    DEFAULT_CONCURRENCY_POLICY,
    DEFAULT_MAX_RUNS,
    DEFAULT_SESSION_DELETED_POLICY,
    DEFAULT_TIMEZONE,
    HEARTBEAT_JOBS_VERSION,
    HEARTBEAT_TERMINAL_STATUSES,
    HeartbeatJob,
    HeartbeatRunState,
    HeartbeatSchedule,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_SKIPPED,
    RUN_SUCCEEDED,
    SOURCE_AGENT_TOOL,
    STATUS_COMPLETED,
    STATUS_DISABLED,
    STATUS_EXPIRED,
    STATUS_RUNNING,
    STATUS_SCHEDULED,
    SCHEDULE_CRON,
    SCHEDULE_INTERVAL,
    SCHEDULE_ONCE,
    empty_heartbeat_jobs_doc,
    validate_metadata_source,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_FILE_LOCK_TIMEOUT_SEC = 10.0


class HeartbeatStoreDataError(ValueError):
    """The persisted Heartbeat store is unreadable or structurally invalid."""


class HeartbeatJobStore:
    """Persist heartbeat jobs to ``heartbeat_jobs.json``."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        file_lock_timeout: float = _FILE_LOCK_TIMEOUT_SEC,
    ) -> None:
        self._path = path or get_heartbeat_jobs_path()
        self._lock = asyncio.Lock()
        self._file_lock_timeout = float(file_lock_timeout)

    @property
    def path(self) -> Path:
        return self._path

    # ---- 双层锁(同进程 + 跨进程),镜像 CronJobStore ----

    def _call_under_file_lock(self, fn: Callable[[], _T]) -> _T:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with portalocker.Lock(str(lock_path), timeout=self._file_lock_timeout):
            return fn()

    async def _run_locked(self, fn: Callable[[], _T]) -> _T:
        async with self._lock:
            return await asyncio.to_thread(self._call_under_file_lock, fn)

    # ---- 原子读写 ----

    def _read_json_unlocked(self) -> dict[str, Any]:
        path = self._path
        try:
            if not path.exists():
                return empty_heartbeat_jobs_doc()
            raw = path.read_text(encoding="utf-8")
            if not raw.strip():
                raise HeartbeatStoreDataError(
                    f"heartbeat store is empty: {path}"
                )
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise HeartbeatStoreDataError(
                    f"heartbeat store root must be an object: {path}"
                )
            if "version" not in data:
                data["version"] = HEARTBEAT_JOBS_VERSION
            if "jobs" not in data:
                data["jobs"] = []
            elif not isinstance(data["jobs"], list):
                raise HeartbeatStoreDataError(
                    f"heartbeat store jobs must be an array: {path}"
                )
            return data
        except HeartbeatStoreDataError:
            logger.error("[HeartbeatStore] invalid persisted store: %s", path)
            raise
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(
                "[HeartbeatStore] failed to read persisted store %s: %s",
                path,
                exc,
            )
            raise HeartbeatStoreDataError(
                f"heartbeat store cannot be read safely: {path}"
            ) from exc

    def _write_json_unlocked(self, data: dict[str, Any]) -> None:
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        data["version"] = int(data.get("version") or HEARTBEAT_JOBS_VERSION)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)

    # ---- 基础 CRUD ----

    async def list_jobs(self) -> list[HeartbeatJob]:
        def _body() -> list[HeartbeatJob]:
            data = self._read_json_unlocked()
            jobs_raw = data.get("jobs") or []
            if not isinstance(jobs_raw, list):
                return []
            jobs: list[HeartbeatJob] = []
            for item in jobs_raw:
                if not isinstance(item, dict):
                    continue
                try:
                    jobs.append(HeartbeatJob.from_dict(item))
                except Exception as exc:
                    logger.warning(
                        "[HeartbeatStore] reject invalid job entry %s: %s",
                        item.get("id") if isinstance(item, dict) else item,
                        exc,
                    )
                    continue
            return jobs

        jobs = await self._run_locked(_body)
        jobs.sort(
            key=lambda j: (j.updated_at or 0.0, j.created_at or 0.0),
            reverse=True,
        )
        return jobs

    async def get_job(self, job_id: str) -> HeartbeatJob | None:
        job_id = str(job_id or "").strip()
        if not job_id:
            return None
        for job in await self.list_jobs():
            if job.id == job_id:
                return job
        return None

    async def _upsert_job(self, job: HeartbeatJob) -> None:
        def _body() -> None:
            data = self._read_json_unlocked()
            jobs_raw = data.get("jobs") or []
            if not isinstance(jobs_raw, list):
                jobs_raw = []
            out: list[dict[str, Any]] = []
            found = False
            for item in jobs_raw:
                if not isinstance(item, dict):
                    continue
                if str(item.get("id") or "").strip() == job.id:
                    out.append(job.to_dict())
                    found = True
                else:
                    out.append(item)
            if not found:
                out.append(job.to_dict())
            data["version"] = int(data.get("version") or HEARTBEAT_JOBS_VERSION)
            data["jobs"] = out
            self._write_json_unlocked(data)

        await self._run_locked(_body)

    async def _mutate_job(
        self,
        job_id: str,
        mutator: Callable[[HeartbeatJob], HeartbeatJob],
    ) -> HeartbeatJob:
        """Atomically read, mutate and write one job under the same file lock."""

        clean_id = str(job_id or "").strip()
        if not clean_id:
            raise ValueError("id is required")

        def _body() -> HeartbeatJob:
            data = self._read_json_unlocked()
            jobs_raw = data.get("jobs") or []
            if not isinstance(jobs_raw, list):
                jobs_raw = []
            out: list[dict[str, Any]] = []
            result: HeartbeatJob | None = None
            for item in jobs_raw:
                if not isinstance(item, dict):
                    continue
                if str(item.get("id") or "").strip() != clean_id:
                    out.append(item)
                    continue
                current = HeartbeatJob.from_dict(item)
                result = mutator(current)
                result.check_invariants()
                out.append(result.to_dict())
            if result is None:
                raise KeyError("job not found")
            data["jobs"] = out
            self._write_json_unlocked(data)
            return result

        return await self._run_locked(_body)

    async def claim_run(
        self,
        job_id: str,
        run_id: str,
        now: float,
        *,
        trigger: str,
        reschedule: bool,
        next_run_at_after_claim: float | None = None,
    ) -> tuple[str, HeartbeatJob, str | None]:
        """Atomically apply concurrency policy and claim a run.

        Returns ``(decision, job, replaced_run_id)`` where decision is one of
        ``run/skip/queued/coalesced/replace/replace_pending/completed``.
        """

        decision = "run"
        replaced_run_id: str | None = None

        def _claim(job: HeartbeatJob) -> HeartbeatJob:
            nonlocal decision, replaced_run_id
            active = job.run_state.current_run_id
            # Persisted files from an older version may still contain an
            # exhausted job as scheduled. Enforce max_runs under the same
            # read-modify-write lock as the claim so reload/tick and run_now
            # cannot race into one extra execution.
            if (
                job.max_runs is not None
                and int(job.run_count) >= int(job.max_runs)
            ):
                decision = "completed"
                if active:
                    # Do not rewrite an in-flight run as terminal. Refuse the
                    # additional claim; the existing run's completion path
                    # remains authoritative for final state cleanup.
                    return job
                return replace(
                    job,
                    enabled=False,
                    status=STATUS_COMPLETED,
                    next_run_at=None,
                    updated_at=float(now),
                )
            if active:
                if job.concurrency_policy == "queue":
                    if job.run_state.queued_run_id:
                        decision = "coalesced"
                        return job
                    decision = "queued"
                    return replace(
                        job,
                        run_state=replace(
                            job.run_state,
                            queued_run_id=run_id,
                            queued_trigger=trigger,
                            queued_reschedule=bool(reschedule),
                        ),
                        updated_at=float(now),
                    )
                if job.concurrency_policy == "replace":
                    if job.run_state.queued_run_id:
                        decision = "replace_pending"
                        return job
                    decision = "replace"
                    replaced_run_id = active
                    return replace(
                        job,
                        run_state=replace(
                            job.run_state,
                            queued_run_id=run_id,
                            queued_trigger=trigger,
                            queued_reschedule=bool(reschedule),
                        ),
                        updated_at=float(now),
                    )
                decision = "skip"
                return replace(
                    job,
                    last_run_at=float(now),
                    run_state=replace(
                        job.run_state,
                        last_run_status=RUN_SKIPPED,
                        last_error="previous_run_active",
                        skipped_count=int(job.run_state.skipped_count) + 1,
                    ),
                    updated_at=float(now),
                )

            return replace(
                job,
                status=STATUS_RUNNING,
                next_run_at=(
                    next_run_at_after_claim
                    if trigger == "scheduler"
                    else job.next_run_at
                ),
                run_state=replace(
                    job.run_state,
                    current_run_id=run_id,
                    current_run_started_at=float(now),
                    current_trigger=trigger,
                    current_reschedule=bool(reschedule),
                    resume_status=job.status,
                    resume_enabled=job.enabled,
                    resume_next_run_at=job.next_run_at,
                ),
                updated_at=float(now),
            )

        claimed = await self._mutate_job(job_id, _claim)
        return decision, claimed, replaced_run_id

    async def replace_claimed_run(
        self,
        job_id: str,
        *,
        expected_run_id: str,
        new_run_id: str,
        now: float,
        trigger: str,
        reschedule: bool,
        next_run_at_after_claim: float | None = None,
    ) -> tuple[bool, HeartbeatJob]:
        """Promote a reserved replacement after exact cancellation is confirmed."""

        promoted = False

        def _replace(job: HeartbeatJob) -> HeartbeatJob:
            nonlocal promoted
            rs = job.run_state
            if rs.queued_run_id != new_run_id:
                raise RuntimeError("replacement reservation changed")
            if rs.current_run_id not in {expected_run_id, None}:
                raise RuntimeError("active run changed while replacing")
            if (
                job.max_runs is not None
                and int(job.run_count) >= int(job.max_runs)
            ):
                return replace(
                    job,
                    enabled=False,
                    status=STATUS_COMPLETED,
                    next_run_at=None,
                    run_state=replace(
                        rs,
                        current_run_id=None,
                        current_run_started_at=None,
                        current_trigger=None,
                        current_reschedule=False,
                        queued_run_id=None,
                        queued_trigger=None,
                        queued_reschedule=False,
                    ),
                    updated_at=float(now),
                )
            if not job.enabled or job.status == STATUS_DISABLED:
                return replace(
                    job,
                    run_state=replace(
                        rs,
                        queued_run_id=None,
                        queued_trigger=None,
                        queued_reschedule=False,
                    ),
                    updated_at=float(now),
                )
            promoted = True
            if rs.current_run_id == expected_run_id:
                resume_status = rs.resume_status
                resume_enabled = rs.resume_enabled
                resume_next_run_at = rs.resume_next_run_at
            else:
                resume_status = job.status
                resume_enabled = job.enabled
                resume_next_run_at = job.next_run_at
            return replace(
                job,
                status=STATUS_RUNNING,
                next_run_at=(
                    next_run_at_after_claim
                    if trigger == "scheduler"
                    else job.next_run_at
                ),
                run_state=replace(
                    rs,
                    current_run_id=new_run_id,
                    current_run_started_at=float(now),
                    current_trigger=trigger,
                    current_reschedule=bool(reschedule),
                    resume_status=resume_status,
                    resume_enabled=resume_enabled,
                    resume_next_run_at=resume_next_run_at,
                    last_run_status=RUN_CANCELLED,
                    last_error="replaced",
                    queued_run_id=None,
                    queued_trigger=None,
                    queued_reschedule=False,
                ),
                updated_at=float(now),
            )

        result = await self._mutate_job(job_id, _replace)
        return promoted, result

    async def clear_replacement_reservation(
        self, job_id: str, replacement_run_id: str, *, now: float
    ) -> HeartbeatJob:
        """Clear only the matching queued replacement after cancellation failed."""

        def _clear(job: HeartbeatJob) -> HeartbeatJob:
            if job.run_state.queued_run_id != replacement_run_id:
                return job
            return replace(
                job,
                run_state=replace(
                    job.run_state,
                    queued_run_id=None,
                    queued_trigger=None,
                    queued_reschedule=False,
                ),
                updated_at=float(now),
            )

        return await self._mutate_job(job_id, _clear)

    async def finish_run(
        self,
        job_id: str,
        run_id: str,
        now: float,
        *,
        outcome: str,
        error: str | None,
        next_run_at: float | None,
        terminal: bool,
        pause_schedule: bool = False,
    ) -> tuple[bool, HeartbeatJob]:
        """Finish only the matching active run; stale completions are ignored."""

        matched = False

        def _finish(job: HeartbeatJob) -> HeartbeatJob:
            nonlocal matched
            rs = job.run_state
            if rs.current_run_id != run_id:
                return job
            matched = True
            increments = outcome in {RUN_SUCCEEDED, RUN_FAILED}
            run_count = int(job.run_count) + (1 if increments else 0)
            resume_status = rs.resume_status or STATUS_SCHEDULED
            resume_enabled = rs.resume_enabled if rs.resume_enabled is not None else True
            resume_next = rs.resume_next_run_at
            last_error = str(error)[:1000] if error else None
            new_rs = replace(
                rs,
                current_run_id=None,
                current_run_started_at=None,
                current_trigger=None,
                current_reschedule=False,
                resume_status=None,
                resume_enabled=None,
                resume_next_run_at=None,
                last_run_status=outcome,
                last_error=last_error,
            )
            if pause_schedule:
                return replace(
                    job,
                    status=STATUS_DISABLED,
                    enabled=False,
                    next_run_at=None,
                    last_run_at=float(now),
                    run_count=run_count,
                    run_state=replace(
                        new_rs,
                        queued_run_id=None,
                        queued_trigger=None,
                        queued_reschedule=False,
                    ),
                    updated_at=float(now),
                )
            # A user may disable the schedule while this run is active.  That
            # newer intent outranks both terminal rules and the start snapshot.
            if not job.enabled or job.status == STATUS_DISABLED:
                return replace(
                    job,
                    status=STATUS_DISABLED,
                    enabled=False,
                    next_run_at=None,
                    last_run_at=float(now),
                    run_count=run_count,
                    run_state=replace(
                        new_rs,
                        queued_run_id=None,
                        queued_trigger=None,
                        queued_reschedule=False,
                    ),
                    updated_at=float(now),
                )
            if terminal:
                return replace(
                    job,
                    status=STATUS_COMPLETED,
                    enabled=False,
                    next_run_at=None,
                    last_run_at=float(now),
                    run_count=run_count,
                    run_state=replace(
                        new_rs,
                        queued_run_id=None,
                        queued_trigger=None,
                        queued_reschedule=False,
                    ),
                    updated_at=float(now),
                )
            if rs.current_trigger == "scheduler" or rs.current_reschedule:
                if next_run_at is None:
                    return replace(
                        job,
                        status=STATUS_EXPIRED,
                        enabled=False,
                        next_run_at=None,
                        last_run_at=float(now),
                        run_count=run_count,
                        run_state=replace(
                            new_rs,
                            queued_run_id=None,
                            queued_trigger=None,
                            queued_reschedule=False,
                        ),
                        updated_at=float(now),
                    )
                return replace(
                    job,
                    status=STATUS_SCHEDULED,
                    enabled=True,
                    next_run_at=float(next_run_at),
                    last_run_at=float(now),
                    run_count=run_count,
                    run_state=new_rs,
                    updated_at=float(now),
                )
            if job.status == STATUS_RUNNING:
                resume_next = job.next_run_at
                resume_enabled = job.enabled
            # A due tick can consume the once slot during a manual run. Keep
            # that decision instead of restoring scheduled with no next run.
            # A non-null due time is still pending, even if it is in the past.
            if (
                job.schedule.type == SCHEDULE_ONCE
                and resume_status == STATUS_SCHEDULED
                and resume_next is None
            ):
                resume_status = STATUS_EXPIRED
                resume_enabled = False
                new_rs = replace(
                    new_rs,
                    queued_run_id=None,
                    queued_trigger=None,
                    queued_reschedule=False,
                )
            return replace(
                job,
                status=resume_status,
                enabled=bool(resume_enabled),
                next_run_at=resume_next,
                last_run_at=float(now),
                run_count=run_count,
                run_state=new_rs,
                updated_at=float(now),
            )

        result = await self._mutate_job(job_id, _finish)
        return matched, result

    async def defer_claimed_run_for_busy(
        self,
        job_id: str,
        run_id: str,
        *,
        now: float,
    ) -> tuple[bool, HeartbeatJob]:
        """Roll back an exact claim when the session became busy before dispatch."""
        matched = False

        def _defer(job: HeartbeatJob) -> HeartbeatJob:
            nonlocal matched
            rs = job.run_state
            if rs.current_run_id != run_id:
                return job
            matched = True
            cleared = replace(
                rs,
                current_run_id=None,
                current_run_started_at=None,
                current_trigger=None,
                current_reschedule=False,
                resume_status=None,
                resume_enabled=None,
                resume_next_run_at=None,
            )
            if not job.enabled or job.status == STATUS_DISABLED:
                return replace(
                    job,
                    status=STATUS_DISABLED,
                    enabled=False,
                    next_run_at=None,
                    run_state=cleared,
                    updated_at=float(now),
                )
            return replace(
                job,
                status=rs.resume_status or STATUS_SCHEDULED,
                enabled=(
                    rs.resume_enabled
                    if rs.resume_enabled is not None
                    else True
                ),
                next_run_at=rs.resume_next_run_at,
                run_state=cleared,
                updated_at=float(now),
            )

        result = await self._mutate_job(job_id, _defer)
        return matched, result

    async def pop_queued_run(
        self, job_id: str
    ) -> tuple[str, str, bool] | None:
        queued: tuple[str, str, bool] | None = None

        def _pop(job: HeartbeatJob) -> HeartbeatJob:
            nonlocal queued
            rs = job.run_state
            if not rs.queued_run_id:
                return job
            queued = (
                rs.queued_run_id,
                rs.queued_trigger or "scheduler",
                bool(rs.queued_reschedule),
            )
            return replace(
                job,
                run_state=replace(
                    rs,
                    queued_run_id=None,
                    queued_trigger=None,
                    queued_reschedule=False,
                ),
                updated_at=time.time(),
            )

        await self._mutate_job(job_id, _pop)
        return queued

    async def record_cancel_result(
        self,
        job_id: str,
        *,
        status: str,
        error: str | None,
        now: float,
    ) -> HeartbeatJob:
        """Persist the delivery result of the most recent exact cancellation."""

        def _record(job: HeartbeatJob) -> HeartbeatJob:
            return replace(
                job,
                run_state=replace(
                    job.run_state,
                    last_cancel_status=str(status),
                    last_cancel_error=(str(error)[:1000] if error else None),
                ),
                updated_at=float(now),
            )

        return await self._mutate_job(job_id, _record)

    async def skip_and_reschedule(
        self,
        job_id: str,
        *,
        now: float,
        reason: str,
        next_run_at: float | None,
    ) -> HeartbeatJob:
        """Atomically record a defensive skip and advance the due time."""

        def _skip(job: HeartbeatJob) -> HeartbeatJob:
            run_state = replace(
                job.run_state,
                last_run_status=RUN_SKIPPED,
                last_error=str(reason)[:1000],
                skipped_count=int(job.run_state.skipped_count) + 1,
            )
            if next_run_at is None and not job.run_state.current_run_id:
                return replace(
                    job,
                    enabled=False,
                    status=STATUS_EXPIRED,
                    next_run_at=None,
                    last_run_at=float(now),
                    run_state=run_state,
                    updated_at=float(now),
                )
            return replace(
                job,
                next_run_at=next_run_at,
                last_run_at=float(now),
                run_state=run_state,
                updated_at=float(now),
            )

        return await self._mutate_job(job_id, _skip)

    async def create_job(
        self,
        *,
        job_id: str | None = None,
        name: str,
        channel_id: str,
        session_id: str,
        prompt: str,
        schedule: HeartbeatSchedule,
        timezone: str = DEFAULT_TIMEZONE,
        enabled: bool = True,
        concurrency_policy: str = DEFAULT_CONCURRENCY_POLICY,
        session_deleted_policy: str = DEFAULT_SESSION_DELETED_POLICY,
        max_runs: int | None = DEFAULT_MAX_RUNS,
        source: str = SOURCE_AGENT_TOOL,
        metadata: dict[str, Any] | None = None,
        next_run_at: float | None = None,
        max_active_jobs_per_session: int | None = None,
        max_active_jobs_global: int | None = None,
        now: float | None = None,
    ) -> HeartbeatJob:
        """创建心跳任务。

        controller 负责按入口强制写入合法 ``source``(枚举校验);store 层兜底校验。
        """
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        ts = float(now) if now is not None else time.time()
        # source 校验:controller 应已校验,此处再校一遍防绕过。
        src = validate_metadata_source(source)

        meta = dict(metadata or {})
        meta["source"] = src

        job_id_clean = str(job_id or "").strip() or f"{_id_prefix()}{uuid.uuid4().hex}"

        resolved_next_run_at = next_run_at
        if enabled and resolved_next_run_at is None:
            if schedule.type == SCHEDULE_ONCE:
                resolved_next_run_at = schedule.run_at
            elif schedule.type == SCHEDULE_INTERVAL:
                resolved_next_run_at = ts + int(schedule.interval_seconds or 0)
            elif schedule.type == SCHEDULE_CRON:
                from datetime import datetime

                from .cron_schedule import next_cron_datetime

                tz = ZoneInfo(schedule.timezone or timezone or DEFAULT_TIMEZONE)
                resolved_next_run_at = next_cron_datetime(
                    schedule.cron_expr or "", datetime.fromtimestamp(ts, tz=tz)
                ).timestamp()

        # 过去的 once 已无可执行时间，直接进入 expired；其余 enabled job 必须有 next。
        once_expired = False
        if schedule.type == SCHEDULE_ONCE:
            once_expired = (
                resolved_next_run_at is None or float(resolved_next_run_at) <= ts
            )
        if enabled and once_expired:
            enabled = False
            status = STATUS_EXPIRED
            resolved_next_run_at = None
        elif enabled:
            status = STATUS_SCHEDULED
        else:
            status = STATUS_DISABLED

        job = HeartbeatJob(
            id=job_id_clean,
            name=str(name or "").strip(),
            enabled=enabled,
            channel_id=str(channel_id or "").strip(),
            session_id=str(session_id or "").strip(),
            prompt=str(prompt or "").strip(),
            schedule=schedule,
            timezone=str(timezone or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE,
            status=status,
            concurrency_policy=str(concurrency_policy or DEFAULT_CONCURRENCY_POLICY),
            session_deleted_policy=str(
                session_deleted_policy or DEFAULT_SESSION_DELETED_POLICY
            ),
            max_runs=max_runs,
            created_at=ts,
            updated_at=ts,
            next_run_at=(
                float(resolved_next_run_at)
                if enabled and resolved_next_run_at is not None
                else None
            ),
            last_run_at=None,
            run_count=0,
            metadata=meta,
            run_state=HeartbeatRunState(),
        )
        # 调度时间由 controller 在写入前计算，禁止持久化 scheduled + None 的僵尸状态。
        job.check_invariants()

        def _create() -> None:
            data = self._read_json_unlocked()
            jobs_raw = data.get("jobs") or []
            if not isinstance(jobs_raw, list):
                jobs_raw = []
            parsed: list[HeartbeatJob] = []
            for item in jobs_raw:
                if not isinstance(item, dict):
                    continue
                existing_id = str(item.get("id") or "").strip()
                if existing_id == job.id:
                    raise ValueError(f"job id already exists: {job.id}")
                try:
                    parsed.append(HeartbeatJob.from_dict(item))
                except ValueError:
                    continue
            if job.enabled:
                active = [
                    item
                    for item in parsed
                    if item.enabled and item.status in {STATUS_SCHEDULED, STATUS_RUNNING}
                ]
                if (
                    max_active_jobs_global is not None
                    and len(active) >= int(max_active_jobs_global)
                ):
                    raise ValueError(
                        f"max_active_jobs_global ({max_active_jobs_global}) exceeded"
                    )
                active_session = [
                    item for item in active if item.session_id == job.session_id
                ]
                if (
                    max_active_jobs_per_session is not None
                    and len(active_session) >= int(max_active_jobs_per_session)
                ):
                    raise ValueError(
                        "max_active_jobs_per_session "
                        f"({max_active_jobs_per_session}) exceeded"
                    )
            data["jobs"] = [*jobs_raw, job.to_dict()]
            self._write_json_unlocked(data)

        await self._run_locked(_create)
        return job

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> HeartbeatJob:
        """patch-only 更新;维护状态机不变量。

        关键规则:
          - 修改 schedule 后重算 next_run_at(由 controller 注入,store 接受 next_run_at 字段)。
          - patch.enabled=false → status=disabled, next_run_at=None。
          - patch.enabled=true 且原为终态 → 重新激活: status=scheduled, next_run_at 由调用方重算注入。
          - 禁止 patch mode/model/approval/sandbox/worktree(controller 层已拦,store 层忽略这些键)。
        """
        job_id = str(job_id or "").strip()
        if not job_id:
            raise ValueError("id is required")
        patch = dict(patch or {})

        def _apply(existing: HeartbeatJob) -> HeartbeatJob:
            updated = existing
            if "name" in patch:
                updated = replace(updated, name=str(patch.get("name") or "").strip())
            if "prompt" in patch:
                updated = replace(updated, prompt=str(patch.get("prompt") or "").strip())
            if "channel_id" in patch:
                updated = replace(updated, channel_id=str(patch.get("channel_id") or "").strip())
            if "session_id" in patch:
                updated = replace(updated, session_id=str(patch.get("session_id") or "").strip())
            if "timezone" in patch:
                from jiuwenswarm.agents.harness.code.rails.heartbeat.models import _validate_timezone

                updated = replace(
                    updated,
                    timezone=_validate_timezone(str(patch.get("timezone") or "").strip()),
                )
            if "concurrency_policy" in patch:
                from jiuwenswarm.agents.harness.code.rails.heartbeat.models import HEARTBEAT_CONCURRENCY_POLICIES

                cp = str(patch.get("concurrency_policy") or DEFAULT_CONCURRENCY_POLICY).strip()
                if cp not in HEARTBEAT_CONCURRENCY_POLICIES:
                    raise ValueError(f"invalid concurrency_policy {cp!r}")
                updated = replace(updated, concurrency_policy=cp)
            if "session_deleted_policy" in patch:
                from jiuwenswarm.agents.harness.code.rails.heartbeat.models import HEARTBEAT_SESSION_DELETED_POLICIES

                sdp = str(patch.get("session_deleted_policy") or DEFAULT_SESSION_DELETED_POLICY).strip()
                if sdp not in HEARTBEAT_SESSION_DELETED_POLICIES:
                    raise ValueError(f"invalid session_deleted_policy {sdp!r}")
                updated = replace(updated, session_deleted_policy=sdp)
            if "max_runs" in patch:
                raw_mr = patch.get("max_runs")
                if raw_mr is None:
                    updated = replace(updated, max_runs=None)
                else:
                    try:
                        mr = int(raw_mr)
                    except Exception as exc:  # noqa: BLE001
                        raise ValueError("max_runs must be int or null") from exc
                    if mr < 1:
                        raise ValueError("max_runs must be at least 1")
                    updated = replace(updated, max_runs=mr)
            if "schedule" in patch:
                updated = replace(
                    updated,
                    schedule=HeartbeatSchedule.from_dict(
                        patch.get("schedule") or {},
                        default_timezone=updated.timezone,
                    ),
                )
            if "metadata" in patch:
                meta_patch = patch.get("metadata")
                if isinstance(meta_patch, dict):
                    merged = dict(updated.metadata or {})
                    merged.update(meta_patch)
                    if "source" in merged:
                        merged["source"] = validate_metadata_source(merged.get("source"))
                    updated = replace(updated, metadata=merged)

            if "enabled" in patch:
                enabled_val = patch.get("enabled")
                if not isinstance(enabled_val, bool):
                    raise ValueError("enabled must be boolean")
                updated = replace(updated, enabled=enabled_val)
                if not enabled_val:
                    # A stale UI may send its pause toggle after the scheduler
                    # has already completed/expired the job.  Disabling an
                    # already terminal job is a no-op for lifecycle status:
                    # otherwise the late request rewrites "completed" into
                    # "disabled" (displayed as "paused").
                    if existing.status not in HEARTBEAT_TERMINAL_STATUSES:
                        updated = replace(
                            updated, status=STATUS_DISABLED, next_run_at=None
                        )
                elif existing.status in HEARTBEAT_TERMINAL_STATUSES:
                    next_at = patch.get("next_run_at")
                    if next_at is None:
                        base = time.time()
                        if updated.schedule.type == SCHEDULE_ONCE:
                            next_at = updated.schedule.run_at
                            if next_at is None or float(next_at) <= base:
                                raise ValueError(
                                    "schedule has no future run; update run_at before enabling"
                                )
                        elif updated.schedule.type == SCHEDULE_INTERVAL:
                            next_at = base + int(updated.schedule.interval_seconds or 0)
                        else:
                            from datetime import datetime

                            from .cron_schedule import next_cron_datetime

                            tz = ZoneInfo(
                                updated.schedule.timezone
                                or updated.timezone
                                or DEFAULT_TIMEZONE
                            )
                            next_at = next_cron_datetime(
                                updated.schedule.cron_expr or "",
                                datetime.fromtimestamp(base, tz=tz),
                            ).timestamp()
                    updated = replace(
                        updated,
                        status=STATUS_SCHEDULED,
                        next_run_at=float(next_at),
                    )
            if "next_run_at" in patch:
                nra_raw = patch.get("next_run_at")
                updated = replace(
                    updated,
                    next_run_at=float(nra_raw) if isinstance(nra_raw, (int, float)) else None,
                )
            updated = replace(updated, updated_at=time.time())
            # Full model validation catches empty strings, invalid enums and max_runs.
            validated = HeartbeatJob.from_dict(updated.to_dict())
            validated.check_invariants()
            return validated

        return await self._mutate_job(job_id, _apply)

    async def delete_job(self, job_id: str) -> bool:
        """物理删除 job 记录。与终态的「保留记录」不同:delete 是真删除。"""
        job_id = str(job_id or "").strip()
        if not job_id:
            return False

        def _body() -> bool:
            data = self._read_json_unlocked()
            jobs_raw = data.get("jobs") or []
            if not isinstance(jobs_raw, list):
                jobs_raw = []
            kept: list[dict[str, Any]] = []
            deleted = False
            for item in jobs_raw:
                if not isinstance(item, dict):
                    continue
                if str(item.get("id") or "").strip() == job_id:
                    deleted = True
                    continue
                kept.append(item)
            data["version"] = int(data.get("version") or HEARTBEAT_JOBS_VERSION)
            data["jobs"] = kept
            if deleted:
                self._write_json_unlocked(data)
            return deleted

        return await self._run_locked(_body)

    async def toggle_job(self, job_id: str, enabled: bool) -> HeartbeatJob:
        """启停;联动 status / next_run_at(语义同 update enabled)。"""
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        return await self.update_job(job_id, {"enabled": enabled})

    async def reschedule(self, job_id: str, next_run_at: float | None) -> HeartbeatJob:
        """更新下次触发时间。next_run_at=None 时若处于终态则保持,否则置 scheduled。"""
        def _reschedule(job: HeartbeatJob) -> HeartbeatJob:
            if job.status in HEARTBEAT_TERMINAL_STATUSES:
                return job
            return replace(job, next_run_at=next_run_at, updated_at=time.time())

        return await self._mutate_job(job_id, _reschedule)

    async def disable(self, job_id: str, now: float) -> HeartbeatJob:
        """session_deleted_policy=disable 或手动停用。"""
        return await self._mutate_job(
            job_id,
            lambda job: replace(
                job,
                status=STATUS_DISABLED,
                enabled=False,
                next_run_at=None,
                updated_at=float(now),
            ),
        )

    async def complete_for_session_deleted(self, job_id: str, now: float) -> HeartbeatJob:
        """session_deleted_policy=completed 时调用。"""
        def _complete(job: HeartbeatJob) -> HeartbeatJob:
            return replace(
                job,
                status=STATUS_COMPLETED,
                enabled=False,
                next_run_at=None,
                run_state=replace(
                    job.run_state,
                    current_run_id=None,
                    current_run_started_at=None,
                    current_trigger=None,
                    current_reschedule=False,
                    last_run_status=RUN_CANCELLED,
                    last_error="session_deleted",
                    queued_run_id=None,
                    queued_trigger=None,
                    queued_reschedule=False,
                ),
                updated_at=float(now),
            )

        return await self._mutate_job(job_id, _complete)

    # ---- 查询辅助 ----

    async def get_active_run(self, job_id: str) -> str | None:
        """返回当前运行中的 run_id(若有),否则 None。"""
        job = await self.get_job(job_id)
        if job is None:
            return None
        return job.run_state.current_run_id

    async def list_jobs_by_session(self, session_id: str) -> list[HeartbeatJob]:
        session_id = str(session_id or "").strip()
        if not session_id:
            return []
        return [j for j in await self.list_jobs() if j.session_id == session_id]

    async def count_active_jobs_for_session(self, session_id: str) -> int:
        session_id = str(session_id or "").strip()
        if not session_id:
            return 0
        active_count = 0
        for job in await self.list_jobs():
            if job.session_id != session_id or not job.enabled:
                continue
            if job.status in {STATUS_SCHEDULED, STATUS_RUNNING}:
                active_count += 1
        return active_count

    async def count_active_jobs_global(self) -> int:
        return sum(
            1
            for j in await self.list_jobs()
            if j.status in {STATUS_SCHEDULED, STATUS_RUNNING} and j.enabled
        )

    # ---- 运行状态查询(source 兜底) ----

    async def reload_mtime(self) -> float:
        """供 scheduler 做 mtime 变化检测(与 Cron 一致)。"""
        try:
            return self._path.stat().st_mtime
        except Exception:
            return 0.0


def _id_prefix() -> str:
    from jiuwenswarm.agents.harness.code.rails.heartbeat.models import HEARTBEAT_ID_PREFIX

    return HEARTBEAT_ID_PREFIX
