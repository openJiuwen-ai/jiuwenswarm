"""Process-local pool of session-bound, ready-to-run DeepAgent instances."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.common.work_mode import (
    DEFAULT_PROJECT_ID_CODE,
    DEFAULT_PROJECT_ID_WORK,
)
from jiuwenswarm.server.runtime.session import project_store

if TYPE_CHECKING:
    from jiuwenswarm.server.runtime.agent_manager import AgentManager
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm


logger = logging.getLogger(__name__)

# Background prewarming is off by default and is only activated through the
# environment. Once off, sessions are allocated an id immediately and initialize
# lazily on their first request.
_PREWARM_ENABLED_ENV_KEY = "JIUWENSWARM_AGENT_PREWARM"
_PREWARM_ON_VALUES = frozenset({"1", "true", "yes", "on"})
_PREWARM_OFF_VALUES = frozenset({"0", "false", "no", "off"})


def _prewarm_enabled_by_env() -> bool:
    """Return whether background session prewarming is switched on.

    Returns:
        True only when the environment explicitly opts in; False when the
        switch is unset or carries an unrecognized value.
    """
    raw = str(os.environ.get(_PREWARM_ENABLED_ENV_KEY, "") or "").strip().lower()
    if raw in _PREWARM_ON_VALUES:
        return True
    if raw and raw not in _PREWARM_OFF_VALUES:
        logger.warning(
            "Ignoring unrecognized %s value %r; keeping prewarming disabled.",
            _PREWARM_ENABLED_ENV_KEY,
            raw,
        )
    return False


def _zero_stats() -> dict[str, int]:
    """Return the pool statistics reported when nothing is being warmed.

    Returns:
        A statistics mapping with every counter set to zero.
    """
    return {"target": 0, "ready": 0, "warming": 0, "failed": 0, "stale": 0}


def _normalize_project_dir(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return os.path.normcase(os.path.abspath(os.path.expanduser(raw))).casefold()


@dataclass(frozen=True, slots=True)
class WarmKey:
    channel_id: str
    project_id: str
    project_dir: str
    work_mode: str
    is_swarm: bool = False

    @property
    def agent_mode(self) -> str:
        return "code" if self.work_mode == "code" else "agent"

    @property
    def agent_sub_mode(self) -> str | None:
        return "normal" if self.work_mode == "code" else None


@dataclass(frozen=True, slots=True)
class WarmRevision:
    boot_id: str
    config_fingerprint: str
    sequence: int


@dataclass(slots=True)
class WarmSlot:
    key: WarmKey
    session_id: str
    revision: WarmRevision
    agent: "JiuWenSwarm"
    ready_at: float


@dataclass(frozen=True, slots=True)
class WarmClaim:
    session_id: str
    prewarm_hit: bool
    prewarm_status: str


class AgentWarmPool:
    """Own a bounded set of unclaimed, initialized Agent sessions."""

    EXCLUDED_CHANNELS = frozenset({"acp", "a2a"})

    def __init__(
        self,
        manager: "AgentManager",
        *,
        max_concurrency: int = 1,
        max_ready_slots: int = 1,
        max_foreground_concurrency: int = 8,
        background_cooldown_seconds: float = 0.25,
        enabled: bool | None = None,
    ) -> None:
        self._manager = manager
        self._enabled = _prewarm_enabled_by_env() if enabled is None else bool(enabled)
        self._boot_id = uuid.uuid4().hex
        self._sequence = 0
        self._revision = WarmRevision(self._boot_id, "", 0)
        self._enabled_channels: set[str] = set()
        self._desired: set[WarmKey] = set()
        self._slots: dict[WarmKey, WarmSlot] = {}
        self._tasks: dict[WarmKey, asyncio.Task[None]] = {}
        self._task_revisions: dict[WarmKey, WarmRevision] = {}
        self._task_session_ids: dict[WarmKey, str] = {}
        self._pending: dict[WarmKey, WarmRevision] = {}
        self._session_tasks: dict[str, asyncio.Task[None]] = {}
        self._promoted_sessions: set[str] = set()
        self._claimed_pins: dict[str, "JiuWenSwarm"] = {}
        self._pin_release_tasks: set[asyncio.Task[None]] = set()
        self._failed: dict[WarmKey, str] = {}
        self._lock = asyncio.Lock()
        # OpenJiuwen registers tools and resources in process-global managers.
        # Never let speculative and foreground DeepAgent construction mutate
        # those registries concurrently.
        self._initialization_lock = asyncio.Lock()
        self._max_concurrency = max(1, int(max_concurrency))
        self._max_ready_slots = max(1, int(max_ready_slots))
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._foreground_semaphore = asyncio.Semaphore(
            max(1, int(max_foreground_concurrency))
        )
        self._foreground_count = 0
        self._foreground_idle = asyncio.Event()
        self._foreground_idle.set()
        self._background_cooldown_seconds = max(0.0, float(background_cooldown_seconds))
        self._background_pump_task: asyncio.Task[None] | None = None
        self._closed = False
        self._marker_dir = get_agent_sessions_dir() / ".prewarm"
        self._cleanup_stale_markers()

    @property
    def boot_id(self) -> str:
        return self._boot_id

    @staticmethod
    def make_key(
        *,
        channel_id: str,
        project_id: str,
        project_dir: str | None,
        work_mode: str,
        is_swarm: bool = False,
    ) -> WarmKey:
        return WarmKey(
            channel_id=str(channel_id or "default").strip() or "default",
            project_id=str(project_id or "").strip(),
            project_dir=_normalize_project_dir(project_dir),
            work_mode="code" if str(work_mode).strip().lower() == "code" else "work",
            is_swarm=bool(is_swarm),
        )

    @staticmethod
    def config_fingerprint(config: Any, env: Any = None) -> str:
        payload = json.dumps(
            {"config": config, "env": env if isinstance(env, dict) else {}},
            sort_keys=True,
            ensure_ascii=False,
            default=repr,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _next_revision(self, config: Any, env: Any = None) -> WarmRevision:
        self._sequence += 1
        return WarmRevision(
            self._boot_id,
            self.config_fingerprint(config, env),
            self._sequence,
        )

    @staticmethod
    def _new_session_id(channel_id: str) -> str:
        prefix = str(channel_id or "default").strip() or "default"
        return f"{prefix}_{int(time.time() * 1000):x}_{secrets.token_hex(6)}"

    def _marker_path(self, session_id: str) -> Path:
        return self._marker_dir / f"{session_id}.json"

    def _write_marker(self, session_id: str, key: WarmKey) -> None:
        self._marker_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "boot_id": self._boot_id,
            "session_id": session_id,
            "key": {
                "channel_id": key.channel_id,
                "project_id": key.project_id,
                "project_dir": key.project_dir,
                "work_mode": key.work_mode,
                "is_swarm": key.is_swarm,
            },
        }
        self._marker_path(session_id).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def clear_marker(self, session_id: str) -> None:
        try:
            self._marker_path(session_id).unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove prewarm marker: session_id=%s", session_id)

    def _cleanup_stale_markers(self) -> None:
        if not self._marker_dir.exists():
            return
        for marker in self._marker_dir.glob("*.json"):
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
                if payload.get("boot_id") != self._boot_id:
                    session_id = str(payload.get("session_id") or "").strip()
                    session_dir = (get_agent_sessions_dir() / session_id).resolve()
                    sessions_root = get_agent_sessions_dir().resolve()
                    has_valid_path = (
                        bool(session_id) and session_dir.parent == sessions_root
                    )
                    is_uninitialized = (
                        session_dir.is_dir()
                        and not (session_dir / "metadata.json").exists()
                    )
                    if has_valid_path and is_uninitialized:
                        shutil.rmtree(session_dir)
                    marker.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError):
                logger.warning("Invalid prewarm marker left in place: %s", marker)

    def _desired_keys(self, enabled_channels: set[str] | None = None) -> set[WarmKey]:
        channels = (
            self._enabled_channels if enabled_channels is None else enabled_channels
        )
        projects = list(project_store.list_projects(cache_bust=True))
        records = [
            (p.project_id, p.project_dir, p.work_mode) for p in projects if not p.hidden
        ]
        records.extend(
            [
                (DEFAULT_PROJECT_ID_WORK, "", "work"),
                (DEFAULT_PROJECT_ID_CODE, "", "code"),
            ]
        )
        return {
            self.make_key(
                channel_id=channel,
                project_id=project_id,
                project_dir=project_dir,
                work_mode=work_mode,
            )
            for channel in channels
            for project_id, project_dir, work_mode in records
        }

    @staticmethod
    def _key_priority(key: WarmKey) -> tuple[int, int, int, str, str]:
        """Prefer the normal Web work slot for the initial global READY slot."""
        return (
            0 if key.channel_id == "web" else 1,
            0 if key.work_mode == "work" else 1,
            0 if key.project_id == DEFAULT_PROJECT_ID_WORK else 1,
            key.channel_id,
            key.project_id,
        )

    async def sync(
        self,
        enabled_channels: list[str],
        *,
        config: Any,
        env: Any = None,
    ) -> dict[str, int]:
        if not self._enabled:
            return _zero_stats()
        channels: set[str] = set()
        for channel in enabled_channels:
            normalized_channel = str(channel).strip().lower()
            if normalized_channel and normalized_channel not in self.EXCLUDED_CHANNELS:
                channels.add(normalized_channel)
        revision = self._next_revision(config, env)
        # Project discovery can touch persistent metadata. Keep it outside both
        # the AgentServer event loop and the pool lock, then reconcile against
        # one immutable target snapshot.
        desired = await asyncio.to_thread(self._desired_keys, channels)
        async with self._lock:
            if self._closed:
                return _zero_stats()
            if revision.sequence < self._revision.sequence:
                warming_tasks = set(self._tasks.values()) | set(
                    self._session_tasks.values()
                )
                return {
                    "target": len(self._desired),
                    "ready": len(self._slots),
                    "warming": len(warming_tasks) + len(self._pending),
                    "failed": len(self._failed),
                    "stale": 0,
                }
            config_changed = (
                self._revision.config_fingerprint != revision.config_fingerprint
            )
            self._enabled_channels = channels
            self._desired = desired
            self._revision = revision
            if config_changed:
                self._failed.clear()
            else:
                self._failed = {
                    key: error for key, error in self._failed.items() if key in desired
                }
            stale_slots: list[WarmSlot] = []
            for key, slot in list(self._slots.items()):
                fingerprint_changed = (
                    slot.revision.config_fingerprint != revision.config_fingerprint
                )
                if key not in desired or fingerprint_changed:
                    stale_slots.append(slot)
            for slot in stale_slots:
                self._slots.pop(slot.key, None)
            for key, task in list(self._tasks.items()):
                task_revision = self._task_revisions.get(key)
                if (
                    key not in desired
                    or task_revision is None
                    or task_revision.config_fingerprint != revision.config_fingerprint
                ):
                    if not task.done():
                        task.cancel()
                    self._tasks.pop(key, None)
                    self._task_revisions.pop(key, None)
                    self._task_session_ids.pop(key, None)
            current_pending: dict[WarmKey, WarmRevision] = {}
            for key, task_revision in self._pending.items():
                fingerprint_matches = (
                    task_revision.config_fingerprint == revision.config_fingerprint
                )
                if key in desired and fingerprint_matches:
                    current_pending[key] = task_revision
            self._pending = current_pending
            for key in sorted(desired, key=self._key_priority):
                slot = self._slots.get(key)
                if slot is None and key not in self._tasks and key not in self._pending:
                    self._enqueue_prepare_locked(key, revision)
            self._pump_background_locked()
        for slot in stale_slots:
            asyncio.create_task(self._dispose_slot(slot))
        return await self.stats()

    async def refresh(self, *, config: Any, env: Any = None) -> dict[str, int]:
        return await self.sync(
            sorted(self._enabled_channels),
            config=config,
            env=env,
        )

    def _schedule_prepare_locked(
        self,
        key: WarmKey,
        revision: WarmRevision,
        *,
        session_id: str | None = None,
        keep_as_slot: bool = True,
    ) -> tuple[str, asyncio.Task[None]]:
        sid = session_id or self._new_session_id(key.channel_id)
        task = asyncio.create_task(
            self._prepare(key, sid, revision, keep_as_slot=keep_as_slot),
            name=f"agent-prewarm-{sid}",
        )
        if keep_as_slot:
            self._tasks[key] = task
            self._task_revisions[key] = revision
            self._task_session_ids[key] = sid
        self._session_tasks[sid] = task
        return sid, task

    def _enqueue_prepare_locked(
        self,
        key: WarmKey,
        revision: WarmRevision,
        *,
        prioritize: bool = False,
    ) -> None:
        """Queue a background slot without creating an unbounded task backlog."""
        if key in self._slots or key in self._tasks or key in self._pending:
            return
        if prioritize:
            self._pending = {key: revision, **self._pending}
        else:
            self._pending[key] = revision

    def _pump_background_locked(self) -> None:
        """Start only the bounded next batch while no user chat is active."""
        if self._closed or self._foreground_count > 0:
            return
        while (
            self._pending
            and len(self._tasks) < self._max_concurrency
            and len(self._slots) + len(self._tasks) < self._max_ready_slots
        ):
            key = next(iter(self._pending))
            revision = self._pending.pop(key)
            current = self._revision
            if (
                revision.boot_id != current.boot_id
                or revision.config_fingerprint != current.config_fingerprint
                or key not in self._desired
            ):
                continue
            self._schedule_prepare_locked(key, revision)

    def _schedule_background_pump_locked(self) -> None:
        """Leave an event-loop window between expensive background sessions."""
        task = self._background_pump_task
        if self._closed or (task is not None and not task.done()):
            return

        async def _delayed_pump() -> None:
            try:
                await asyncio.sleep(self._background_cooldown_seconds)
                async with self._lock:
                    self._background_pump_task = None
                    self._pump_background_locked()
            except asyncio.CancelledError:
                return

        self._background_pump_task = asyncio.create_task(
            _delayed_pump(), name="agent-prewarm-background-pump"
        )

    async def begin_foreground(self) -> None:
        """Preempt speculative preparation while a real chat is active."""
        cancelled = 0
        async with self._lock:
            self._foreground_count += 1
            self._foreground_idle.clear()
            for task in list(self._tasks.values()):
                if not task.done():
                    task.cancel()
                    cancelled += 1
            logger.info(
                "Agent prewarm background paused: foreground=%s pending=%s cancelled=%s",
                self._foreground_count,
                len(self._pending),
                cancelled,
            )
        if cancelled:
            # Deliver cancellation before the foreground path starts building a
            # second DeepAgent and touching the shared OpenJiuwen registry.
            await asyncio.sleep(0)

    async def end_foreground(self) -> None:
        """Resume lazy background preparation after the final chat completes."""
        async with self._lock:
            self._foreground_count = max(0, self._foreground_count - 1)
            if self._foreground_count == 0:
                self._foreground_idle.set()
                self._schedule_background_pump_locked()
                logger.info(
                    "Agent prewarm background resumed: pending=%s",
                    len(self._pending),
                )

    async def _prepare(
        self,
        key: WarmKey,
        session_id: str,
        revision: WarmRevision,
        *,
        keep_as_slot: bool,
    ) -> None:
        agent: "JiuWenSwarm | None" = None
        pinned = False
        published = False
        cancelled = False
        foreground_registered = False
        if keep_as_slot:
            self._write_marker(session_id, key)
        else:
            await self.begin_foreground()
            foreground_registered = True
        started_at = time.monotonic()
        try:
            semaphore = self._semaphore if keep_as_slot else self._foreground_semaphore
            if keep_as_slot:
                await self._foreground_idle.wait()
            async with semaphore:
                async with self._initialization_lock:
                    if keep_as_slot:
                        await self._foreground_idle.wait()
                    agent = await self._manager.get_agent(
                        channel_id=key.channel_id,
                        mode=key.agent_mode,
                        project_dir=key.project_dir or None,
                        sub_mode=key.agent_sub_mode,
                    )
                    if agent is None:
                        raise RuntimeError("agent creation returned None")
                    # Yield between root creation and session-heavy setup. If a
                    # chat arrived meanwhile, cancellation wins before shared
                    # OpenJiuwen resources are mutated again.
                    await asyncio.sleep(0)
                    if keep_as_slot:
                        await self._foreground_idle.wait()
                    await agent.prepare_session(
                        session_id=session_id,
                        channel_id=key.channel_id,
                        mode=("code.normal" if key.work_mode == "code" else "agent"),
                        project_dir=key.project_dir or None,
                    )
            logger.info(
                "Agent prepare completed: session_id=%s foreground=%s duration_ms=%.1f",
                session_id,
                not keep_as_slot,
                (time.monotonic() - started_at) * 1000,
            )
            if not keep_as_slot:
                return
            async with self._lock:
                promoted = session_id in self._promoted_sessions
                current = self._revision
                revision_changed = (
                    current.boot_id != revision.boot_id
                    or current.config_fingerprint != revision.config_fingerprint
                )
                stale = not promoted and (
                    self._closed or revision_changed or key not in self._desired
                )
                if promoted:
                    self._manager.pin_agent(agent)
                    pinned = True
                    self._claimed_pins[session_id] = agent
                    pin_task = asyncio.create_task(
                        self._release_claim_pin_after(session_id, 300),
                        name=f"agent-prewarm-claim-pin-timeout-{session_id}",
                    )
                    self._pin_release_tasks.add(pin_task)
                    pin_task.add_done_callback(self._pin_release_tasks.discard)
                elif not stale:
                    self._manager.pin_agent(agent)
                    pinned = True
                    self._slots[key] = WarmSlot(
                        key=key,
                        session_id=session_id,
                        revision=revision,
                        agent=agent,
                        ready_at=time.time(),
                    )
                    published = True
                    self._failed.pop(key, None)
            if stale:
                await self._dispose_runtime(
                    agent, key.channel_id, session_id, pinned=True
                )
                pinned = False
        except asyncio.CancelledError:
            cancelled = True
            if agent is not None:
                await self._dispose_runtime(
                    agent, key.channel_id, session_id, pinned=pinned
                )
            raise
        except Exception as exc:
            logger.exception(
                "Agent prewarm failed: key=%s session_id=%s", key, session_id
            )
            if agent is not None:
                await self._dispose_runtime(
                    agent, key.channel_id, session_id, pinned=pinned
                )
            async with self._lock:
                self._failed[key] = str(exc)
        finally:
            promoted = session_id in self._promoted_sessions
            if keep_as_slot and not published and not promoted:
                self.clear_marker(session_id)
            async with self._lock:
                current_task = asyncio.current_task()
                if self._tasks.get(key) is current_task:
                    self._tasks.pop(key, None)
                    self._task_revisions.pop(key, None)
                    self._task_session_ids.pop(key, None)
                if self._session_tasks.get(session_id) is current_task:
                    self._session_tasks.pop(session_id, None)
                self._promoted_sessions.discard(session_id)
                if cancelled and keep_as_slot:
                    fingerprint_matches = (
                        revision.config_fingerprint == self._revision.config_fingerprint
                    )
                    if key in self._desired and fingerprint_matches:
                        self._enqueue_prepare_locked(
                            key, self._revision, prioritize=True
                        )
                if keep_as_slot:
                    self._schedule_background_pump_locked()
            if foreground_registered:
                await self.end_foreground()

    async def claim(self, key: WarmKey) -> WarmClaim:
        if not self._enabled or key.is_swarm:
            return WarmClaim(self._new_session_id(key.channel_id), False, "bypassed")
        async with self._lock:
            if self._closed:
                raise RuntimeError("agent warm pool is closed")
            is_desired = key in self._desired
            slot = self._slots.pop(key, None)
            if slot is not None:
                self._claimed_pins[slot.session_id] = slot.agent
                pin_task = asyncio.create_task(
                    self._release_claim_pin_after(slot.session_id, 300),
                    name=f"agent-prewarm-claim-pin-timeout-{slot.session_id}",
                )
                self._pin_release_tasks.add(pin_task)
                pin_task.add_done_callback(self._pin_release_tasks.discard)
                if key not in self._tasks:
                    self._enqueue_prepare_locked(key, self._revision, prioritize=True)
                return WarmClaim(slot.session_id, True, "ready")
            if key in self._tasks:
                # The speculative instance already owns its final Session ID.
                # Promote that exact task instead of creating a competing
                # foreground DeepAgent against the shared tool registry.
                task = self._tasks.pop(key)
                self._task_revisions.pop(key, None)
                sid = self._task_session_ids.pop(key)
                self._promoted_sessions.add(sid)
                self._session_tasks[sid] = task
                if is_desired:
                    self._enqueue_prepare_locked(key, self._revision, prioritize=True)
            else:
                sid, _ = self._schedule_prepare_locked(
                    key, self._revision, keep_as_slot=False
                )
                if is_desired:
                    self._enqueue_prepare_locked(key, self._revision, prioritize=True)
            return WarmClaim(sid, False, "warming")

    async def wait_for_session(self, session_id: str) -> None:
        async with self._lock:
            task = self._session_tasks.get(str(session_id))
        wait_started = time.monotonic()
        try:
            if task is not None:
                logger.info(
                    "Waiting for foreground session preparation: session_id=%s",
                    session_id,
                )
                await asyncio.shield(task)
                logger.info(
                    "Foreground session preparation ready: session_id=%s wait_ms=%.1f",
                    session_id,
                    (time.monotonic() - wait_started) * 1000,
                )
        finally:
            async with self._lock:
                pinned_agent = self._claimed_pins.pop(str(session_id), None)
            if pinned_agent is not None:
                self._manager.unpin_agent(pinned_agent)

    async def release_claim_pin(self, session_id: str) -> None:
        async with self._lock:
            pinned_agent = self._claimed_pins.pop(str(session_id), None)
        if pinned_agent is not None:
            self._manager.unpin_agent(pinned_agent)

    async def _release_claim_pin_after(
        self, session_id: str, delay_seconds: float
    ) -> None:
        await asyncio.sleep(delay_seconds)
        await self.release_claim_pin(session_id)

    async def _dispose_runtime(
        self,
        agent: "JiuWenSwarm",
        channel_id: str,
        session_id: str,
        *,
        pinned: bool,
    ) -> None:
        try:
            await agent.cleanup_session_runtime(session_id)
        finally:
            if pinned:
                self._manager.unpin_agent(agent)
            self.clear_marker(session_id)

    async def _dispose_slot(self, slot: WarmSlot) -> None:
        await self._dispose_runtime(
            slot.agent, slot.key.channel_id, slot.session_id, pinned=True
        )

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            warming_tasks = set(self._tasks.values()) | set(
                self._session_tasks.values()
            )
            return {
                "target": len(self._desired),
                "ready": len(self._slots),
                "warming": len(warming_tasks) + len(self._pending),
                "failed": len(self._failed),
                "stale": max(0, len(self._slots) - len(self._desired)),
            }

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            self._foreground_idle.set()
            tasks = list(
                {
                    *self._tasks.values(),
                    *self._session_tasks.values(),
                    *self._pin_release_tasks,
                    *(
                        [self._background_pump_task]
                        if self._background_pump_task is not None
                        else []
                    ),
                }
            )
            slots = list(self._slots.values())
            claimed_agents = list(self._claimed_pins.values())
            self._tasks.clear()
            self._task_revisions.clear()
            self._task_session_ids.clear()
            self._pending.clear()
            self._desired.clear()
            self._session_tasks.clear()
            self._promoted_sessions.clear()
            self._slots.clear()
            self._claimed_pins.clear()
            self._pin_release_tasks.clear()
            self._background_pump_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for slot in slots:
            await self._dispose_slot(slot)
        for agent in claimed_agents:
            self._manager.unpin_agent(agent)
