# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process-level owner for the Heartbeat Rail domain."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import get_heartbeat_jobs_path

from .controller import HeartbeatController
from .execution import (
    DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    DEFAULT_USER_PREEMPTION_TIMEOUT_SECONDS,
    HeartbeatExecutionService,
    SessionRunAdmission,
)
from .scheduler import HeartbeatSchedulerService
from .store import HeartbeatJobStore

logger = logging.getLogger(__name__)


def _load_limits() -> dict[str, Any]:
    config = get_config()
    heartbeat = config.get("heartbeat") if isinstance(config, dict) else None
    jobs = heartbeat.get("jobs") if isinstance(heartbeat, dict) else None
    limits: dict[str, Any] = {}
    if isinstance(jobs, dict):
        for key in (
            "min_interval_seconds",
            "max_active_jobs_per_session",
            "max_active_jobs_global",
            "default_max_runs",
            "default_concurrency_policy",
            "default_session_deleted_policy",
            "execution_timeout_seconds",
            "user_preemption_timeout_seconds",
        ):
            if key in jobs:
                limits[key] = jobs[key]
    env_limits = {
        "min_interval_seconds": os.getenv("HEARTBEAT_JOBS_MIN_INTERVAL"),
        "max_active_jobs_per_session": os.getenv(
            "HEARTBEAT_JOBS_MAX_ACTIVE_PER_SESSION"
        ),
        "max_active_jobs_global": os.getenv("HEARTBEAT_JOBS_MAX_ACTIVE_GLOBAL"),
        "default_max_runs": os.getenv("HEARTBEAT_JOBS_DEFAULT_MAX_RUNS"),
        "default_concurrency_policy": os.getenv(
            "HEARTBEAT_JOBS_DEFAULT_CONCURRENCY_POLICY"
        ),
        "default_session_deleted_policy": os.getenv(
            "HEARTBEAT_JOBS_DEFAULT_SESSION_DELETED_POLICY"
        ),
        "execution_timeout_seconds": os.getenv(
            "HEARTBEAT_JOBS_EXECUTION_TIMEOUT"
        ),
        "user_preemption_timeout_seconds": os.getenv(
            "HEARTBEAT_JOBS_USER_PREEMPTION_TIMEOUT"
        ),
    }
    for key, value in env_limits.items():
        if value not in (None, ""):
            limits[key] = value
    return limits


class HeartbeatRuntimeUnavailableError(RuntimeError):
    """Heartbeat ownership exists, but its local runtime is not ready yet."""


class HeartbeatRailRuntime:
    """The sole Store/Controller/Scheduler/Execution owner in AgentServer."""

    protocol_version = 1
    owner = "agentserver"

    def __init__(self, server: Any) -> None:
        self._server = server
        limits = _load_limits()
        self.admission = SessionRunAdmission(
            user_preemption_timeout_seconds=float(
                limits.get(
                    "user_preemption_timeout_seconds",
                    DEFAULT_USER_PREEMPTION_TIMEOUT_SECONDS,
                )
            )
        )
        self.execution = HeartbeatExecutionService(
            server,
            self.admission,
            execution_timeout_seconds=float(
                limits.get(
                    "execution_timeout_seconds",
                    DEFAULT_EXECUTION_TIMEOUT_SECONDS,
                )
            ),
        )
        self.store = HeartbeatJobStore(path=get_heartbeat_jobs_path())
        self.scheduler = HeartbeatSchedulerService(
            store=self.store,
            execution_service=self.execution,
        )
        self.execution.set_scheduler(self.scheduler)
        self.execution.set_completion_hook(self._release_if_no_active_jobs)
        self.controller = HeartbeatController(
            store=self.store,
            scheduler=self.scheduler,
            limits=limits,
        )
        self._available = False
        self._start_lock = asyncio.Lock()
        self._pinned_agents: dict[str, Any] = {}
        self._deleting_sessions: set[str] = set()

    @property
    def is_available(self) -> bool:
        return self._available

    async def should_retain_session(self, session_id: str) -> bool:
        """Return whether a live Heartbeat still owns this Session runtime."""
        session_id = str(session_id or "").strip()
        if not session_id:
            return False
        if session_id in self._deleting_sessions:
            return False
        if session_id in self.execution.active_session_ids():
            return True
        return await self.store.count_active_jobs_for_session(session_id) > 0

    async def start(self) -> None:
        async with self._start_lock:
            if self._available:
                return
            await self.scheduler.start()
            self._available = True
            logger.info("[HeartbeatRailRuntime] AgentServer scheduler started")

    async def stop(self) -> None:
        self._available = False
        await self.scheduler.stop()
        await self.execution.stop()
        for agent in list(self._pinned_agents.values()):
            self._server.get_agent_manager().unpin_agent(agent)
        self._pinned_agents.clear()
        logger.info("[HeartbeatRailRuntime] stopped")

    def retain_agent(self, session_id: str, agent: Any) -> None:
        """Keep the single-agent facade alive while its Heartbeat exists."""
        session_id = str(session_id or "").strip()
        if not session_id:
            return
        previous = self._pinned_agents.get(session_id)
        if previous is agent:
            return
        manager = self._server.get_agent_manager()
        # Transfer ownership without an unpinned window: the current facade is
        # protected before the obsolete mode/project facade is released.
        manager.pin_agent(agent)
        self._pinned_agents[session_id] = agent
        if previous is not None:
            manager.unpin_agent(previous)

    def _retain_existing_agent(self, channel_id: str, session_id: str) -> None:
        manager = self._server.get_agent_manager()
        agent = manager.get_agent_for_session_nowait(
            channel_id=channel_id,
            session_id=session_id,
        )
        if agent is None:
            agent = manager.get_agent_nowait(channel_id=channel_id)
        if agent is not None:
            self.retain_agent(session_id, agent)

    def release_agent(self, session_id: str) -> None:
        agent = self._pinned_agents.pop(str(session_id or "").strip(), None)
        if agent is not None:
            self._server.get_agent_manager().unpin_agent(agent)

    async def _release_if_no_active_jobs(self, session_id: str) -> None:
        if await self.store.count_active_jobs_for_session(session_id) == 0:
            self.release_agent(session_id)

    async def begin_session_delete(self, session_id: str) -> None:
        """Quiesce one Session without committing job deletion policies yet."""
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        if session_id in self._deleting_sessions:
            raise RuntimeError("heartbeat session deletion is already in progress")
        self._deleting_sessions.add(session_id)
        self.scheduler.suspend_session(session_id)
        try:
            active_run_id = await self.admission.block_heartbeats(session_id)
            if active_run_id:
                cancelled = await self.execution.cancel(active_run_id)
                if not cancelled and self.execution.has_active_run(active_run_id):
                    raise RuntimeError(
                        "failed to cancel active heartbeat before session deletion"
                    )
            self.release_agent(session_id)
        except BaseException:
            await self.abort_session_delete(session_id)
            raise

    async def abort_session_delete(
        self, session_id: str, *, channel_id: str = ""
    ) -> None:
        """Roll back a prepared product deletion and allow future dispatch."""
        session_id = str(session_id or "").strip()
        self._deleting_sessions.discard(session_id)
        self.scheduler.resume_session(session_id)
        await self.admission.unblock_heartbeats(session_id)
        if session_id and await self.store.count_active_jobs_for_session(session_id):
            self._retain_existing_agent(channel_id, session_id)

    async def commit_session_delete(self, session_id: str) -> None:
        """Apply configured job policies after the product Session is gone."""
        session_id = str(session_id or "").strip()
        try:
            await self.scheduler.on_session_deleted(session_id)
        finally:
            self.release_agent(session_id)
            self._deleting_sessions.discard(session_id)
            self.scheduler.resume_session(session_id)
            await self.admission.unblock_heartbeats(session_id)

    async def _sync_session_agent(
        self,
        channel_id: str,
        session_id: str,
    ) -> None:
        if await self.store.count_active_jobs_for_session(session_id) == 0:
            self.release_agent(session_id)
        else:
            self._retain_existing_agent(channel_id, session_id)

    async def _check_job_identity(self, job_id: str, user_id: str) -> None:
        if not job_id or not user_id:
            return
        job = await self.store.get_job(job_id)
        if job is None:
            return
        owner_user_id = str((job.metadata or {}).get("user_id") or "").strip()
        # Historical jobs without user_id remain session-scoped for compatibility.
        if owner_user_id and owner_user_id != user_id:
            raise PermissionError("heartbeat job belongs to another user")

    @staticmethod
    def _validated_session_user_id(session_id: str, user_id: str) -> str:
        """Resolve legacy identity and reject a forged Session binding."""
        session_id = str(session_id or "").strip()
        caller_user_id = str(user_id or "").strip()
        if not session_id:
            return caller_user_id

        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        metadata = get_session_metadata(
            session_id,
            cache_bust=True,
            enable_writeback=False,
        )
        owner_user_id = str((metadata or {}).get("user_id") or "").strip()
        if owner_user_id and caller_user_id and owner_user_id != caller_user_id:
            raise PermissionError("heartbeat session belongs to another user")
        # Old clients and Agent tools may omit user_id. Once the authoritative
        # Session has an owner, use it instead of persisting another empty job
        # identity. Historical Sessions with no owner remain Session-scoped.
        return caller_user_id or owner_user_id

    async def handle_operation(
        self,
        action: str,
        data: dict[str, Any],
        *,
        channel_id: str,
        session_id: str,
        user_id: str = "",
        source: str,
    ) -> dict[str, Any]:
        """Execute one public/tool operation against the local controller."""
        if not self._available:
            raise HeartbeatRuntimeUnavailableError(
                "heartbeat scheduler is not ready"
            )
        op = str(action or "").strip()
        payload = dict(data or {})
        job_id = str(payload.get("job_id") or payload.get("id") or "").strip()
        effective_user_id = self._validated_session_user_id(session_id, user_id)
        if op not in {"list", "meta", "create"}:
            await self._check_job_identity(job_id, effective_user_id)

        if op == "list":
            return await self.controller.list_jobs(
                payload,
                access_session_id=session_id,
                allow_all_visible=False,
            )
        if op == "meta":
            return self.controller.get_meta()
        if op == "get":
            job = await self.controller.get_job(
                job_id, access_session_id=session_id
            )
            if job is None:
                raise KeyError("job not found")
            return {"job": job}
        if op == "create":
            create = {
                **payload,
                "channel_id": channel_id,
                "session_id": session_id,
                "source": source,
            }
            job = await self.controller.create_job(
                create,
                identity_metadata={
                    "user_id": effective_user_id,
                    "channel_id": channel_id,
                },
            )
            self._retain_existing_agent(channel_id, session_id)
            return {"job": job}
        if op == "update":
            job = await self.controller.update_job(
                job_id,
                payload.get("patch") or {},
                access_session_id=session_id,
            )
            await self._sync_session_agent(channel_id, session_id)
            return {"job": job}
        if op == "delete":
            result = await self.controller.delete_job(
                job_id, access_session_id=session_id
            )
            await self._sync_session_agent(channel_id, session_id)
            return result
        if op == "toggle":
            job = await self.controller.toggle_job(
                job_id,
                payload.get("enabled"),
                access_session_id=session_id,
            )
            await self._sync_session_agent(channel_id, session_id)
            return {"job": job}
        if op == "preview":
            return await self.controller.preview_job(
                job_id,
                count=payload.get("count", 5),
                access_session_id=session_id,
            )
        if op == "run_now":
            return await self.controller.run_now(
                job_id,
                reschedule=payload.get("reschedule", False),
                access_session_id=session_id,
            )
        if op == "cancel":
            return await self.controller.cancel_run(
                job_id,
                pause_schedule=payload.get("pause_schedule", False),
                access_session_id=session_id,
            )
        raise ValueError(f"unsupported heartbeat action: {op}")

    async def on_session_deleted(self, session_id: str) -> None:
        """Compatibility hook for already-deleted Sessions."""
        await self.commit_session_delete(session_id)


__all__ = ["HeartbeatRailRuntime", "HeartbeatRuntimeUnavailableError"]
