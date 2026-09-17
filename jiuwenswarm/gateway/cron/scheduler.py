from __future__ import annotations

import asyncio
import heapq
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from jiuwenswarm.gateway.routing.agent_client import AgentServerClient
from jiuwenswarm.gateway.cron.dingtalk_routing import (
    is_usable_dingtalk_staff_id,
    resolve_dingtalk_push_metadata,
)
from jiuwenswarm.gateway.cron.models import (
    CRON_JOB_DEFAULT_MODE,
    CronJob,
    CronRunState,
    CronTargetChannel,
    is_team_cron_mode,
    resolve_cron_job_timeout_seconds,
)
from jiuwenswarm.gateway.cron.store import CronJobStore
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.common.work_mode import DEFAULT_WEB_WORK_MODE
from jiuwenswarm.server.runtime.session.session_history import append_history_record

logger = logging.getLogger(__name__)


def _now_utc_ts() -> float:
    return time.time()


def _resolve_cron_execution_context(
    job: CronJob,
    *,
    ts: str,
    message_handler: MessageHandler | None = None,
) -> tuple[str, str]:
    """Resolve channel_id and session_id for team cron agent execution.

    Team jobs always use an isolated ``cron_*`` session so scheduled runs start
    fresh and are not cancelled when the creator TUI/web window closes
    (``cancel_agent_sessions_on_disconnect``). ``job.session_id`` is kept for IM
    push routing only.
    """
    _ = message_handler
    channel_id = (job.targets or CronTargetChannel.TUI.value).strip() or CronTargetChannel.TUI.value
    return channel_id, f"cron_{ts}_{job.id}"


def _normalize_workflow_result_text(result: str) -> str:
    text = result.strip()
    prefix = "Workflow completed, result:"
    if not text.startswith(prefix):
        return text
    remainder = text[len(prefix):].strip()
    try:
        parsed = json.loads(remainder)
    except json.JSONDecodeError:
        return remainder
    if not isinstance(parsed, dict):
        return remainder
    for key in ("final_report", "executive_summary", "summary", "report"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return remainder


def _extract_workflow_result_text(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    if str(payload.get("event_type") or "").strip() != "workflow.updated":
        return None
    workflow = payload.get("workflow")
    if not isinstance(workflow, dict):
        return None
    status = str(workflow.get("status") or "").strip().lower()
    if status not in ("completed", "failed"):
        return None

    summary = workflow.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()

    result = workflow.get("result")
    if isinstance(result, str) and result.strip():
        return _normalize_workflow_result_text(result)

    phases = workflow.get("phases")
    if isinstance(phases, list):
        for phase in reversed(phases):
            if not isinstance(phase, dict):
                continue
            agents = phase.get("agents")
            if not isinstance(agents, list):
                continue
            for agent in reversed(agents):
                if not isinstance(agent, dict):
                    continue
                outcome = agent.get("outcome")
                if isinstance(outcome, str) and outcome.strip():
                    return outcome.strip()

    if status == "failed":
        error = workflow.get("error")
        if error is not None:
            return f"[cron] SwarmFlow 执行失败: {error}"
    return None


def _extract_text_from_stream_payload(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    event_type = str(payload.get("event_type") or "").strip()
    if event_type == "chat.final":
        content = payload.get("content")
        if isinstance(content, str) and content.strip():
            return content
    if event_type == "chat.error":
        error = payload.get("error")
        if isinstance(error, str) and error.strip():
            return error
    workflow_text = _extract_workflow_result_text(payload)
    if workflow_text:
        return workflow_text
    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        return content
    return None


from jiuwenswarm.common.cron_team_completion import (
    apply_cron_team_round_event,
    cron_team_round_should_end,
    is_cron_leader_placeholder_text as _is_cron_leader_placeholder_text,
    new_cron_team_round_state,
)


def _is_cron_team_result_insufficient(*, text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return True
    return _is_cron_leader_placeholder_text(normalized)


def _pick_cron_team_result_text(*, leader_text: str, workflow_text: str) -> str:
    leader = str(leader_text or "").strip()
    workflow = str(workflow_text or "").strip()
    if leader and not _is_cron_leader_placeholder_text(leader):
        return leader
    if workflow:
        return workflow
    return leader


def _resolve_cron_team_timeout_result(
    *,
    leader_text: str,
    workflow_text: str,
    workflow_completed: bool,
    timeout_min: int,
) -> tuple[str, bool]:
    result_text = _pick_cron_team_result_text(
        leader_text=leader_text,
        workflow_text=workflow_text,
    )
    if result_text and workflow_completed and not _is_cron_leader_placeholder_text(result_text):
        return result_text, True
    if result_text and not _is_cron_leader_placeholder_text(result_text):
        return (
            f"{result_text}\n\n[cron] 任务流超时（>{timeout_min}min），以上为已获取的结果。",
            False,
        )
    return f"[cron] 任务执行超时（>{timeout_min}min）", False


def _extract_text_from_agent_payload(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    # AgentServer unary error responses use ok=False, payload={"error": "..."}
    # Pass through raw error text, same as normal chat behavior
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error
    # Common: {"content": {"output": "...", "result_type": "answer"}}
    content = payload.get("content")
    if isinstance(content, dict):
        out = content.get("output")
        if isinstance(out, str):
            return out
        if out is not None:
            return str(out)
        return str(content)
    if isinstance(content, str):
        return content
    # Fallbacks
    heartbeat = payload.get("heartbeat")
    if isinstance(heartbeat, str) and heartbeat:
        return heartbeat
    text = payload.get("text")
    if isinstance(text, str) and text:
        return text
    return ""


def _format_cron_broadcast_text(*, job_name: str, text: str, is_placeholder: bool) -> str:
    # Result body, in-progress placeholders, and [cron] status text are all
    # delivered as-is — no job-name prefix is prepended.
    return str(text or "").strip()


def _cron_next_push_dt(cron_expr: str, base_dt: datetime) -> datetime:
    # Lazy import so the rest of the system can still run without cron enabled.
    from croniter import croniter  # type: ignore

    # Support Quartz 7-field format: second minute hour day month dow year
    # croniter default is minute hour day month dow second year
    field_count = len(cron_expr.strip().split())
    second_at_beginning = field_count == 7

    it = croniter(cron_expr, base_dt, second_at_beginning=second_at_beginning)
    nxt = it.get_next(datetime)
    if not isinstance(nxt, datetime):
        raise RuntimeError("croniter returned invalid datetime")
    if nxt.tzinfo is None:
        # Keep tz-consistent; base_dt is tz-aware in our usage.
        return nxt.replace(tzinfo=base_dt.tzinfo)
    return nxt


@dataclass(frozen=True, order=True)
class _Event:
    at_ts: float
    seq: int
    kind: str  # wake|push|push_update
    job_id: str
    run_id: str


class CronSchedulerService:
    """Async scheduler that wakes agent and pushes results to channels."""

    def __init__(
        self,
        *,
        store: CronJobStore,
        agent_client: AgentServerClient,
        message_handler: MessageHandler,
        now_fn: Callable[[], float] = _now_utc_ts,
    ) -> None:
        self._store = store
        self._agent_client = agent_client
        self._message_handler = message_handler
        self._now_fn = now_fn

        self._running = False
        self._task: asyncio.Task | None = None
        self._reload_event = asyncio.Event()

        self._jobs: dict[str, CronJob] = {}
        self._events: list[tuple[float, int, _Event]] = []
        self._seq = 0
        self._runs: dict[str, CronRunState] = {}  # run_id -> state
        self._run_tasks: dict[str, asyncio.Task] = {}
        self._last_store_mtime: float = 0.0
        self._store_poll_interval: float = 5.0  # seconds

    def _get_store_mtime(self) -> float:
        """Return mtime of the cron_jobs.json file, or 0.0 if unavailable."""
        try:
            return self._store.path.stat().st_mtime
        except OSError:
            return 0.0

    def _sync_store_mtime(self) -> None:
        """Snapshot current store file mtime to avoid redundant reloads."""
        self._last_store_mtime = self._get_store_mtime()

    async def _check_store_changed(self) -> bool:
        """If cron_jobs.json was modified or deleted externally, reload and return True."""
        mtime = self._get_store_mtime()
        # Detect: file modified (mtime changed, both nonzero),
        #         file deleted (mtime became 0.0 from nonzero),
        #         file recreated (mtime became nonzero from 0.0).
        # Skip: no change (mtime == last), or both 0.0 (never had a file).
        if mtime != self._last_store_mtime and (mtime or self._last_store_mtime):
            logger.info(
                "[Cron] store file changed (mtime %.3f -> %.3f), reloading",
                self._last_store_mtime,
                mtime,
            )
            await self.reload()
            return True
        return False

    def is_running(self) -> bool:
        return self._running

    async def _cancel_agent_session(
        self,
        state: CronRunState,
        *,
        reason: str = "ghost",
    ) -> None:
        """Fire-and-forget: 向 AgentServer 发送 CHAT_CANCEL 中断请求。

        触发场景:
        - ``reason="ghost"``: cron_jobs.json 被删除或 job 被移除后，gateway
          的 asyncio Task 被 task.cancel() 取消，但这只终止了 gateway 端
          等待响应的协程。AgentServer 不知道请求已被取消，会继续执行 LLM
          调用。此方法主动发送中断请求，让后端也停止处理，彻底消灭"幽灵任务"。
        - ``reason="timeout"``: unary 超时分支（_run_unary_cron_job）在
          ``asyncio.wait_for`` 超时后仅取消了 gateway 端等待协程，若不
          主动 cancel，AgentServer 端的 task 会继续跑到完成——飞书等渠道
          会先收到"超时"文案，但任务实际仍在后台正常完成，结果到达 gateway
          时已无人接收而被丢弃（用户反馈"超时但结果正常完成"的矛盾现象）。

        session_id 必须用真实执行会话 ``state.exec_session_id``（格式
        ``cron_{ts}_{job.id}``）。AgentServer 的 ``_stop_session_interrupt_work``
        按 ``request.session_id`` 定位 in-flight task；若用旧占位
        ``cron_{job_id}`` 与真实会话不匹配，cancel 请求落不到点上。
        """
        target_session_id = (state.exec_session_id or "").strip() or f"cron_{state.job_id}"
        job = self._jobs.get(state.job_id)
        try:
            interrupt_env = e2a_from_agent_fields(
                request_id=f"cron-cancel-{state.run_id}",
                channel_id="__cron__",
                session_id=target_session_id,
                req_method=ReqMethod.CHAT_CANCEL,
                params={"cron": {"job_id": state.job_id, "run_id": state.run_id}},
                is_stream=False,
                timestamp=self._now_fn(),
                user_id=str(getattr(job, "user_id", "") or "").strip() or None,
            )
            await self._agent_client.send_request(interrupt_env)
            logger.info(
                "[Cron] AgentServer interrupt sent (%s): "
                "job_id=%s run_id=%s session_id=%s",
                reason,
                state.job_id,
                state.run_id,
                target_session_id,
            )
        except (OSError, RuntimeError) as exc:
            # Fire-and-forget: 网络断开、连接超时或 AgentServer 不可达
            # 都不影响主流程，只是最佳努力的中断
            logger.warning(
                "[Cron] AgentServer interrupt failed (%s, non-critical): "
                "job_id=%s run_id=%s session_id=%s error=%s",
                reason,
                state.job_id,
                state.run_id,
                target_session_id,
                exc,
            )

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self.reload()
        self._task = asyncio.create_task(self._loop(), name="cron-scheduler")
        logger.info("[Cron] scheduler started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # best-effort cancel in-flight runs
        for t in list(self._run_tasks.values()):
            if not t.done():
                t.cancel()
        self._run_tasks.clear()
        logger.info("[Cron] scheduler stopped")

    async def reload(self) -> None:
        """Reload jobs from store and rebuild the event queue.

        When the store is empty (e.g. cron_jobs.json was deleted externally),
        all in-memory running tasks for jobs that no longer exist in the store
        are cancelled and their state cleaned up, preventing "ghost" tasks that
        continue executing and pushing results despite having no persistent record.
        """
        jobs = await self._store.list_jobs()
        self._jobs = {j.id: j for j in jobs}
        new_job_ids = set(self._jobs.keys())

        # 保留飞行中的 push_update 事件，但仅限于仍存在于 store 中的 job。
        # 不存在的 job 的 push_update 不应继续推送：store 已无记录意味着
        # 用户已明确删除了这些任务（或删除了整个文件），它们的运行结果
        # 也应该中止，否则会形成"幽灵任务"——/cron 显示无任务但后台仍在推送。
        pending_push_updates = [
            (at_ts, seq, ev)
            for at_ts, seq, ev in self._events
            if ev.kind == "push_update" and ev.job_id in new_job_ids
        ]
        self._events.clear()
        # 不重置 _seq：保留的 push_update 事件携带原始 seq 值，
        # 若重置为 0，新调度事件的 seq 会从 1 开始递增，与保留事件的 seq 碰撞。
        # 当 at_ts 也相同时，heapq 元组比较回退到 _Event 比较（即使 _Event
        # 已加 order=True，仍应避免 seq 碰撞以保证排序语义正确）。
        for item in pending_push_updates:
            heapq.heappush(self._events, item)

        # 取消并清理不再存在于 store 中的运行任务（ghost tasks）。
        # 这些 task 的 job 已经没有持久化记录了，继续运行只会产生
        # 无法被用户管理（无 job_id 可删除）的后台任务。
        # 同时向 AgentServer 发送 CHAT_CANCEL 中断请求，否则
        # task.cancel() 只取消 gateway 端的等待协程，AgentServer
        # 仍会继续执行 LLM 请求（用户看到的"后台还在派发"现象）。
        ghost_run_ids = [
            rid for rid, state in self._runs.items()
            if state.job_id not in new_job_ids
        ]
        for rid in ghost_run_ids:
            state = self._runs[rid]
            task = self._run_tasks.pop(rid, None)
            if task is not None and not task.done():
                logger.info(
                    "[Cron] cancelling ghost run task: job_id=%s run_id=%s "
                    "(job no longer in store)",
                    state.job_id,
                    rid,
                )
                task.cancel()
                # 向 AgentServer 发送 fire-and-forget 中断请求，让后端真正停止 LLM 处理
                asyncio.create_task(
                    self._cancel_agent_session(state),
                    name=f"cron-ghost-cancel-{state.job_id}",
                )
            self._runs.pop(rid, None)

        now = self._now_fn()
        for job in jobs:
            try:
                push_dt, wake_dt, run_id = self._compute_next_run(job, now_ts=now)
            except Exception as exc:  # noqa: BLE001
                if self._is_croniter_no_next_date(exc):
                    # 已过期的 one-shot：标记 expired 并停用，避免 UI 仍显示"运行中/已暂停"。
                    # 这里故意不提前 continue 掉 disabled 的任务——一个单次任务如果在到期前
                    # 被手动暂停，同样需要能被检测到"已经没有下一次执行时间"从而转入过期态，
                    # 否则会永远卡在"已暂停"（见 2026-07-16 bugfix）。
                    # proactive job 的 enabled 由 ConfigPanel 开关管，scheduler 不碰。
                    # 幂等保护：已经是 expired+disabled 状态的任务不再重复写回——
                    # store.update_job 每次都会刷 updated_at 和 cron_jobs.json 的 mtime，
                    # 若对已过期任务每次 reload 都写一次，会形成"mtime 变 → _check_store_changed
                    # 触发 reload → 又写一次 → mtime 又变"的循环（每 5 秒一轮），导致过期任务的
                    # updated_at 一直是最新的、永远排在任务列表最前面（PR #3756 review 期间
                    # 发现的 Bug7 回归）
                    is_proactive = getattr(job, "mode", "") == "proactive.tick"
                    already_expired = bool(getattr(job, "expired", False))
                    already_disabled = is_proactive or not bool(getattr(job, "enabled", True))
                    if not (already_expired and already_disabled):
                        try:
                            patch = {"expired": True}
                            if not is_proactive:
                                patch["enabled"] = False
                                job.enabled = False
                            job.expired = True
                            await self._store.update_job(job.id, patch)
                        except Exception as update_exc:  # noqa: BLE001
                            logger.warning(
                                "[Cron] mark expired failed job=%s: %s",
                                job.id,
                                update_exc,
                            )
                else:
                    logger.warning("[Cron] compute next run failed job=%s: %s", job.id, exc)
                continue
            if not job.enabled:
                continue
            # 幂等保护：reload 清空事件队列后重新排入，但同一 run_id
            # 可能已执行中或已完成（wake_offset 过大时 wake_dt 在过去，
            # 每次 reload 都会立刻触发）。跳过已活跃 run 的 wake 事件，
            # 但 push 事件仍需排入——_on_push 内部有 pushed_final 兜底。
            existing = self._runs.get(run_id)
            already_active = (
                existing is not None
                and existing.status in ("running", "succeeded", "failed")
            ) or (
                run_id in self._run_tasks
                and not self._run_tasks[run_id].done()
            )
            # 崩溃恢复保护：gateway 重启后 _runs/_run_tasks 清空，already_active
            # 失效。若 wake_dt 已在过去（本应在崩溃前触发的 wake），重排 wake 会
            # 立即触发 _on_wake 创建新 agent task（新 session_id），而 AgentServer
            # 上原 task 可能还在跑 → 重复执行。此时跳过 wake 重排，宁可丢结果也
            # 不重复触发。push 事件也不排（崩溃后 _runs 无 state，_on_push 会重建
            # state 并推占位——但此时根本没有执行，推占位会误导用户）。
            # 仅当内存无该 run 记录时启用此保护（existing is None 即崩溃重启特征）。
            wake_already_passed = wake_dt.timestamp() <= now
            crash_recovery_skip = existing is None and wake_already_passed
            if not already_active and not crash_recovery_skip:
                self._schedule_event(wake_dt, "wake", job.id, run_id)
            # push 事件：已 active 时仍需排入——_on_push 内部有 pushed_final 兜底，
            # 若 agent 还没完成到达 push 时间会推"正在执行中"占位，完成后 push_update
            # 仍可补发最终结果。崩溃恢复分支除外（无 state、无执行，推占位会误导）。
            if not crash_recovery_skip:
                self._schedule_event(push_dt, "push", job.id, run_id)
            if crash_recovery_skip:
                logger.info(
                    "[Cron] reload skip wake+push (crash recovery, wake_dt in past) "
                    "job=%s run_id=%s wake_dt=%s",
                    job.id, run_id, wake_dt.isoformat(),
                )

        self._sync_store_mtime()
        self._reload_event.set()

    async def trigger_run_now(self, job_id: str) -> str:
        info = await self.trigger_run_now_info(job_id)
        return str(info["run_id"])

    async def trigger_run_now_info(self, job_id: str) -> dict[str, str]:
        job_id = str(job_id or "").strip()
        job = self._jobs.get(job_id) or await self._store.get_job(job_id)
        if job is None:
            raise KeyError("job not found")
        now = datetime.now(tz=ZoneInfo(job.timezone))
        push_dt = now
        wake_dt = now
        run_id = f"{job.id}:{int(push_dt.timestamp())}"
        channel_id, exec_session_id = self._make_execution_context(job)
        self._runs[run_id] = CronRunState(
            run_id=run_id,
            job_id=job.id,
            wake_at_iso=wake_dt.isoformat(),
            push_at_iso=push_dt.isoformat(),
            job_name=job.name,
            targets=job.targets,
            session_id=job.session_id,
            chat_type=job.chat_type,
            timezone=job.timezone,
            exec_channel_id=channel_id,
            exec_session_id=exec_session_id,
        )
        self._schedule_event(wake_dt, "wake", job.id, run_id)
        self._schedule_event(push_dt, "push", job.id, run_id)
        self._reload_event.set()
        return {"run_id": run_id, "session_id": exec_session_id}

    def _make_execution_context(self, job: CronJob) -> tuple[str, str]:
        ts = format(int(time.time() * 1000), "x")
        mode = str(job.mode or CRON_JOB_DEFAULT_MODE).strip() or CRON_JOB_DEFAULT_MODE
        if is_team_cron_mode(mode):
            return _resolve_cron_execution_context(
                job,
                ts=ts,
                message_handler=self._message_handler,
            )
        return "__cron__", f"cron_{ts}_{job.id}"

    async def _allocate_single_agent_session(
        self,
        job: CronJob,
        *,
        mode: str,
        project_dir: str,
        run_id: str,
    ) -> str:
        cron_user_id = str(job.user_id or "").strip()
        env = e2a_from_agent_fields(
            request_id=f"cron-session-create-{run_id}",
            channel_id="__cron__",
            req_method=ReqMethod.SESSION_CREATE,
            params={
                "create_token": f"cron:{run_id}",
                "mode": mode,
                "is_swarm": False,
                "project_id": job.project_id or "",
                "project_dir": project_dir,
                "work_mode": job.work_mode or DEFAULT_WEB_WORK_MODE,
                "model_name": job.model_name or None,
                "cron_id": job.id,
                "user_id": cron_user_id,
            },
            is_stream=False,
            timestamp=self._now_fn(),
            user_id=cron_user_id or None,
        )
        response = await self._agent_client.send_request(env)
        payload = dict(response.payload or {}) if isinstance(response.payload, dict) else {}
        if not response.ok:
            raise RuntimeError(str(payload.get("error") or "cron session.create failed"))
        session_id = str(
            payload.get("session_id") or payload.get("sessionId") or ""
        ).strip()
        if not session_id:
            raise RuntimeError("cron session.create returned empty session_id")
        return session_id

    def _schedule_event(self, at_dt: datetime, kind: str, job_id: str, run_id: str) -> None:
        at_ts = float(at_dt.timestamp())
        self._seq += 1
        ev = _Event(at_ts=at_ts, seq=self._seq, kind=kind, job_id=job_id, run_id=run_id)
        heapq.heappush(self._events, (ev.at_ts, ev.seq, ev))
        # 若事件已在 1 秒内到期（如 push_update 补发），需唤醒主循环，否则会等到 timeout（可能 10 分钟）
        if at_ts <= self._now_fn() + 1.0:
            self._reload_event.set()

    _MISSED_TRIGGER_WINDOW_SECONDS = 10.0

    def _compute_next_run(self, job: CronJob, *, now_ts: float) -> tuple[datetime, datetime, str]:
        tz = ZoneInfo(job.timezone)
        base = datetime.fromtimestamp(now_ts, tz=tz)
        try:
            push_dt = _cron_next_push_dt(job.cron_expr, base)
        except Exception as original_exc:
            if not self._is_croniter_no_next_date(original_exc):
                raise original_exc
            from croniter import croniter
            field_count = len(job.cron_expr.strip().split())
            second_at_beginning = field_count == 7
            it = croniter(job.cron_expr, base, second_at_beginning=second_at_beginning)
            prev_dt = it.get_prev(datetime)
            if prev_dt is not None and isinstance(prev_dt, datetime):
                if prev_dt.tzinfo is None:
                    prev_dt = prev_dt.replace(tzinfo=tz)
                elapsed = (base.timestamp() - prev_dt.timestamp())
                if elapsed <= self._MISSED_TRIGGER_WINDOW_SECONDS:
                    logger.info(
                        "[Cron] one-shot job=%s missed trigger by %.1fs (within %ss window), "
                        "scheduling immediate execution instead of marking expired",
                        job.id, elapsed, self._MISSED_TRIGGER_WINDOW_SECONDS,
                    )
                    push_dt = prev_dt
                else:
                    raise original_exc
            else:
                raise original_exc
        # proactive.tick 无视 wake_offset——到点就执行，不提前 wake。
        # 否则 wake_dt = push_dt - offset 可能在过去，导致 reschedule 后立刻
        # 触发 → 每 6 秒循环（实测 14:26-14:28 反复 triggering 同一 run_id）。
        if getattr(job, "mode", "") == "proactive.tick":
            offset = 0
        else:
            offset = max(0, int(job.wake_offset_seconds or 0))
        wake_dt = push_dt - timedelta(seconds=offset)
        run_id = f"{job.id}:{int(push_dt.timestamp())}"
        return push_dt, wake_dt, run_id

    @staticmethod
    def _is_croniter_no_next_date(exc: Exception) -> bool:
        """croniter 找不到下一次日期（通常为单次 year 固定为过去）时视为过期。"""
        return (
            exc.__class__.__name__ == "CroniterBadDateError"
            or "failed to find next date" in str(exc)
        )

    async def _loop(self) -> None:
        while self._running:
            try:
                if not self._events:
                    self._reload_event.clear()
                    try:
                        await asyncio.wait_for(
                            self._reload_event.wait(),
                            timeout=self._store_poll_interval,
                        )
                    except asyncio.TimeoutError:
                        await self._check_store_changed()
                    continue

                now = self._now_fn()
                at_ts, _, ev = self._events[0]
                delay = max(0.0, at_ts - now)

                if delay > 0:
                    self._reload_event.clear()
                    try:
                        await asyncio.wait_for(
                            self._reload_event.wait(),
                            timeout=min(delay, self._store_poll_interval),
                        )
                        continue
                    except asyncio.TimeoutError:
                        # Check if store changed before processing the event
                        if await self._check_store_changed():
                            continue
                        # If delay hasn't elapsed yet, loop back to re-check
                        if self._now_fn() < at_ts:
                            continue

                # due
                heapq.heappop(self._events)
                await self._handle_event(ev)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Cron] scheduler loop error: %s", exc, exc_info=True)
                await asyncio.sleep(0.5)

    async def _handle_event(self, ev: _Event) -> None:
        job = self._jobs.get(ev.job_id)

        # Handle proactive.tick mode: send WebSocket request to AgentServer
        if job is not None and job.mode == "proactive.tick" and ev.kind == "wake":
            logger.info("[Cron] triggering proactive.tick for job=%s run_id=%s", job.id, ev.run_id)
            try:
                # Create run state for tracking
                tz = ZoneInfo(job.timezone)
                push_ts = int(ev.run_id.split(":")[-1])
                push_dt = datetime.fromtimestamp(push_ts, tz=tz)
                wake_dt = push_dt - timedelta(seconds=max(0, int(job.wake_offset_seconds or 0)))
                state = CronRunState(
                    run_id=ev.run_id,
                    job_id=job.id,
                    wake_at_iso=wake_dt.isoformat(),
                    push_at_iso=push_dt.isoformat(),
                    job_name=job.name,
                    targets=job.targets,
                    session_id=job.session_id,
                    chat_type=job.chat_type,
                    timezone=job.timezone,
                )
                self._runs[ev.run_id] = state
                state.status = "running"
                state.started_at = self._now_fn()

                # Send proactive.tick request to AgentServer via WebSocket
                envelope = e2a_from_agent_fields(
                    request_id=f"proactive-tick-{ev.run_id}",
                    channel_id="__cron__",
                    session_id=f"cron_{job.id}",
                    req_method=ReqMethod.PROACTIVE_TICK,
                    params={"target_channel": job.targets or None},
                    is_stream=False,
                    timestamp=self._now_fn(),
                    metadata={"cron": {"job_id": job.id, "run_id": ev.run_id}},
                    user_id=str(job.user_id or "").strip() or None,
                )
                resp = await self._agent_client.send_request(envelope)

                state.finished_at = self._now_fn()
                if resp.ok:
                    success = resp.payload.get("success", False) if resp.payload else False
                    state.status = "succeeded" if success else "skipped"
                    # success 现在表示"是否真的推送了推荐"(cooldown/无新内容/配额已满均为 False)。
                    # 文案与实际一致：不再出现"已发送"但实际没内容的情况。
                    state.result_text = "推荐已发送" if success else "本次无需推荐（冷却中或无新内容）"
                    logger.info("[Cron] proactive.tick completed job=%s success=%s", job.id, success)
                else:
                    state.status = "failed"
                    state.error = resp.payload.get("error", "unknown") if resp.payload else "unknown"
                    logger.warning("[Cron] proactive.tick failed job=%s: %s", job.id, state.error)
            except Exception as exc:
                logger.warning("[Cron] proactive.tick failed job=%s: %s", job.id, exc, exc_info=True)
            # proactive.tick 走专属分支提前 return，跳过下方 643 通用 reschedule。
            # 必须显式排下一次 wake，否则只在 reload 时才排，期间漏跑
            # （实测：19:56 tick 完，20:00 整点不触发，因为没 reload）。
            try:
                push_dt, wake_dt, next_run_id = self._compute_next_run(job, now_ts=self._now_fn())
                if push_dt.timestamp() <= self._now_fn():
                    logger.info(
                        "[Cron] one-shot job=%s push_dt is in the past (%s), "
                        "marking expired instead of rescheduling",
                        job.id, push_dt.isoformat(),
                    )
                    job.expired = True
                    await self._store.update_job(job.id, {"expired": True})
                    return
                self._schedule_event(wake_dt, "wake", job.id, next_run_id)
                self._schedule_event(push_dt, "push", job.id, next_run_id)
            except Exception as exc:  # 兜底：reschedule 失败不阻断本次 tick 结果（下个 reload 会重排）
                if self._is_croniter_no_next_date(exc):
                    try:
                        is_proactive = getattr(job, "mode", "") == "proactive.tick"
                        patch = {"expired": True}
                        if not is_proactive:
                            patch["enabled"] = False
                            job.enabled = False
                        job.expired = True
                        await self._store.update_job(job.id, patch)
                    except Exception as update_exc:  # 兜底：标记过期失败仅告警，不影响主流程
                        logger.warning("[Cron] mark expired after proactive.tick failed job=%s: %s", job.id, update_exc)
                else:
                    logger.warning("[Cron] reschedule after proactive.tick failed job=%s: %s", job.id, exc)
            return

        if job is None and ev.kind != "push_update":
            return
        # For wake/push/push_update events: if the job no longer exists in the
        # persistent store (e.g. cron_jobs.json was deleted), skip execution.
        # This prevents "ghost tasks" — tasks that continue running and pushing
        # results even though the user sees no tasks in /cron and has no job_id
        # to manage or delete them.
        # Note: push_update was previously exempted to deliver already-computed
        # results, but that exemption creates the ghost task problem. When the
        # store is gone, the job is gone, and its results should not be pushed.
        store_job = await self._store.get_job(ev.job_id)
        if store_job is None:
            # If job was in memory but not in store, reload to clear stale data
            if ev.kind in ("wake", "push") and job is not None:
                logger.info(
                    "[Cron] job %s no longer in store (file may have been deleted), "
                    "skipping event %s and triggering reload",
                    ev.job_id, ev.kind,
                )
                await self.reload()
            elif ev.kind == "push_update":
                logger.info(
                    "[Cron] push_update skipped: job %s no longer in store "
                    "(file may have been deleted), skipping ghost push job_id=%s run_id=%s",
                    ev.job_id, ev.job_id, ev.run_id,
                )
            return
        if job is None and ev.kind == "push_update":
            # Job not in _jobs but still in store (e.g. disabled/expired job):
            # rebuild from state for routing purposes. This is legitimate —
            # push_update for a disabled one-shot job that's still in the store
            # must still deliver its result.
            state = self._runs.get(ev.run_id)
            if state is None:
                logger.info("[Cron] push_update skipped: no state and no job job_id=%s run_id=%s", ev.job_id, ev.run_id)
                return
            job = CronJob(
                id=state.job_id,
                name=state.job_name or "",
                enabled=False,
                expired=False,
                cron_expr="",
                timezone=state.timezone or "Asia/Shanghai",
                targets=state.targets or "",
                session_id=state.session_id,
                chat_type=state.chat_type,
            )
            logger.info("[Cron] push_update using rebuilt job from state job_id=%s run_id=%s", ev.job_id, ev.run_id)
        # push_update 是对已触发任务的补发，即使单次任务已过期也必须放行，否则真正结果永远发不出去
        if not job.enabled and ev.kind != "push_update":
            return

        if ev.kind == "wake":
            await self._on_wake(job, ev.run_id)
        elif ev.kind == "push":
            await self._on_push(job, ev.run_id)
            if job.delete_after_run:
                # 不删除，改为标记过期（与自然过期的一次性任务行为一致）
                logger.info("[Cron] delete_after_run job=%s, marking expired after push", job.id)
                try:
                    is_proactive = getattr(job, "mode", "") == "proactive.tick"
                    patch = {"expired": True}
                    if not is_proactive:
                        patch["enabled"] = False
                        job.enabled = False
                    job.expired = True
                    await self._store.update_job(job.id, patch)
                except Exception as update_exc:
                    logger.warning("[Cron] mark expired after push failed job=%s: %s", job.id, update_exc)
                return
            # proactive.tick 的 wake 分支（525）已处理执行 + reschedule，push 事件
            # 在此仅 _on_push（已 return，不做推送）。若 push 也 reschedule，会和
            # wake 的 reschedule 重复排 wake 事件，形成 push→reschedule→wake→
            # reschedule→push 循环（实测每 6 秒反复 triggering 同一 run_id）。
            # proactive.tick 跳过 push 的 reschedule，由 wake 分支统一管调度。
            if getattr(job, "mode", "") == "proactive.tick":
                return
            try:
                push_dt, wake_dt, next_run_id = self._compute_next_run(job, now_ts=self._now_fn())
                if push_dt.timestamp() <= self._now_fn():
                    logger.info(
                        "[Cron] one-shot job=%s push_dt is in the past (%s), "
                        "marking expired instead of rescheduling",
                        job.id, push_dt.isoformat(),
                    )
                    job.enabled = False
                    job.expired = True
                    await self._store.update_job(job.id, {"enabled": False, "expired": True})
                    return
                self._schedule_event(wake_dt, "wake", job.id, next_run_id)
                self._schedule_event(push_dt, "push", job.id, next_run_id)
            except Exception as exc:  # noqa: BLE001
                if self._is_croniter_no_next_date(exc):
                    # 执行后无下一次：将任务标记为过期并停用。
                    try:
                        job.enabled = False
                        job.expired = True
                        await self._store.update_job(job.id, {"enabled": False, "expired": True})
                    except Exception as update_exc:  # noqa: BLE001
                        logger.warning(
                            "[Cron] mark expired after push failed job=%s: %s",
                            job.id,
                            update_exc,
                        )
                else:
                    logger.warning("[Cron] compute next run failed after push job=%s: %s", job.id, exc)
        elif ev.kind == "push_update":
            await self._on_push_update(job, ev.run_id)

    async def _on_wake(self, job: CronJob, run_id: str) -> None:
        state = self._runs.get(run_id)
        if state is None:
            tz = ZoneInfo(job.timezone)
            # Approx from run_id timestamp suffix
            try:
                push_ts = int(run_id.split(":")[-1])
            except Exception:
                push_ts = int(self._now_fn())
            push_dt = datetime.fromtimestamp(push_ts, tz=tz)
            wake_dt = push_dt - timedelta(seconds=max(0, int(job.wake_offset_seconds or 0)))
            state = CronRunState(
                run_id=run_id,
                job_id=job.id,
                wake_at_iso=wake_dt.isoformat(),
                push_at_iso=push_dt.isoformat(),
                job_name=job.name,
                targets=job.targets,
                session_id=job.session_id,
                chat_type=job.chat_type,
                timezone=job.timezone,
            )
            self._runs[run_id] = state

        # 幂等保护：reload 可能对同一 run_id 重复排入 wake 事件。
        # 如果该 run 已完成（succeeded/failed）或已有结果文本，不再重复执行 agent。
        if state.status in ("succeeded", "failed"):
            logger.info(
                "[Cron] _on_wake skipped: already %s run_id=%s job=%s",
                state.status, run_id, job.id,
            )
            return
        if state.result_text and state.pushed_final:
            logger.info(
                "[Cron] _on_wake skipped: result already pushed run_id=%s job=%s",
                run_id, job.id,
            )
            return
        if run_id in self._run_tasks and not self._run_tasks[run_id].done():
            return

        async def _run_agent() -> None:
            state.status = "running"
            state.started_at = self._now_fn()
            ok = False
            mode = CRON_JOB_DEFAULT_MODE
            channel_id = ""
            exec_session_id = ""
            envelope = None
            try:
                mode = str(job.mode or CRON_JOB_DEFAULT_MODE).strip() or CRON_JOB_DEFAULT_MODE
                if state.exec_channel_id and state.exec_session_id:
                    channel_id = state.exec_channel_id
                    exec_session_id = state.exec_session_id
                else:
                    channel_id, exec_session_id = self._make_execution_context(job)
                    state.exec_channel_id = channel_id
                    state.exec_session_id = exec_session_id
                # 解析 project_dir 供 AgentServer 写入会话归属（与 project_id 联动）
                try:
                    from jiuwenswarm.server.runtime.session import project_store as _ps
                    exec_project_dir = _ps.get_project_dir_by_id(job.project_id)
                except Exception as pdir_exc:  # noqa: BLE001
                    logger.warning(
                        "[Cron] resolve project_dir failed job=%s: %s", job.id, pdir_exc,
                    )
                    exec_project_dir = ""
                if not is_team_cron_mode(mode):
                    exec_session_id = await self._allocate_single_agent_session(
                        job,
                        mode=mode,
                        project_dir=exec_project_dir,
                        run_id=run_id,
                    )
                    state.exec_channel_id = "__cron__"
                    state.exec_session_id = exec_session_id
                cron_meta = {
                    "job_id": job.id,
                    "job_name": job.name,
                    "run_id": run_id,
                    "push_at": state.push_at_iso,
                    "wake_at": state.wake_at_iso,
                    "current_time": datetime.fromtimestamp(self._now_fn(), tz=ZoneInfo(job.timezone)).isoformat(),
                }
                params: dict[str, Any] = {
                    "content": job.description,
                    "query": job.description,
                    "mode": mode,
                    "cron": cron_meta,
                    "cron_id": job.id,
                    "project_id": job.project_id or "",
                    "project_dir": exec_project_dir,
                    "work_mode": job.work_mode or DEFAULT_WEB_WORK_MODE,
                }
                if job.model_name:
                    params["model_name"] = job.model_name
                envelope = e2a_from_agent_fields(
                    request_id=f"cron-{run_id}",
                    channel_id=channel_id,
                    session_id=exec_session_id,
                    req_method=ReqMethod.CHAT_SEND,
                    params=params,
                    is_stream=is_team_cron_mode(mode),
                    timestamp=self._now_fn(),
                    metadata={"cron": {"job_id": job.id, "run_id": run_id}},
                    user_id=job.user_id,
                )
                if not str(job.user_id or "").strip():
                    logger.warning(
                        "[Cron] job has no user_id, faas X-Session-Context will be omitted: "
                        "job_id=%s",
                        job.id,
                    )
                if is_team_cron_mode(mode):
                    timeout_seconds = resolve_cron_job_timeout_seconds(job)
                    text, ok = await self._run_team_stream_job(
                        envelope=envelope,
                        exec_session_id=exec_session_id,
                        cron_meta=cron_meta,
                        timeout_seconds=timeout_seconds,
                    )
                else:
                    envelope.is_stream = False
                    timeout_seconds = resolve_cron_job_timeout_seconds(job)
                    text, ok = await self._run_unary_cron_job(
                        envelope=envelope,
                        timeout_seconds=timeout_seconds,
                        state=state,
                    )
                await self._mark_last_session_ready(job, exec_session_id)
                state.result_text = text
                state.status = "succeeded" if ok else "failed"
            except asyncio.CancelledError:
                state.status = "failed"
                state.error = "cancelled"
                # Ghost task: cancelled by reload because job no longer in store.
                # Do NOT schedule push_update — the user has removed this job and
                # should not see any result from it. Raising CancelledError here
                # so the finally block can detect it via state.error == "cancelled"
                # and skip push_update scheduling.
                raise
            except Exception as exc:  # noqa: BLE001
                state.status = "failed"
                state.error = str(exc)
            finally:
                state.finished_at = self._now_fn()
                is_cancelled_ghost = state.error == "cancelled"
                should_deliver_result = bool(state.result_text) and not is_cancelled_ghost
                # Ensure failed runs also produce result_text so push logic can deliver it.
                # But for cancelled ghost tasks, skip — no result should be pushed for
                # a job the user has removed.
                if not state.result_text and state.error and not is_cancelled_ghost:
                    state.result_text = f"[cron] 任务执行失败: {state.error}"
                if state.result_text and not ok and not is_cancelled_ghost:
                    append_history_record(
                        session_id=exec_session_id,
                        request_id=getattr(envelope, "request_id", ""),
                        channel_id=job.targets or channel_id,
                        role="assistant",
                        event_type="chat.final",
                        content=state.result_text,
                        timestamp=self._now_fn(),
                        mode=mode,
                    )
                if not state.pushed_final and state.result_text and not is_cancelled_ghost:
                    logger.info(
                        "[Cron] scheduling push_update after agent finished "
                        "job=%s run_id=%s text_len=%d",
                        job.id,
                        run_id,
                        len(state.result_text or ""),
                    )
                    push_dt = datetime.fromisoformat(state.push_at_iso)
                    now_dt = datetime.fromtimestamp(self._now_fn(), tz=ZoneInfo(job.timezone))
                    scheduled_dt = max(now_dt, push_dt)
                    self._schedule_event(
                        scheduled_dt,
                        "push_update", job.id, run_id,
                    )

        task = asyncio.create_task(_run_agent(), name=f"cron-run-{job.id}")
        self._run_tasks[run_id] = task

    async def _mark_last_session_ready(self, job: CronJob, exec_session_id: str) -> None:
        """Record the execution session after the agent request has accepted it."""
        try:
            await self._store.update_job(job.id, {"last_session_id": exec_session_id})
            job.last_session_id = exec_session_id
        except Exception as lsid_exc:  # noqa: BLE001
            logger.warning(
                "[Cron] update last_session_id failed job=%s: %s", job.id, lsid_exc,
            )

    async def _cancel_cron_team_agent_session(
        self,
        *,
        envelope: Any,
        exec_session_id: str,
        mode: str = "team",
    ) -> None:
        """Stop lingering AgentServer team work after cron stream ends or times out."""
        cancel_fn = getattr(self._message_handler, "_cancel_agent_work_for_session", None)
        if not callable(cancel_fn):
            logger.warning(
                "[Cron] cannot cancel team agent: message_handler missing "
                "_cancel_agent_work_for_session request_id=%s",
                getattr(envelope, "request_id", ""),
            )
            return
        channel_id = str(
            getattr(envelope, "channel", None)
            or getattr(envelope, "channel_id", None)
            or ""
        ).strip()
        cancel_msg = Message(
            id=f"cron-cancel-{getattr(envelope, 'request_id', '')}",
            type="req",
            channel_id=channel_id,
            session_id=exec_session_id,
            params={
                "intent": "cancel",
                "mode": mode,
                "session_id": exec_session_id,
            },
            req_method=ReqMethod.CHAT_CANCEL,
            timestamp=self._now_fn(),
            ok=True,
        )
        try:
            await cancel_fn(
                cancel_msg,
                exec_session_id,
                publish_interrupt_result=False,
                channel_id=channel_id or None,
                cancel_gateway_tasks=False,
            )
            logger.info(
                "[Cron] cancelled team agent session request_id=%s session_id=%s",
                getattr(envelope, "request_id", ""),
                exec_session_id,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning(
                "[Cron] failed to cancel team agent session request_id=%s session_id=%s error=%s",
                getattr(envelope, "request_id", ""),
                exec_session_id,
                exc,
            )

    async def _run_unary_cron_job(
        self,
        *,
        envelope: Any,
        timeout_seconds: float,
        state: CronRunState,
    ) -> tuple[str, bool]:
        try:
            resp = await asyncio.wait_for(
                self._agent_client.send_request(envelope),
                timeout=timeout_seconds,
            )
            text = _extract_text_from_agent_payload(resp.payload)
            ok = bool(resp.ok)
            # 成功但 result_text 为空：响应格式与 _extract_text_from_agent_payload
            # 的识别字段不匹配（如 E2A result 非 dict、或字段名不在 error/content/
            # heartbeat/text 之内）。若放任空字符串进入 state.result_text，会触发
            # _on_push_update 的 "empty result_text" 跳过 → 真实结果永不补发、
            # 占位永久留在界面。这里降级为失败并生成可见文案，触发 error 兜底通道。
            if ok and not text:
                payload_keys = (
                    list(resp.payload.keys())
                    if isinstance(resp.payload, dict)
                    else type(resp.payload).__name__
                )
                logger.warning(
                    "[Cron] unary succeeded but result_text empty request_id=%s payload_keys=%s",
                    getattr(envelope, "request_id", ""),
                    payload_keys,
                )
                return "[cron] 任务执行完成但未返回结果内容", False
            return text, ok
        except asyncio.TimeoutError:
            timeout_min = max(1, int(timeout_seconds // 60))
            logger.warning(
                "[Cron] unary request timed out after %ss request_id=%s",
                timeout_seconds,
                getattr(envelope, "request_id", ""),
            )
            # asyncio.wait_for 超时仅取消 gateway 端等待协程，AgentServer 端
            # 的 task 不受影响会继续跑到完成——飞书等渠道会先收到"超时"文案，
            # 但任务实际仍在后台正常完成，结果回到 gateway 时已无人接收而被
            # 丢弃（用户反馈"超时但结果正常完成"的矛盾现象）。对齐 team 流式
            # 超时分支（_run_team_stream_job）主动发 CHAT_CANCEL，让后端真正停止。
            await self._cancel_agent_session(state, reason="timeout")
            return f"[cron] 任务执行超时（>{timeout_min}min）", False

    async def _run_team_stream_job(
        self,
        *,
        envelope: Any,
        exec_session_id: str,
        cron_meta: dict[str, Any],
        timeout_seconds: float,
    ) -> tuple[str, bool]:
        """Run a team-mode cron job via streaming so SwarmFlow events reach the TUI."""
        request_metadata = dict(envelope.channel_context or {})
        request_metadata.setdefault("source", "cron")
        request_metadata.setdefault("cron", cron_meta)

        round_state = new_cron_team_round_state()
        consume_meta: dict[str, Any] = {"ok": True, "ended_early": False}
        stream_gen = self._agent_client.send_request_stream(envelope)

        async def _consume() -> tuple[str, bool]:
            publish_chunk = getattr(self._message_handler, "publish_stream_chunk", None)
            if publish_chunk is None:
                logger.warning(
                    "[Cron] message_handler.publish_stream_chunk unavailable; "
                    "team stream chunks will not be forwarded request_id=%s",
                    getattr(envelope, "request_id", ""),
                )
            try:
                async for chunk in stream_gen:
                    if callable(publish_chunk):
                        await publish_chunk(
                            chunk,
                            session_id=exec_session_id,
                            request_metadata=request_metadata,
                        )
                    payload = chunk.payload if isinstance(chunk.payload, dict) else None
                    event_type = str((payload or {}).get("event_type") or "").strip()
                    if payload:
                        apply_cron_team_round_event(round_state, payload)
                        if event_type == "chat.error":
                            consume_meta["ok"] = False
                        # _extract_workflow_result_text normalizes result JSON and
                        # walks phases; run it after apply_cron_team_round_event so
                        # the richer value wins over the plain summary fallback.
                        if event_type == "workflow.updated":
                            workflow_text = _extract_workflow_result_text(payload)
                            if workflow_text:
                                round_state["workflow_text"] = workflow_text
                    if cron_team_round_should_end(
                        round_state,
                        chunk_complete=bool(chunk.is_complete),
                    ):
                        consume_meta["ended_early"] = not chunk.is_complete
                        break
            finally:
                try:
                    await stream_gen.aclose()
                except Exception:
                    pass

            text = _pick_cron_team_result_text(
                leader_text=str(round_state.get("leader_text") or ""),
                workflow_text=str(round_state.get("workflow_text") or ""),
            )
            if _is_cron_team_result_insufficient(text=text):
                return "[cron] 定时任务未产生有效报告", False
            return text, bool(consume_meta["ok"])

        try:
            text, ok = await asyncio.wait_for(
                _consume(),
                timeout=timeout_seconds,
            )
            if consume_meta.get("ended_early"):
                await self._cancel_cron_team_agent_session(
                    envelope=envelope,
                    exec_session_id=exec_session_id,
                )
            return text, ok
        except asyncio.TimeoutError:
            logger.warning(
                "[Cron] team stream timed out after %ss request_id=%s",
                timeout_seconds,
                getattr(envelope, "request_id", ""),
            )
            try:
                await stream_gen.aclose()
            except Exception:
                pass
            await self._cancel_cron_team_agent_session(
                envelope=envelope,
                exec_session_id=exec_session_id,
            )
            timeout_min = max(1, int(timeout_seconds // 60))
            return _resolve_cron_team_timeout_result(
                leader_text=str(round_state.get("leader_text") or ""),
                workflow_text=str(round_state.get("workflow_text") or ""),
                workflow_completed=bool(round_state.get("workflow_completed")),
                timeout_min=timeout_min,
            )
        except Exception:
            logger.warning(
                "[Cron] team stream failed request_id=%s",
                getattr(envelope, "request_id", ""),
                exc_info=True,
            )
            try:
                await stream_gen.aclose()
            except Exception:
                pass
            await self._cancel_cron_team_agent_session(
                envelope=envelope,
                exec_session_id=exec_session_id,
            )
            raise

    async def _on_push(self, job: CronJob, run_id: str) -> None:
        # proactive.tick 的结果在 wake 分支已同步产出：有推荐时由
        # trigger_main_agent → send_push 直接推送内容；无推荐时静默。
        # push 事件不再推 result_text 或"正在执行中"占位，避免 wake 的
        # tick_now 还在跑（LLM 耗时）时误推占位消息。
        if getattr(job, "mode", None) == "proactive.tick":
            return
        state = self._runs.get(run_id)
        if state is None:
            tz = ZoneInfo(job.timezone)
            try:
                push_ts = int(run_id.split(":")[-1])
            except Exception:
                push_ts = int(self._now_fn())
            push_dt = datetime.fromtimestamp(push_ts, tz=tz)
            wake_dt = push_dt - timedelta(seconds=max(0, int(job.wake_offset_seconds or 0)))
            state = CronRunState(
                run_id=run_id,
                job_id=job.id,
                wake_at_iso=wake_dt.isoformat(),
                push_at_iso=push_dt.isoformat(),
                job_name=job.name,
                targets=job.targets,
                session_id=job.session_id,
                chat_type=job.chat_type,
                timezone=job.timezone,
            )
            self._runs[run_id] = state

        if state.pushed_final:
            return

        if state.result_text:
            await self._push_to_targets(job, state, text=state.result_text, is_placeholder=False)
            state.pushed_final = True
            return

        # Not ready: send placeholder
        # 幂等保护：reload 或 push reschedule 可能对同一 run_id 重复排入 push 事件，
        # 若 result_text 仍为空会反复推占位。已推过就不再推。
        if state.placeholder_sent:
            logger.info(
                "[Cron] _on_push skipped placeholder: already sent run_id=%s job=%s",
                run_id, job.id,
            )
            return
        placeholder = f"{job.name} 正在执行中，结果稍后补发（push_at={state.push_at_iso}）"
        await self._push_to_targets(job, state, text=placeholder, is_placeholder=True)
        state.placeholder_sent = True

    async def _on_push_update(self, job: CronJob, run_id: str) -> None:
        state = self._runs.get(run_id)
        if state is None:
            logger.info("[Cron] push_update skipped: no state job=%s run_id=%s", job.id, run_id)
            return
        if state.pushed_final:
            logger.info("[Cron] push_update skipped: already pushed_final job=%s run_id=%s", job.id, run_id)
            return
        if not state.result_text:
            logger.info("[Cron] push_update skipped: empty result_text job=%s run_id=%s", job.id, run_id)
            return
        logger.info(
            "[Cron] push_update start job=%s run_id=%s text_len=%d",
            job.id,
            run_id,
            len(state.result_text or ""),
        )
        await self._push_to_targets(job, state, text=state.result_text, is_placeholder=False)
        state.pushed_final = True
        logger.info("[Cron] push_update done job=%s run_id=%s", job.id, run_id)

    async def _push_to_targets(self, job: CronJob, state: CronRunState, *, text: str, is_placeholder: bool) -> None:
        logger.info(
            "[Cron] push_to_targets job=%s run_id=%s channel=%s is_placeholder=%s text_len=%d status=%s",
            job.id,
            state.run_id,
            (job.targets or "").strip(),
            bool(is_placeholder),
            len(text or ""),
            state.status,
        )
        broadcast_text = _format_cron_broadcast_text(
            job_name=job.name,
            text=text,
            is_placeholder=is_placeholder,
        )
        payload_extra = {
            "content": broadcast_text,
            "cron": {
                "job_id": job.id,
                "job_name": job.name,
                "run_id": state.run_id,
                "push_at": state.push_at_iso,
                "wake_at": state.wake_at_iso,
                "exec_channel_id": state.exec_channel_id,
                "exec_session_id": state.exec_session_id,
                "is_placeholder": bool(is_placeholder),
                "status": state.status,
            },
        }
        channel_id = (job.targets or "").strip()
        if not channel_id:
            # targets 为空：占位/真实结果/push_update 补发均会在此静默丢失。
            # 不 raise 以免中断事件循环，但必须留日志让问题可见（历史 bug：
            # agent 跑完用户却看不到任何消息）。
            logger.warning(
                "[Cron] push skipped: empty targets job=%s run_id=%s is_placeholder=%s status=%s",
                job.id, state.run_id, bool(is_placeholder), state.status,
            )
            return

        # 企业飞书：优先用作业里绑定的 SessionMap session_id（feishu::chat_id::bot_id::...），
        # 避免多群共用 bot 时误用 config 中的 last_*（最近一条消息的会话）。
        # TUI：不绑定 session_id，否则 TUI 重启后新 session_id 与旧不同，消息会被前端过滤。
        # Web：不绑定 session_id——WebChannel.send 对带 payload.cron 的推送会广播给所有 web 客户端，
        # 绑定旧 session_id 反而会让前端 shouldHandleSessionEvent 因 session 不匹配而丢弃。
        # 关闭 tab/换设备后旧会话再无连接，置空 session_id 让消息进当前活跃会话流。
        metadata: dict | None = None
        msg_session_id: str | None = None
        routing_sid = str(getattr(job, "session_id", None) or "").strip()
        if routing_sid and channel_id != "tui" and channel_id != "web":
            msg_session_id = routing_sid
        if channel_id.startswith("feishu_enterprise:") and routing_sid and "::" in routing_sid:
            parts = routing_sid.split("::")
            if len(parts) >= 3 and parts[0] == "feishu":
                chat_part = str(parts[1] or "").strip()
                if chat_part:
                    metadata = {"feishu_chat_id": chat_part}
                    if len(parts) >= 6:
                        open_part = str(parts[3] or "").strip()
                        if open_part:
                            metadata["feishu_open_id"] = open_part
                    msg_session_id = chat_part

        # 钉钉：优先用作业绑定的发起会话（Issue #2449）。
        # Gateway 内部会话 ID（dingtalk_…）不能当 staffId，此时留空走下方 last_* 兜底。
        if channel_id == "dingtalk" and routing_sid and metadata is None:
            bound = resolve_dingtalk_push_metadata(routing_sid)
            if bound is not None:
                metadata = dict(bound)
                sender = str(bound.get("dingtalk_sender_id") or "").strip()
                if is_usable_dingtalk_staff_id(sender):
                    # 保留 msg_session_id 为 job.session_id（可能是内部会话），
                    # 仅在 binding 场景用真实 staff 作为发送目标已写入 metadata。
                    pass

        # 针对 feishu/xiaoyi/whatsapp/dingtalk：从 config.yaml 取最近一次可回发的平台身份，写入 metadata
        # 这样即使 cron 推送没有 session_id，也能让 Channel.send 正常路由到对应会话。
        if metadata is None:
            channels_cfg: dict = {}
            ch_cfg: dict = {}
            try:
                from jiuwenswarm.common.config import get_config_raw

                cfg = get_config_raw() or {}
                channels_cfg = cfg.get("channels") or {}
                ch_cfg = channels_cfg.get(channel_id) or {}
                if channel_id == "feishu" or channel_id.startswith("feishu:"):
                    # V2 多应用：从 apps 列表取对应 app 的 last_*（而非平铺字段）
                    target_app_id = str(getattr(job, "app_id", None) or "").strip()
                    if not target_app_id:
                        if channel_id.startswith("feishu:") and not channel_id.startswith("feishu_enterprise:"):
                            target_app_id = channel_id.split(":", 1)[1].strip()
                    apps = ch_cfg.get("apps") or []
                    if isinstance(apps, list):
                        for app in apps:
                            if not isinstance(app, dict):
                                continue
                            if target_app_id and app.get("app_id") != target_app_id:
                                continue
                            if not target_app_id and not app.get("is_default", False):
                                continue
                            last_chat_id = str(app.get("last_chat_id") or "").strip()
                            last_open_id = str(app.get("last_open_id") or "").strip()
                            if last_chat_id or last_open_id:
                                metadata = {
                                    "feishu_chat_id": last_chat_id,
                                    "feishu_open_id": last_open_id,
                                }
                            break
                    # 兜底：如果 apps 列表为空或无匹配，回退到旧平铺字段
                    if metadata is None:
                        last_chat_id = str(ch_cfg.get("last_chat_id") or "").strip()
                        last_open_id = str(ch_cfg.get("last_open_id") or "").strip()
                        if last_chat_id or last_open_id:
                            metadata = {
                                "feishu_chat_id": last_chat_id,
                                "feishu_open_id": last_open_id,
                            }
                elif channel_id.startswith("feishu_enterprise:"):
                    app_id = channel_id.split(":", 1)[1].strip()
                    enterprise_cfg = channels_cfg.get("feishu_enterprise") or {}
                    if isinstance(enterprise_cfg, dict) and app_id:
                        for _, bot_cfg in enterprise_cfg.items():
                            if not isinstance(bot_cfg, dict):
                                continue
                            bot_app_id = str(bot_cfg.get("app_id") or "").strip()
                            if bot_app_id != app_id:
                                continue
                            last_chat_id = str(bot_cfg.get("last_chat_id") or "").strip()
                            last_open_id = str(bot_cfg.get("last_open_id") or "").strip()
                            if last_chat_id or last_open_id:
                                metadata = {
                                    "feishu_chat_id": last_chat_id,
                                    "feishu_open_id": last_open_id,
                                }
                            break
                elif channel_id == "xiaoyi":
                    last_session_id = str(ch_cfg.get("last_session_id") or "").strip()
                    last_task_id = str(ch_cfg.get("last_task_id") or "").strip()
                    if last_session_id or last_task_id:
                        metadata = {
                            "xiaoyi_session_id": last_session_id,
                            "xiaoyi_task_id": last_task_id,
                        }
                elif channel_id == "whatsapp":
                    last_jid = str(ch_cfg.get("last_jid") or "").strip()
                    if last_jid:
                        metadata = {
                            "whatsapp_jid": last_jid,
                        }
                elif channel_id == "wecom":
                    last_chat_id = str(ch_cfg.get("last_chat_id") or "").strip()
                    last_user_id = str(ch_cfg.get("last_user_id") or "").strip()
                    if last_chat_id or last_user_id:
                        metadata = {
                            "wecom_chat_id": last_chat_id,
                            "wecom_user_id": last_user_id,
                        }
                elif channel_id == "wechat":
                    last_user_id = str(ch_cfg.get("last_user_id") or "").strip()
                    last_context_token = str(ch_cfg.get("last_context_token") or "").strip()
                    if last_user_id:
                        metadata = {
                            "wechat_user_id": last_user_id,
                            "reply_to_user_id": last_user_id,
                        }
                        if last_context_token:
                            metadata["wechat_context_token"] = last_context_token
                            metadata["context_token"] = last_context_token
                elif channel_id == "dingtalk":
                    last_sender_id = str(ch_cfg.get("last_sender_id") or "").strip()
                    last_conversation_id = str(ch_cfg.get("last_conversation_id") or "").strip()
                    last_conversation_type = str(ch_cfg.get("last_conversation_type") or "").strip()
                    # 钉钉 send() 依赖 metadata 决定单聊/群聊（conversation_type + conversation_id）。
                    # sender_id 作为单聊兜底接收者；群聊以 conversation_id 为主。
                    if last_sender_id or last_conversation_id:
                        metadata = {
                            "dingtalk_sender_id": last_sender_id,
                            "dingtalk_chat_id": last_conversation_id,
                            "conversation_id": last_conversation_id,
                            "conversation_type": last_conversation_type or "1",
                        }
            except Exception:
                metadata = None

        if metadata is None:
            metadata = {}
        if channel_id == "dingtalk":
            # 仅用可用的钉钉 staffId / delivery binding 补路由；禁止把 dingtalk_… 内部会话当 staffId。
            if routing_sid and not str(metadata.get("dingtalk_sender_id") or "").strip():
                bound = resolve_dingtalk_push_metadata(routing_sid)
                if bound and is_usable_dingtalk_staff_id(bound.get("dingtalk_sender_id")):
                    metadata["dingtalk_sender_id"] = bound["dingtalk_sender_id"]
                    if not str(metadata.get("conversation_id") or "").strip():
                        metadata["conversation_id"] = bound.get("conversation_id") or ""
                        metadata["dingtalk_chat_id"] = bound.get("dingtalk_chat_id") or ""
                    if not str(metadata.get("conversation_type") or "").strip():
                        metadata["conversation_type"] = bound.get("conversation_type") or "1"
            # 若 metadata 里误塞了内部会话 ID，清掉以免 batchSend 报 staffId.notExisted。
            if not is_usable_dingtalk_staff_id(metadata.get("dingtalk_sender_id")):
                metadata.pop("dingtalk_sender_id", None)
            if not str(metadata.get("conversation_type") or "").strip():
                metadata["conversation_type"] = "1"

        # 获取 group_digital_avatar 和 my_user_id 配置
        _group_digital_avatar = False
        _my_user_id = ""
        if channel_id == "wecom":
            _group_digital_avatar = bool(ch_cfg.get("group_digital_avatar") or False)
            _my_user_id = str(ch_cfg.get("my_user_id") or "").strip()
        elif channel_id == "feishu":
            _group_digital_avatar = bool(ch_cfg.get("group_digital_avatar") or False)
            _my_user_id = str(ch_cfg.get("my_user_id") or "").strip()
        elif channel_id.startswith("feishu_enterprise:"):
            app_id = channel_id.split(":", 1)[1].strip()
            enterprise_cfg = channels_cfg.get("feishu_enterprise") or {}
            if isinstance(enterprise_cfg, dict) and app_id:
                for _, bot_cfg in enterprise_cfg.items():
                    if not isinstance(bot_cfg, dict):
                        continue
                    bot_app_id = str(bot_cfg.get("app_id") or "").strip()
                    if bot_app_id != app_id:
                        continue
                    _group_digital_avatar = bool(bot_cfg.get("group_digital_avatar") or False)
                    _my_user_id = str(bot_cfg.get("my_user_id") or "").strip()
                    break

        if _group_digital_avatar and _my_user_id:
            # 判断定时任务是在群聊还是私聊中创建的
            # 优先使用 job.chat_type（创建时保存的），如果没有则尝试从 session_id 推断
            _is_cron_from_group = job.chat_type == "group"

            # 只有同时满足以下条件才启用 IMOutboundPipeline 路由决策：
            # 1. 开启了 group_digital_avatar
            # 2. 配置了 my_user_id
            # 3. 定时任务是在群聊中创建的（私聊创建的任务直接推送，不走路由决策）
            if _is_cron_from_group:
                # 不在此处硬编码 reply_scope，交由 IMOutboundPipeline 根据内容决定 DM 还是群聊。
                # 只需补充 outbound pipeline 所需的 metadata 前置条件：
                #   - chat_type=group（pipeline 仅对群聊做路由决策）
                #   - reply_candidate_feishu_open_id / reply_candidate_reason（pipeline 需要知道目标用户）
                metadata["chat_type"] = "group"
                if channel_id == "wecom":
                    metadata["reply_wecom_user_id"] = _my_user_id
                elif channel_id == "feishu" or channel_id.startswith("feishu_enterprise:"):
                    metadata["reply_candidate_feishu_open_id"] = _my_user_id
                metadata["reply_candidate_reason"] = "cron_target_user"
                metadata["reply_target_name"] = _my_user_id
                # 标记为定时任务消息，避免在群聊中重复发送确认消息
                metadata["is_cron_job"] = True
                logger.info(
                    "[Cron] 定时任务创建于群聊，启用 IMOutboundPipeline 路由决策: my_user_id=%s channel=%s job_id=%s",
                    _my_user_id, channel_id, job.id,
                )
            else:
                logger.info(
                    "[Cron] 定时任务创建于私聊，跳过 IMOutboundPipeline 路由决策: job.chat_type=%s channel=%s job_id=%s",
                    job.chat_type, channel_id, job.id,
                )

        _msg_app_id = str(getattr(job, "app_id", None) or "").strip() or None
        msg = Message(
            id=f"cron-push-{state.run_id}-{channel_id}",
            type="event",
            channel_id=channel_id,
            session_id=msg_session_id,
            params={},
            timestamp=self._now_fn(),
            ok=True,
            payload=payload_extra,
            event_type=EventType.CHAT_FINAL,
            metadata=metadata,
            group_digital_avatar=_group_digital_avatar,
            app_id=_msg_app_id,
        )
        await self._message_handler.publish_robot_messages(msg)
