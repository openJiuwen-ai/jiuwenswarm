# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""HeartbeatSchedulerService — 心跳任务后台调度.

职责:
  - ``_loop`` 周期扫描 due jobs,调用 ``_tick_once``。
  - ``_handle_due_job``:session 校验 + 并发策略 + dispatch。
  - ``_dispatch_job``:构造 ``CHAT_SEND`` 投递回原 session,带 automation metadata。
  - ``_finish_run``:更新 last_run_at/run_count/next_run_at;达成停止条件则 completed。
  - ``compute_next_run``:interval/cron/once 下一次触发。
  - ``_apply_concurrency_policy``:skip/queue/replace。
  - ``_handle_missing_session``:session 删除/不可恢复按 session_deleted_policy 处理。
  - ``reload``/``_check_store_changed``:外部编辑 heartbeat_jobs.json 后刷新。
  - ghost task 清理:job 被删除后取消当前 run。

关键约束:
  - 不创建 heartbeat_* 临时会话;不读 HEARTBEAT.md;不用 __heartbeat__ channel。
  - CHAT_SEND 投递回原 session_id;不传 params.mode(web/tui 由原 session 配置决定)。
  - 不补跑系统离线期间错过的历史触发(next_run_at 远早于 now 时基于 now 重算)。
  - 自动触发消息必须带 metadata.automation。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

from jiuwenswarm.agents.harness.code.rails.heartbeat.models import (
    HEARTBEAT_TERMINAL_STATUSES,
    HeartbeatJob,
    SOURCE_AGENT_TOOL,
    SOURCE_SCHEDULE_RECOVERY,
    SCHEDULE_CRON,
    SCHEDULE_INTERVAL,
    SCHEDULE_ONCE,
    SESSION_DELETED_COMPLETED,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    STATUS_SCHEDULED,
)
from jiuwenswarm.agents.harness.code.rails.heartbeat.session_resolver import HeartbeatSessionResolver
from jiuwenswarm.agents.harness.code.rails.heartbeat.cron_schedule import next_cron_datetime

if TYPE_CHECKING:
    from jiuwenswarm.agents.harness.code.rails.heartbeat.store import HeartbeatJobStore

logger = logging.getLogger(__name__)

# Product policy: waiting for the bound session is distinct from a heartbeat
# run's concurrency_policy.  Keep this internal and uniform across all jobs.
_SESSION_BUSY_WAIT_TIMEOUT_SECONDS = 60.0


class HeartbeatExecution(Protocol):
    """AgentServer-local execution contract used by the scheduler."""

    def is_session_busy(
        self, session_id: str, *, exclude_run_id: str = ""
    ) -> bool:
        ...

    def has_active_run(self, run_id: str) -> bool:
        ...

    async def dispatch(
        self, job: HeartbeatJob, run_id: str, request_message: Any
    ) -> bool:
        ...

    async def cancel(self, run_id: str) -> bool:
        ...


def _now_ts() -> float:
    return time.time()


class HeartbeatSchedulerService:
    """后台调度心跳任务,到点投递 follow-up prompt 回原 session。"""

    def __init__(
        self,
        *,
        store: "HeartbeatJobStore",
        execution_service: HeartbeatExecution,
        session_resolver: HeartbeatSessionResolver | None = None,
        now_fn: Callable[[], float] = _now_ts,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        self._store = store
        self._execution_service = execution_service
        self._session_resolver = session_resolver or HeartbeatSessionResolver(scheduler=self)
        # 确保 resolver 能回调本 scheduler
        self._session_resolver.set_scheduler(self)
        self._now_fn = now_fn
        self._poll_interval = float(poll_interval_seconds)

        self._running = False
        self._task: asyncio.Task | None = None
        self._reload_event = asyncio.Event()
        self._last_store_mtime: float = 0.0

        # 内存中的活跃 run:run_id -> (job_id, started_at)
        self._active_runs: dict[str, tuple[str, float]] = {}
        # Exact cancellation intent consumed by the stream's finally callback.
        # Keeping this in-process is sufficient because cancel_request awaits
        # the exact stream task before it returns.
        self._cancel_intents: dict[str, bool] = {}
        self._limits: dict[str, Any] = {}
        # Product Session deletion temporarily suspends dispatch without
        # mutating job policy until the product deletion has committed.
        self._suspended_sessions: set[str] = set()

    def set_limits(self, limits: dict[str, Any] | None) -> None:
        self._limits = dict(limits or {})

    def suspend_session(self, session_id: str) -> None:
        session_id = str(session_id or "").strip()
        if session_id:
            self._suspended_sessions.add(session_id)

    def resume_session(self, session_id: str) -> None:
        self._suspended_sessions.discard(str(session_id or "").strip())
        self._reload_event.set()

    # ---- 生命周期 ----

    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            await self.reload()
        except Exception:
            # A corrupt/unreadable store is intentionally fail-closed, but a
            # failed start must not leave a scheduler that claims to be running.
            self._running = False
            raise
        self._task = asyncio.create_task(self._loop(), name="heartbeat-scheduler-loop")

    async def stop(self) -> None:
        self._running = False
        self._reload_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # ---- store mtime 检测(镜像 CronSchedulerService) ----

    def _get_store_mtime(self) -> float:
        try:
            return self._store.path.stat().st_mtime
        except OSError:
            return 0.0

    def _sync_store_mtime(self) -> None:
        self._last_store_mtime = self._get_store_mtime()

    async def _check_store_changed(self) -> bool:
        mtime = self._get_store_mtime()
        if mtime != self._last_store_mtime and (mtime or self._last_store_mtime):
            logger.info(
                "[HeartbeatScheduler] store file changed (mtime %.3f -> %.3f), reloading",
                self._last_store_mtime,
                mtime,
            )
            await self.reload()
            return True
        return False

    async def reload(self) -> None:
        """重新加载 store;清理 ghost run 并恢复超时的持久化 run。"""
        await self._store.list_jobs()  # 触发读盘 + 缓存刷新
        # 清理 store 中已不存在的 job 的活跃 run
        all_jobs = await self._store.list_jobs()
        live_job_ids = {j.id for j in all_jobs}
        ghost_run_ids = [
            rid
            for rid, (jid, _) in self._active_runs.items()
            if jid not in live_job_ids
        ]
        for rid in ghost_run_ids:
            self._active_runs.pop(rid, None)
            logger.info(
                "[HeartbeatScheduler] cleared ghost run: run_id=%s (job no longer in store)",
                rid,
            )
        # A persisted run is live only when this AgentServer still tracks its
        # exact execution task. After a cold restart no completion callback can arrive, so
        # waiting on an arbitrary 24h lease would leave a ghost run all day.
        for job in all_jobs:
            rid = job.run_state.current_run_id
            started = job.run_state.current_run_started_at
            if not rid or started is None or rid in self._active_runs:
                continue
            if self._execution_service.has_active_run(rid):
                self._active_runs[rid] = (job.id, started)
                continue
            await self.on_run_finished(
                job.id,
                rid,
                outcome="failed",
                error="orphan run recovered after scheduler restart",
            )
        self._sync_store_mtime()
        self._reload_event.set()

    # ---- 主循环 ----

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick_once()
                self._reload_event.clear()
                try:
                    await asyncio.wait_for(
                        self._reload_event.wait(),
                        timeout=self._poll_interval,
                    )
                except asyncio.TimeoutError:
                    await self._check_store_changed()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("[HeartbeatScheduler] loop error: %s", exc, exc_info=True)
                await asyncio.sleep(0.5)

    async def _tick_once(self) -> None:
        """扫描到期任务并逐个执行调度判断。"""
        jobs = await self._store.list_jobs()
        now = self._now_fn()
        admitted_job_ids = self._resource_admitted_job_ids(jobs, now=now)
        for job in jobs:
            try:
                await self._handle_job_tick(job, now, admitted_job_ids)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[HeartbeatScheduler] handle job tick failed job=%s: %s",
                    job.id,
                    exc,
                    exc_info=True,
                )

    def _resource_admitted_job_ids(
        self, jobs: list[HeartbeatJob], *, now: float | None = None
    ) -> set[str]:
        """Select a fair runnable subset after limits are lowered.

        Non-due scheduled jobs must not consume admission capacity. Previously
        skipped jobs are preferred on the next due tick so a stable created_at
        ordering cannot starve newer jobs forever.
        """
        max_session = int(self._limits.get("max_active_jobs_per_session", 5))
        max_global = int(self._limits.get("max_active_jobs_global", 100))
        current_time = self._now_fn() if now is None else float(now)
        candidates: list[HeartbeatJob] = []
        for job in jobs:
            if not job.enabled:
                continue
            if job.status not in {STATUS_SCHEDULED, STATUS_RUNNING}:
                continue
            if job.status == STATUS_RUNNING:
                candidates.append(job)
                continue
            if job.next_run_at is not None and float(job.next_run_at) <= current_time:
                candidates.append(job)
        candidates.sort(
            key=lambda job: (
                0 if job.status == STATUS_RUNNING else 1,
                -int(job.run_state.skipped_count),
                job.next_run_at or 0.0,
                job.created_at or 0.0,
                job.id,
            ),
        )
        admitted: set[str] = set()
        per_session: dict[str, int] = {}
        for job in candidates:
            if len(admitted) >= max_global:
                break
            current = per_session.get(job.session_id, 0)
            if current >= max_session:
                continue
            admitted.add(job.id)
            per_session[job.session_id] = current + 1
        return admitted

    def _session_is_busy(self, job: HeartbeatJob) -> bool:
        return bool(
            self._execution_service.is_session_busy(
                job.session_id,
                exclude_run_id=job.run_state.current_run_id or "",
            )
        )

    async def _handle_job_tick(
        self, job: HeartbeatJob, now: float, admitted_job_ids: set[str]
    ) -> None:
        """单 job 的调度判断:可调度性 + due + session + 并发 + dispatch。"""
        if job.session_id in self._suspended_sessions:
            return
        if not job.enabled:
            return
        if job.status not in {STATUS_SCHEDULED, "running"}:
            logger.warning(
                "[HeartbeatScheduler] skip inconsistent job %s: status=%s enabled=%s",
                job.id,
                job.status,
                job.enabled,
            )
            return
        if job.next_run_at is None or job.next_run_at > now:
            return
        if job.id not in admitted_job_ids:
            logger.warning(
                "[HeartbeatScheduler] skip job %s: resource admission limit exceeded",
                job.id,
            )
            await self._store.skip_and_reschedule(
                job.id,
                now=now,
                reason="resource_admission_limit_exceeded",
                next_run_at=self.compute_next_run(job, now),
            )
            return
        await self._handle_due_job(job, now)

    # ---- 到期任务处理 ----

    async def _handle_due_job(self, job: HeartbeatJob, now: float) -> None:
        session = self._session_resolver.resolve(job.channel_id, job.session_id)
        if session is None:
            await self._handle_missing_session(job, now)
            return
        if self._session_is_busy(job):
            due_at = float(job.next_run_at if job.next_run_at is not None else now)
            waited = max(0.0, now - due_at)
            wait_timeout = _SESSION_BUSY_WAIT_TIMEOUT_SECONDS
            if waited < wait_timeout:
                logger.debug(
                    "[HeartbeatScheduler] waiting for bound session to become idle: "
                    "job=%s session=%s waited=%.1fs timeout=%.1fs",
                    job.id,
                    job.session_id,
                    waited,
                    wait_timeout,
                )
                return
            await self._store.skip_and_reschedule(
                job.id,
                now=now,
                reason="session_busy_timeout",
                next_run_at=self.compute_next_run(job, now),
            )
            return
        run_id = self._new_run_id(job, now)
        decision = await self._start_run(
            job, run_id, now, trigger="scheduler", reschedule=True
        )
        if decision == "skip":
            # 跳过本轮,基于 now 重算下次触发(不补跑历史积压)
            await self._store.reschedule(job.id, self.compute_next_run(job, now))
            return
        if decision in {"queued", "coalesced", "replace_pending"}:
            await self._store.reschedule(job.id, self.compute_next_run(job, now))
            return
        if decision == "cancel_failed":
            await self._store.reschedule(job.id, self.compute_next_run(job, now))
            return

    async def _handle_missing_session(self, job: HeartbeatJob, now: float) -> None:
        """处理 session 删除、归档或不可恢复的任务。"""
        await self._apply_session_deleted_job(job, now)
        logger.info(
            "[HeartbeatScheduler] session missing for job=%s, applied policy=%s",
            job.id,
            job.session_deleted_policy,
        )

    async def _apply_session_deleted_job(
        self, job: HeartbeatJob, now: float
    ) -> None:
        """Stop an exact active run before applying its session lifecycle policy."""
        cancel_failed = False
        if job.run_state.current_run_id:
            result = await self.cancel_run(job.id, pause_schedule=True)
            cancel_failed = result.get("cancel_status") == "failed"
        if (
            job.session_deleted_policy == SESSION_DELETED_COMPLETED
            and not cancel_failed
        ):
            await self._store.complete_for_session_deleted(job.id, now)
        else:
            # On cancellation failure, disabled preserves current_run_id so a
            # still-live stream remains authoritative until its callback.
            await self._store.disable(job.id, now)

    # ---- 投递回原 session ----

    @staticmethod
    def _new_run_id(job: HeartbeatJob, now: float) -> str:
        return f"{job.id}_run_{int(now)}_{secrets.token_hex(3)}"

    def _build_message(self, job: HeartbeatJob, run_id: str, now: float) -> Any:
        """构造投递回原 session 的 CHAT_SEND 消息。"""
        from jiuwenswarm.common.schema.message import Message, ReqMethod

        # metadata.source 审计:scheduler 只消费,缺失/非法记 warning 后兜底。
        raw_source = str((job.metadata or {}).get("source") or "").strip()
        source = raw_source
        if raw_source not in {
            SOURCE_AGENT_TOOL,
            "web_rpc",
            "tui_rpc",
            SOURCE_SCHEDULE_RECOVERY,
        }:
            logger.warning(
                "[HeartbeatScheduler] job %s missing or invalid source=%r, "
                "falling back to schedule_recovery",
                job.id,
                raw_source,
            )
            source = SOURCE_SCHEDULE_RECOVERY

        session = self._session_resolver.resolve(job.channel_id, job.session_id)
        route = dict(session.route_metadata or {}) if session is not None else {}
        metadata = dict(route)
        automation = {
            "kind": "heartbeat",
            "job_id": job.id,
            "run_id": run_id,
            "triggered_at": now,
            "source": source,
            "trigger": job.run_state.current_trigger or "scheduler",
        }
        metadata["automation"] = automation
        return Message(
            id=run_id,
            type="req",
            channel_id=(session.channel_id if session is not None else job.channel_id),
            session_id=job.session_id,
            req_method=ReqMethod.CHAT_SEND,
            timestamp=now,
            ok=True,
            is_stream=True,
            params={
                "query": job.prompt,
                "content": job.prompt,
                # 不设置 params.mode:web/tui 由原 session 当前配置决定。
                # 心跳仍是普通 CHAT_SEND，不引入底层 RunKind；
                # 执行身份只由 metadata.automation 关联。
                # Persist the same marker with user/assistant history so a later
                # session restore can identify this complete automated turn.
                "automation": dict(automation),
            },
            metadata=metadata,
            provider=str(route.get("provider") or "") or None,
            chat_id=str(route.get("chat_id") or "") or None,
            user_id=str(
                route.get("user_id") or (job.metadata or {}).get("user_id") or ""
            ) or None,
            bot_id=str(route.get("bot_id") or "") or None,
            app_id=str(route.get("app_id") or "") or None,
        )

    async def _dispatch_claimed_job(
        self, job: HeartbeatJob, run_id: str, now: float
    ) -> None:
        """Start a claimed run inside AgentServer through the shared admission."""
        self._active_runs[run_id] = (job.id, now)
        try:
            msg = self._build_message(job, run_id, now)
            admitted = await self._execution_service.dispatch(job, run_id, msg)
            if not admitted:
                await self.on_session_busy_after_dispatch(job.id, run_id)
        except Exception as exc:
            logger.warning(
                "[HeartbeatScheduler] dispatch failed job=%s run_id=%s: %s",
                job.id,
                run_id,
                exc,
            )
            await self.on_run_finished(
                job.id, run_id, outcome="failed", error=str(exc)
            )

    async def _start_run(
        self,
        job: HeartbeatJob,
        run_id: str,
        now: float,
        *,
        trigger: str,
        reschedule: bool,
    ) -> str:
        decision, claimed, replaced_run_id = await self._store.claim_run(
            job.id,
            run_id,
            now,
            trigger=trigger,
            reschedule=reschedule,
            next_run_at_after_claim=(
                self.compute_next_run(job, now) if trigger == "scheduler" else None
            ),
        )
        if decision == "replace" and replaced_run_id:
            # Reserve the replacement while the old run remains authoritative.
            # The stream callback consumes this intent without starting queued
            # work; only a confirmed cancellation promotes the replacement.
            self._cancel_intents[replaced_run_id] = False
            try:
                cancel_status, cancel_error = await self._send_cancel_to_session(
                    job, replaced_run_id
                )
            except asyncio.CancelledError:
                self._cancel_intents.pop(replaced_run_id, None)
                await asyncio.shield(
                    self._store.clear_replacement_reservation(
                        job.id, run_id, now=self._now_fn()
                    )
                )
                raise
            if cancel_status == "failed":
                self._cancel_intents.pop(replaced_run_id, None)
                await self._store.clear_replacement_reservation(
                    job.id, run_id, now=self._now_fn()
                )
                await self._store.record_cancel_result(
                    job.id,
                    status=cancel_status,
                    error=cancel_error,
                    now=self._now_fn(),
                )
                return "cancel_failed"
            # Fake/custom handlers may acknowledge without invoking the normal
            # stream finally callback. Finalize the exact old run idempotently.
            await self.on_run_finished(
                job.id,
                replaced_run_id,
                outcome="cancelled",
                consume_queue=False,
            )
            self._cancel_intents.pop(replaced_run_id, None)
            self._active_runs.pop(replaced_run_id, None)
            promoted, claimed = await self._store.replace_claimed_run(
                job.id,
                expected_run_id=replaced_run_id,
                new_run_id=run_id,
                now=now,
                trigger=trigger,
                reschedule=reschedule,
                next_run_at_after_claim=(
                    self.compute_next_run(job, now)
                    if trigger == "scheduler"
                    else None
                ),
            )
            await self._store.record_cancel_result(
                job.id,
                status=cancel_status,
                error=cancel_error,
                now=self._now_fn(),
            )
            decision = "run" if promoted else "cancelled"
        if decision == "run":
            await self._dispatch_claimed_job(claimed, run_id, now)
        return decision

    # ---- 下一次触发计算 ----

    def compute_next_run(self, job: HeartbeatJob, base_ts: float) -> float | None:
        """interval/cron/once 下一次触发;基于 now 重算,不补跑历史。"""
        stype = job.schedule.type
        if stype == SCHEDULE_ONCE:
            run_at = job.schedule.run_at
            return float(run_at) if run_at is not None and float(run_at) > base_ts else None
        if stype == SCHEDULE_INTERVAL:
            iv = job.schedule.interval_seconds or 60
            return base_ts + max(60, int(iv))
        if stype == SCHEDULE_CRON:
            return self._next_cron_ts(job, base_ts)
        raise ValueError(f"unsupported schedule type: {stype}")

    @staticmethod
    def _next_cron_ts(job: HeartbeatJob, base_ts: float) -> float | None:
        tz_name = job.schedule.timezone or job.timezone or "Asia/Shanghai"
        tz = ZoneInfo(tz_name)
        base_dt = datetime.fromtimestamp(base_ts, tz=tz)
        try:
            nxt = next_cron_datetime(job.schedule.cron_expr or "", base_dt)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[HeartbeatScheduler] cron next run failed job=%s expr=%r: %s",
                job.id,
                job.schedule.cron_expr,
                exc,
            )
            return None
        return nxt.timestamp()

    # ---- 完成与停止条件 ----

    async def on_run_finished(
        self,
        job_id: str,
        run_id: str,
        *,
        outcome: str,
        error: str | None = None,
        pause_schedule: bool = False,
        consume_queue: bool = True,
    ) -> bool:
        """Complete the exact run reported by the AgentServer execution owner."""
        job = await self._store.get_job(job_id)
        if job is None or job.run_state.current_run_id != run_id:
            return False
        now = self._now_fn()
        normalized = {
            "succeeded": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
            "skipped": "skipped",
        }.get(outcome, "failed")
        cancel_pause = self._cancel_intents.get(run_id)
        if normalized == "cancelled" and cancel_pause is not None:
            pause_schedule = bool(cancel_pause)
            consume_queue = False
        completed_attempt = normalized in {"succeeded", "failed"}
        next_count = int(job.run_count) + (1 if completed_attempt else 0)
        terminal = completed_attempt and (
            job.schedule.type == SCHEDULE_ONCE
            or (job.max_runs is not None and next_count >= int(job.max_runs))
        )
        # Scheduler claims already advanced next_run_at from the trigger time.
        # Finishing must not move interval jobs again by their execution time.
        if job.run_state.current_trigger == "scheduler":
            next_run_at = job.next_run_at
        elif job.run_state.current_reschedule:
            next_run_at = self.compute_next_run(job, now)
        else:
            next_run_at = job.next_run_at
        matched, finished = await self._store.finish_run(
            job.id,
            run_id,
            now,
            outcome=normalized,
            error=error,
            next_run_at=next_run_at,
            terminal=terminal,
            pause_schedule=pause_schedule,
        )
        self._active_runs.pop(run_id, None)
        if not matched or finished.is_terminal() or not consume_queue:
            return matched
        await self._consume_queued_run(job.id)
        return matched

    async def on_session_busy_after_dispatch(
        self, job_id: str, run_id: str
    ) -> bool:
        """Return an exact claim to its original due time after a dispatch race."""
        try:
            matched, _ = await self._store.defer_claimed_run_for_busy(
                job_id,
                run_id,
                now=self._now_fn(),
            )
        except KeyError:
            matched = False
        self._active_runs.pop(run_id, None)
        return matched

    async def _consume_queued_run(self, job_id: str) -> None:
        """Start the single queued run, if the job is still runnable."""
        queued = await self._store.pop_queued_run(job_id)
        if queued is not None:
            queued_id, trigger, queued_reschedule = queued
            refreshed = await self._store.get_job(job_id)
            if refreshed is not None:
                await self._start_run(
                    refreshed,
                    queued_id,
                    self._now_fn(),
                    trigger=trigger,
                    reschedule=queued_reschedule,
                )

    # ---- 取消执行 ----

    async def cancel_run(self, job_id: str, *, pause_schedule: bool = False) -> dict[str, Any]:
        """中断当前心跳触发的运行;可选同时停用后续调度。"""
        job = await self._store.get_job(job_id)
        if job is None:
            raise KeyError("job not found")
        run_id = job.run_state.current_run_id
        if run_id is None and job.status in HEARTBEAT_TERMINAL_STATUSES:
            return {
                "job_id": job.id,
                "cancelled_run_id": None,
                "cancel_status": "idle",
                "paused": False,
                "reason": "job_terminal",
            }
        now = self._now_fn()
        cancelled_run_id: str | None = None
        cancel_status = "idle"
        if run_id:
            self._cancel_intents[run_id] = bool(pause_schedule)
            try:
                cancel_status, cancel_error = await self._send_cancel_to_session(
                    job, run_id
                )
                if cancel_status != "failed":
                    # The normal cancel path already awaited the stream callback;
                    # this covers not_found and simple test/custom transports.
                    await self.on_run_finished(
                        job.id,
                        run_id,
                        outcome="cancelled",
                        pause_schedule=pause_schedule,
                        consume_queue=False,
                    )
                    self._active_runs.pop(run_id, None)
                elif pause_schedule:
                    # Stop future scheduling while preserving the still-live run.
                    await self._store.disable(job.id, now)
            finally:
                self._cancel_intents.pop(run_id, None)
            await self._store.record_cancel_result(
                job.id,
                status=cancel_status,
                error=cancel_error,
                now=self._now_fn(),
            )
            cancelled_run_id = run_id
        elif pause_schedule:
            await self._store.disable(job.id, now)
        return {
            "job_id": job.id,
            "cancelled_run_id": cancelled_run_id,
            "cancel_status": cancel_status,
            "paused": bool(pause_schedule),
        }

    async def _send_cancel_to_session(
        self, job: HeartbeatJob, run_id: str
    ) -> tuple[str, str | None]:
        """Cancel only the AgentServer execution task for this Heartbeat run."""
        try:
            accepted = bool(await self._execution_service.cancel(run_id))
            return ("cancelled", None) if accepted else (
                "not_found",
                "exact AgentServer heartbeat execution was not found",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[HeartbeatScheduler] send cancel to session failed job=%s run_id=%s: %s",
                job.id,
                run_id,
                exc,
            )
            return "failed", str(exc)

    # ---- trigger_run_now(controller 调用) ----

    async def trigger_run_now(
        self, job_id: str, *, reschedule: bool = False
    ) -> dict[str, Any]:
        """立即执行一次指定心跳任务。"""
        job = await self._store.get_job(job_id)
        if job is None:
            raise KeyError("job not found")
        if job.session_id in self._suspended_sessions:
            return {
                "accepted": False,
                "run_id": "",
                "session_id": job.session_id,
                "reason": "session_deleting",
            }
        # completed 表示 once/max_runs 已经达成。run_now
        # 不能绕过这些停止条件；需要再次执行时必须先由显式 update/toggle
        # 立即执行终态任务前先恢复其可运行状态。
        if job.status == STATUS_COMPLETED or (
            job.max_runs is not None and int(job.run_count) >= int(job.max_runs)
        ):
            return {
                "accepted": False,
                "run_id": "",
                "session_id": job.session_id,
                "reason": "job_completed",
            }
        # 与 _handle_due_job 一致:先校验 session 存在性。
        session = self._session_resolver.resolve(job.channel_id, job.session_id)
        if session is None:
            await self._handle_missing_session(job, self._now_fn())
            return {
                "accepted": False,
                "run_id": "",
                "session_id": job.session_id,
                "reason": "session_missing",
            }
        if self._session_is_busy(job):
            return {
                "accepted": False,
                "run_id": "",
                "session_id": job.session_id,
                "reason": "session_busy",
            }
        now = self._now_fn()
        run_id = self._new_run_id(job, now)
        # run_now 受并发策略约束，且 reschedule=false 时恢复执行前调度状态。
        decision = await self._start_run(
            job, run_id, now, trigger="run_now", reschedule=reschedule
        )
        if decision == "skip":
            return {
                "accepted": False,
                "run_id": run_id,
                "session_id": job.session_id,
                "reason": "previous_run_active",
            }
        if decision == "queued":
            return {
                "accepted": True,
                "queued": True,
                "run_id": run_id,
                "session_id": job.session_id,
            }
        if decision in {"coalesced", "replace_pending"}:
            current = await self._store.get_job(job.id)
            pending_run_id = (
                current.run_state.queued_run_id if current is not None else ""
            )
            return {
                "accepted": False,
                "queued": True,
                "run_id": pending_run_id or "",
                "session_id": job.session_id,
                "reason": (
                    "already_queued"
                    if decision == "coalesced"
                    else "replacement_pending"
                ),
            }
        if decision in {"cancel_failed", "cancelled"}:
            return {
                "accepted": False,
                "run_id": run_id,
                "session_id": job.session_id,
                "reason": (
                    "replacement_cancel_failed"
                    if decision == "cancel_failed"
                    else "job_disabled_during_replace"
                ),
            }
        if decision == "completed":
            return {
                "accepted": False,
                "run_id": "",
                "session_id": job.session_id,
                "reason": "job_completed",
            }
        return {
            "accepted": True,
            "run_id": run_id,
            "session_id": job.session_id,
        }

    # ---- 会话删除回调(session_resolver 转发) ----

    async def on_session_deleted(self, session_id: str) -> None:
        """session 被删除/归档时,按各 job 的 session_deleted_policy 处理。"""
        now = self._now_fn()
        jobs = await self._store.list_jobs_by_session(session_id)
        for job in jobs:
            if job.status in HEARTBEAT_TERMINAL_STATUSES:
                continue
            await self._apply_session_deleted_job(job, now)
            logger.info(
                "[HeartbeatScheduler] session %s deleted, applied policy=%s to job=%s",
                session_id,
                job.session_deleted_policy,
                job.id,
            )

    # ---- preview(controller 调用) ----

    def preview_next_runs(self, job: HeartbeatJob, count: int = 5) -> list[dict[str, Any]]:
        """预览未来 N 次触发时间。"""
        count = max(1, min(int(count), 50))
        out: list[dict[str, Any]] = []
        now = self._now_fn()
        base = now
        stype = job.schedule.type
        if stype == SCHEDULE_ONCE:
            ra = job.schedule.run_at
            if ra is not None and ra >= now:
                out.append(self._format_preview(ra, timezone=job.timezone))
            return out
        for _ in range(count):
            if stype == SCHEDULE_INTERVAL:
                iv = job.schedule.interval_seconds or 60
                nxt = base + max(60, int(iv))
            elif stype == SCHEDULE_CRON:
                nxt_ts = self._next_cron_ts(job, base)
                if nxt_ts is None:
                    break
                nxt = nxt_ts
            else:
                break
            out.append(
                self._format_preview(
                    nxt,
                    timezone=job.schedule.timezone or job.timezone,
                )
            )
            base = nxt
        return out

    @staticmethod
    def _format_preview(
        ts: float, *, timezone: str = "Asia/Shanghai"
    ) -> dict[str, Any]:
        from datetime import datetime as _dt

        try:
            iso = _dt.fromtimestamp(ts, tz=ZoneInfo(timezone)).isoformat()
        except Exception:
            iso = ""
        return {"run_at": ts, "iso": iso}
