"""Archive operations owned by the AgentServer runtime, never Gateway fallback."""

from __future__ import annotations

import asyncio
import os
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from jiuwenswarm.common.cron_session import cron_session_matches_job
from gateway.common.work_mode import is_default_project_id
from jiuwenswarm.server.runtime.session import lifecycle as lc, project_store
from jiuwenswarm.server.runtime.session.session_info import to_session_info

logger = logging.getLogger(__name__)

# Archive and delete return as soon as the logical result is persisted; the
# physical cleanup below runs in the background.  Its retries are deliberately
# bounded: a Session that cannot be stopped or removed is abandoned with an
# error log instead of retrying forever.
_MAX_CLEANUP_ATTEMPTS = 5
_CLEANUP_BACKOFF_SECONDS = (5.0, 15.0, 60.0, 300.0)
_MAX_PARALLEL_FINALIZERS = 4


def _cleanup_backoff(attempts: int) -> float:
    index = min(max(attempts - 1, 0), len(_CLEANUP_BACKOFF_SECONDS) - 1)
    return _CLEANUP_BACKOFF_SECONDS[index]


def get_agent_sessions_dir():
    return lc.get_agent_sessions_dir()


@dataclass(frozen=True)
class ProjectSessionInventoryItem:
    """One session found while scanning the active and archived roots."""

    session_id: str
    metadata: dict
    active: bool
    logically_archived: bool = False


class SessionArchiveService:
    def __init__(self, runtime):
        self.runtime = runtime
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._stopping: dict[str, asyncio.Task] = {}
        # Background physical cleanup, keyed by session id.  A live task also
        # stops the recovery loop from starting a second attempt on the same
        # directory.
        self._finalizing: dict[str, asyncio.Task] = {}
        self._finalizing_operations: dict[str, str] = {}
        self._finalizer_tasks: set[asyncio.Task] = set()
        self._finalize_semaphore = asyncio.Semaphore(_MAX_PARALLEL_FINALIZERS)
        self._recovery_task: asyncio.Task | None = None
        self._backfill_task: asyncio.Task | None = None
        self._recovery_wake = asyncio.Event()
        self._owner_id = uuid.uuid4().hex

    @asynccontextmanager
    async def lock(self, kind: str, resource_id: str):
        """One execution owner across processes, separate from commit locks."""
        local = self._locks.setdefault((kind, resource_id), asyncio.Lock())
        if local.locked():
            raise lc.LifecycleError(
                "OPERATION_IN_PROGRESS", "resource operation is already running"
            )
        async with local:
            owner_path = (
                lc.resource_path(kind, resource_id).parent.parent
                / "owners"
                / f"{kind}_{resource_id}.json"
            )
            owner = lc.file_lock(owner_path)
            # Acquisition runs in a worker; never block the event loop while
            # another process owns the operation. Release on the same live fd.
            acquire = asyncio.create_task(asyncio.to_thread(owner.__enter__))
            try:
                await asyncio.shield(acquire)
            except asyncio.CancelledError:
                await acquire
                owner.__exit__(None, None, None)
                raise
            try:

                async def renew():
                    while True:
                        await asyncio.sleep(10)
                        try:
                            # Worker threads may hold the same resource lock
                            # for a directory move; a blocked renew must never
                            # stall the event loop or kill the lease task.
                            await asyncio.to_thread(
                                lc.renew_operation, kind, resource_id, self._owner_id
                            )
                        except Exception:
                            logger.debug(
                                "lifecycle lease renewal deferred: %s/%s",
                                kind,
                                resource_id,
                                exc_info=True,
                            )

                lease = asyncio.create_task(renew())
                yield
            finally:
                try:
                    if "lease" in locals():
                        lease.cancel()
                        await asyncio.gather(lease, return_exceptions=True)
                    release = asyncio.create_task(asyncio.to_thread(
                        lc.renew_operation, kind, resource_id, self._owner_id, release=True
                    ))
                    try:
                        await asyncio.shield(release)
                    except asyncio.CancelledError:
                        # Keep ownership until the worker's lease write exits.
                        await release
                        raise
                finally:
                    owner.__exit__(None, None, None)

    def start_recovery(self):
        if self._recovery_task is None:
            self._recovery_task = asyncio.create_task(
                self._recover(), name="session-lifecycle-recovery"
            )
        if self._backfill_task is None:
            self._backfill_task = asyncio.create_task(
                asyncio.to_thread(self._backfill_archive_times),
                name="session-archive-time-backfill",
            )

    async def close(self):
        tasks = [
            task
            for task in (self._recovery_task, self._backfill_task)
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._recovery_task = None
        self._backfill_task = None
        tasks = list(self._stopping.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._stopping.clear()
        # A cancelled finalizer leaves its operation running/retryable, so the
        # next process can pick it up through recovery.
        tasks = list(self._finalizer_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._finalizing.clear()
        self._finalizing_operations.clear()
        self._finalizer_tasks.clear()

    async def _recover(self):
        failures: dict[str, tuple[int, float]] = {}
        # Steady state only stats the files: unchanged lifecycle files reuse
        # the previous parse instead of re-reading JSON every pass.
        parsed: dict[str, tuple[tuple[int, int], dict]] = {}
        while True:
            directory = lc.resource_path("session", "_scan").parent
            seen: set[str] = set()
            for path in directory.glob("session_*.json") if directory.exists() else ():
                operation = {}
                try:
                    seen.add(path.name)
                    try:
                        stat = path.stat()
                        stamp = (stat.st_mtime_ns, stat.st_size)
                    except OSError:
                        continue
                    cached = parsed.get(path.name)
                    if cached is not None and cached[0] == stamp:
                        operation = cached[1]
                    else:
                        operation = lc.read_json(path).get("operation") or {}
                        parsed[path.name] = (stamp, operation)
                    if not operation or operation.get("status") in {
                        "completed",
                        "abandoned",
                    }:
                        continue
                    if not operation.get("retryable", True):
                        continue
                    sid = str(operation.get("resource_id") or "")
                    if not sid or sid in self._finalizing:
                        # A live finalizer already owns this session.  Starting
                        # a second one could move or delete the directory twice.
                        continue
                    if time.time() < float(operation.get("cleanup_next_at") or 0):
                        continue
                    attempts, next_try = failures.get(sid, (0, 0))
                    if time.monotonic() < next_try:
                        continue
                    kind = operation.get("kind", "")
                    if kind in {"archive", "delete"}:
                        # Accepted synchronously; finish the physical part.
                        self._spawn_finalizer(
                            sid,
                            kind,
                            str(operation.get("channel_id") or ""),
                            str(operation.get("project_id") or ""),
                            operation.get("stop_generation"),
                        )
                    else:
                        await self.session(sid, kind, "")
                    failures.pop(sid, None)
                except Exception:
                    key = operation.get("resource_id", path.name)
                    attempts = failures.get(key, (0, 0))[0] + 1
                    failures[key] = (
                        attempts,
                        time.monotonic() + min(60, 2 ** min(attempts, 6)),
                    )
                    logger.debug("lifecycle recovery deferred: %s", key, exc_info=True)
            for name in list(parsed):
                if name not in seen:
                    del parsed[name]
            # Idle poll is 10s, but a pending per-resource backoff must not
            # wait it out: wake at the earliest scheduled retry, or right away
            # when a failure path pokes the loop.
            delay = 10.0
            now = time.monotonic()
            for _, next_try in failures.values():
                if next_try > now:
                    delay = min(delay, next_try - now)
            try:
                await asyncio.wait_for(
                    self._recovery_wake.wait(), timeout=max(0.05, delay)
                )
            except asyncio.TimeoutError:
                pass
            else:
                self._recovery_wake.clear()

    def _poke_recovery(self) -> None:
        """Request a prompt recovery pass instead of waiting out the idle poll.

        Deferred by a short grace period: at failure/cancel time the previous
        execution is usually still unwinding (lease not yet released,
        execution locks still held), and an immediate pass would bounce off
        it — or silently skip — and then sleep the full idle interval.
        """
        asyncio.get_running_loop().call_later(0.5, self._recovery_wake.set)

    async def stop(self, session_id: str, channel_id: str) -> None:
        task = self._stopping.get(session_id)
        if task is None:
            task = asyncio.create_task(
                self.runtime.stop_session_for_archive(
                    session_id=session_id, channel_id=channel_id
                )
            )
            self._stopping[session_id] = task
        done, _ = await asyncio.wait({task}, timeout=10)
        if not done:
            raise lc.LifecycleError(
                "STOP_TIMEOUT", "session is still stopping; writes remain isolated"
            )
        self._stopping.pop(session_id, None)
        try:
            task.result()
        except lc.LifecycleError:
            raise
        except Exception as exc:
            raise lc.LifecycleError("STOP_SUBMIT_FAILED", str(exc)) from exc
        from jiuwenswarm.server.runtime.session import session_metadata, session_history

        flushed = await asyncio.gather(
            asyncio.to_thread(session_metadata.flush_pending_writes, 10),
            asyncio.to_thread(session_history.flush_pending_writes, 10),
        )
        if not all(flushed):
            raise lc.LifecycleError(
                "STOP_TIMEOUT", "accepted history or metadata writes are still draining"
            )

    async def session(
        self,
        session_id: str,
        action: str,
        channel_id: str,
        *,
        parent_operation: str = "",
    ) -> dict:
        lc.validate_id(session_id)
        # Project lock must precede the session lock; project cascade already
        # owns it and explicitly supplies its operation ID.
        meta = lc.raw_metadata(session_id)
        project_id = lc.project_id_for(meta)
        if not parent_operation:
            async with self.lock("project", project_id):
                parent = lc.state("project", project_id).get("operation")
                if parent and parent["status"] != "completed":
                    raise lc.LifecycleError(
                        "OPERATION_IN_PROGRESS", "project operation is pending"
                    )
                return await self._session(
                    session_id, action, channel_id, project_id
                )
        return await self._session(
            session_id, action, channel_id, project_id
        )

    def _session_message_service(self):
        """Return the Runtime-attached mailbox, or None when messaging is off."""

        service = getattr(self.runtime, "session_message_service", None)
        return service if service is not None else None

    async def _stop_heartbeat(self, session_id: str) -> None:
        """Stop the Session's background Heartbeat instead of waiting it out.

        A live Heartbeat run must not turn archive or delete into a busy
        rejection: the action stops the run first, then reads a settled
        Session.  Failure is only logged, so a run that refuses to cancel
        still trips the ordinary busy check below.
        """
        stopper = getattr(self.runtime, "stop_heartbeat_runs", None)
        if not callable(stopper):
            return
        try:
            await stopper(session_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "session lifecycle could not stop heartbeat runs: session_id=%s",
                session_id,
                exc_info=True,
            )

    async def _stop_subagents(self, session_id: str, action: str, channel_id: str) -> None:
        """Release the Session's resident subagents instead of waiting them out.

        A subagent stays resident until something explicitly releases it, so it
        can outlive the user's own stop and keep the Session looking busy —
        archive and delete then reject it with SESSION_BUSY and the user is
        asked to stop a Session they already stopped.  The action releases them
        first, then reads a settled Session.  Failure is only logged, so a
        subagent that refuses to release still trips the ordinary busy check.
        """
        stopper = getattr(self.runtime, "stop_subagent_runtimes", None)
        if not callable(stopper):
            return
        try:
            await stopper(
                session_id,
                channel_id=channel_id,
                reason="session_archived" if action == "archive" else "session_deleted",
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "session lifecycle could not release subagents: session_id=%s",
                session_id,
                exc_info=True,
            )

    def _capture_generation(self, session_id: str) -> int | None:
        """Snapshot the runtime generation the background stop must target.

        A Session may be reopened (or unarchived) while cleanup is pending.
        Without the snapshot a late finalizer could close the new runtime.
        """
        coordinator = getattr(self.runtime, "_session_coordinator", None)
        snapshot = getattr(coordinator, "snapshot_session", None)
        if not callable(snapshot):
            return None
        try:
            value = snapshot(session_id)
        except Exception:  # noqa: BLE001
            return None
        return getattr(value, "generation", None)

    @staticmethod
    def _accepted_payload(
        session_id: str, action: str, project_id: str, operation: dict
    ) -> dict:
        if action == "archive":
            return dict(
                session_id=session_id,
                ok=True,
                archived=True,
                archived_at=operation.get("archived_at"),
                accepted=True,
                finalizing=True,
                stop_pending=True,
                project_id=project_id,
            )
        return dict(
            session_id=session_id,
            ok=True,
            accepted=True,
            finalizing=True,
            stop_pending=True,
            project_id=project_id,
        )

    def _spawn_finalizer(
        self,
        session_id: str,
        action: str,
        channel_id: str,
        project_id: str,
        generation: int | None,
    ) -> asyncio.Task | None:
        """Start (or reuse) the background cleanup for one Session."""
        operation = lc.state("session", session_id).get("operation") or {}
        operation_id = str(operation.get("operation_id") or "")
        existing = self._finalizing.get(session_id)
        if existing is not None and not existing.done():
            if self._finalizing_operations.get(session_id) == operation_id:
                return existing
            # A failed operation may have been undone while its retry waited
            # for a slot. Its task must never be reused by the new operation.
            existing.cancel()
        try:
            task = asyncio.create_task(
                self._finalize_session(
                    session_id,
                    action,
                    channel_id,
                    project_id,
                    generation,
                    operation_id=operation_id,
                ),
                name=f"session-finalize-{action}-{session_id}",
            )
        except RuntimeError:
            # No running loop (unit tests / synchronous callers): the persisted
            # operation stays retryable and recovery will pick it up.
            logger.debug(
                "session finalizer not started without a running loop: session_id=%s",
                session_id,
            )
            return None
        self._finalizing[session_id] = task
        self._finalizing_operations[session_id] = operation_id
        self._finalizer_tasks.add(task)

        def _done(completed: asyncio.Task, sid: str = session_id) -> None:
            self._finalizer_tasks.discard(completed)
            if self._finalizing.get(sid) is completed:
                self._finalizing.pop(sid, None)
                self._finalizing_operations.pop(sid, None)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("session finalizer crashed: session_id=%s", sid)

        task.add_done_callback(_done)
        return task

    async def _finalize_session(
        self,
        session_id: str,
        action: str,
        channel_id: str,
        project_id: str,
        generation: int | None,
        *,
        operation_id: str | None = None,
    ) -> None:
        if operation_id is None:
            operation = lc.state("session", session_id).get("operation") or {}
            operation_id = str(operation.get("operation_id") or "")
        async with self._finalize_semaphore:
            busy_exc: BaseException | None = None
            for _ in range(40):
                try:
                    async with self.lock("session", session_id):
                        await self._finalize_session_locked(
                            session_id,
                            action,
                            channel_id,
                            project_id,
                            generation,
                            operation_id,
                        )
                    return
                except asyncio.CancelledError:
                    # Shutdown or caller cancellation: leave a retryable
                    # failure marker so the next process (or the recovery loop)
                    # can finish the physical cleanup.
                    try:
                        updated = await asyncio.to_thread(
                            self._save_cleanup_failure,
                            session_id,
                            operation_id,
                            "operation cancelled",
                        )
                        if updated is not None:
                            self._poke_recovery()
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "session finalizer could not mark cancellation: "
                            "session_id=%s",
                            session_id,
                            exc_info=True,
                        )
                    raise
                except Exception as exc:  # noqa: BLE001
                    if getattr(exc, "code", "") == "OPERATION_IN_PROGRESS":
                        # The accepting request may still be unwinding its own
                        # lock (its async context manager awaits the lease
                        # teardown).  Wait briefly instead of dropping the
                        # cleanup; a genuinely busy user action falls through
                        # after the retry budget and recovery picks it up.
                        busy_exc = exc
                        await asyncio.sleep(0.05)
                        continue
                    operation = lc.state("session", session_id).get("operation") or {}
                    attempts = int(operation.get("cleanup_attempts") or 0) + 1
                    await self._record_finalize_failure(
                        session_id, action, attempts, exc, operation_id
                    )
                    return
            logger.warning(
                "session finalizer deferred by a live operation: session_id=%s",
                session_id,
            )
            # 「拿不到锁」也是失败：此前这里只打日志 + `_poke_recovery()`，既不写
            # `cleanup_next_at` 也不加 `cleanup_attempts`，有界重试预算对它完全无效——
            # `_recover` 每 10 秒重新 spawn 一次，同一个被残留 flock 占住的会话会被
            # 无限重试，每次还占一个收尾槽位约 2 秒。记一次可重试失败：退避按
            # `_cleanup_backoff` 生效，达到 `_MAX_CLEANUP_ATTEMPTS` 后转入 abandoned。
            operation = lc.state("session", session_id).get("operation") or {}
            attempts = int(operation.get("cleanup_attempts") or 0) + 1
            await self._record_finalize_failure(
                session_id,
                action,
                attempts,
                busy_exc or RuntimeError("session operation still in progress"),
                operation_id,
            )
            self._poke_recovery()

    async def _finalize_session_locked(
        self,
        session_id: str,
        action: str,
        channel_id: str,
        project_id: str,
        generation: int | None,
        operation_id: str,
    ) -> None:
        value = await asyncio.to_thread(lc.state, "session", session_id)
        operation = value.get("operation") or {}
        if operation.get("operation_id") != operation_id:
            return
        if operation.get("status") in {"completed", "abandoned"}:
            return
        if operation.get("kind") != action:
            return
        # Count the attempt before doing work so a process that repeatedly
        # crashes mid-cleanup still exhausts its budget instead of looping
        # forever across restarts.
        attempts = int(operation.get("cleanup_attempts") or 0) + 1
        if attempts > _MAX_CLEANUP_ATTEMPTS:
            await self._abandon_cleanup(
                session_id,
                action,
                attempts,
                "cleanup attempt limit reached",
                operation_id,
            )
            return
        cleanup_only = action == "delete" and bool(operation.get("runtime_deleted"))
        await asyncio.to_thread(
            lc.update,
            "session",
            session_id,
            status="running",
            phase="cleanup_mailbox" if cleanup_only else "stop_sessions",
            stop_pending=not cleanup_only,
            cleanup_attempts=attempts,
            cleanup_next_at=0,
        )
        try:
            await self._finalize_session_body(
                session_id,
                action,
                str(operation.get("channel_id") or channel_id),
                str(operation.get("project_id") or project_id),
                operation.get("stop_generation", generation),
                operation,
            )
        except Exception as exc:  # noqa: BLE001
            await self._record_finalize_failure(
                session_id, action, attempts, exc, operation_id
            )

    async def _finalize_session_body(
        self,
        session_id: str,
        action: str,
        channel_id: str,
        project_id: str,
        generation: int | None,
        operation: dict,
    ) -> None:
        if action == "delete" and operation.get("runtime_deleted"):
            await self._complete_session_delete(session_id, project_id)
            return
        active, archived = lc.session_paths(session_id)
        meta = lc.raw_metadata(session_id)
        owner_channel = str(meta.get("channel_id") or channel_id or "")
        if action == "delete":
            # The accept path only blocks new work; waiting for a consumer that
            # ignores cancellation belongs here, off the request path.
            mailbox = self._session_message_service()
            if mailbox is not None:
                await mailbox.begin_target_delete(session_id)
        if active.exists():
            await self._stop_heartbeat(session_id)
            await self._stop_subagents(session_id, action, owner_channel)
            running = True
            probe = getattr(self.runtime, "is_session_running", None)
            if callable(probe):
                try:
                    running = bool(probe(session_id))
                except Exception:  # noqa: BLE001
                    running = True
            if running or generation is not None or action == "delete":
                # Delete always drains the writer path, even when no execution
                # is marked RUNNING: orphan team streams and resident work
                # still own resources that must be released before rmtree.
                # Archive retries must also finish a captured generation: a
                # previous stop timeout can leave it QUIESCING after work exits.
                stopper = getattr(
                    self.runtime, "stop_session_for_lifecycle", None
                )
                if callable(stopper):
                    await stopper(
                        channel_id=owner_channel,
                        session_id=session_id,
                        generation=generation,
                        stop_runner=(action == "archive"),
                    )
                else:
                    # Older runtimes only expose the delete-oriented entry point.
                    await self.stop(session_id, owner_channel)
        await asyncio.to_thread(lc.update, "session", session_id, phase="flush_writes")
        from jiuwenswarm.server.runtime.session import (
            session_history,
            session_metadata,
        )

        flushed = await asyncio.gather(
            asyncio.to_thread(session_metadata.flush_pending_writes, 10),
            asyncio.to_thread(session_history.flush_pending_writes, 10),
        )
        if not all(flushed):
            # Accepted writes are best-effort now; the directory operation is
            # still attempted so a stuck writer cannot block the user forever.
            logger.warning(
                "session cleanup flushed with pending writes: session_id=%s action=%s",
                session_id,
                action,
            )
        await asyncio.to_thread(lc.update, "session", session_id, phase="fence_writes")
        await asyncio.to_thread(lc.fence_writes, "session", session_id)
        if action == "delete":
            await asyncio.to_thread(lc.update, "session", session_id, phase="delete_directory")
            result = await self.runtime.delete_session(
                channel_id=owner_channel, session_id=session_id
            )
            if not result.ok:
                raise lc.LifecycleError(
                    result.error_code or "DELETE_FAILED",
                    result.error_message or "delete failed",
                )
            # Persist this boundary before mailbox cleanup so a retry after
            # deletion never repeats destructive Runtime work or reopens FIFO.
            await asyncio.to_thread(
                lc.update,
                "session",
                session_id,
                phase="cleanup_mailbox",
                runtime_deleted=True,
                stop_pending=False,
            )
            await self._complete_session_delete(session_id, project_id)
            return
        await asyncio.to_thread(lc.update, "session", session_id, phase="move_directory")
        archived_at = float(operation.get("archived_at") or time.time())
        await asyncio.to_thread(
            self._move_session_directory,
            session_id,
            active,
            archived,
            "archive",
            archived_at,
        )
        from jiuwenswarm.server.runtime.session.session_metadata import (
            remove_session_metadata_cache,
        )

        remove_session_metadata_cache(session_id)
        if bool(operation.get("pin_reindex_required")):
            await asyncio.to_thread(self.reindex_pins)
        payload = dict(
            session_id=session_id,
            ok=True,
            archived=True,
            archived_at=archived_at,
            stop_pending=False,
            project_id=project_id,
        )
        await asyncio.to_thread(
            lc.complete, "session", session_id, archived=True, result=payload
        )

    async def _complete_session_delete(self, session_id: str, project_id: str) -> None:
        """Finish durable mailbox cleanup before publishing deletion completion."""
        mailbox = self._session_message_service()
        if mailbox is not None:
            await mailbox.on_target_deleted(session_id)
        payload = dict(session_id=session_id, ok=True, project_id=project_id)
        await asyncio.to_thread(
            lc.complete, "session", session_id, deleted=True, result=payload
        )
        from jiuwenswarm.server.runtime.recommendation_host import (
            active_recommendation_host,
        )

        host = active_recommendation_host()
        if host is not None:
            await host.session_left_project(str(project_id or ""), session_id)

    @staticmethod
    def _save_cleanup_failure(
        session_id: str,
        operation_id: str,
        reason: str,
        *,
        attempts: int | None = None,
        next_at: float = 0.0,
        exhausted: bool = False,
    ) -> dict | None:
        """Compare and save one operation's failure in the same worker thread.

        ``attempts`` 必须随失败一起落盘，不能只留局部变量：占用路径（拿不到跨进程锁）
        永远进不到 ``_finalize_session_locked``，而那是唯一写 ``cleanup_attempts`` 的地方。
        不落盘时 ``attempts`` 恒为 1，``_MAX_CLEANUP_ATTEMPTS`` 永不达成、
        ``_abandon_cleanup`` 不可达，收尾退化成无限重试。
        """
        with lc.resource_lock("session", session_id):
            operation = lc.state("session", session_id).get("operation") or {}
            if operation.get("operation_id") != operation_id or operation.get("status") in {
                "completed",
                "abandoned",
            }:
                return None
            changes: dict = dict(
                status="abandoned" if exhausted else "failed",
                stop_pending=False,
                retryable=not exhausted,
                cleanup_next_at=next_at,
                cleanup_exhausted=exhausted,
                errors=[reason],
            )
            if attempts is not None:
                # 单调不减：并发/重入时不要把已经记下的次数写回更小的值。
                changes["cleanup_attempts"] = max(
                    int(attempts), int(operation.get("cleanup_attempts") or 0)
                )
            return lc.update("session", session_id, **changes)

    async def _record_finalize_failure(
        self,
        session_id: str,
        action: str,
        attempts: int,
        exc: BaseException,
        operation_id: str,
    ) -> None:
        code = getattr(exc, "code", "") or ""
        non_retryable = code in {
            "BAD_REQUEST",
            "SESSION_ID_CONFLICT",
            "PROJECT_NAME_CONFLICT",
        }
        if non_retryable or attempts >= _MAX_CLEANUP_ATTEMPTS:
            await self._abandon_cleanup(
                session_id, action, attempts, str(exc), operation_id
            )
            return
        next_at = time.time() + _cleanup_backoff(attempts)
        updated = await asyncio.to_thread(
            self._save_cleanup_failure,
            session_id,
            operation_id,
            str(exc),
            attempts=attempts,
            next_at=next_at,
        )
        if updated is None:
            return
        logger.warning(
            "session cleanup failed: session_id=%s action=%s attempt=%d "
            "retry_at=%.0f error=%s",
            session_id,
            action,
            attempts,
            next_at,
            exc,
        )
        self._poke_recovery()

    async def _abandon_cleanup(
        self,
        session_id: str,
        action: str,
        attempts: int,
        reason: str,
        operation_id: str,
    ) -> None:
        operation = await asyncio.to_thread(
            self._save_cleanup_failure,
            session_id,
            operation_id,
            reason,
            attempts=attempts,
            exhausted=True,
        )
        if operation is None:
            return
        logger.error(
            "session cleanup abandoned: session_id=%s action=%s attempts=%d "
            "reason=%s",
            session_id,
            action,
            attempts,
            reason,
        )
        if action == "delete" and not operation.get("runtime_deleted"):
            mailbox = self._session_message_service()
            if mailbox is not None:
                try:
                    await mailbox.abort_target_delete(session_id)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "session cleanup could not release mailbox barrier: "
                        "session_id=%s",
                        session_id,
                        exc_info=True,
                    )

    async def _session(
        self,
        session_id: str,
        action: str,
        channel_id: str,
        project_id: str,
        *,
        preloaded_meta: dict | None = None,
    ) -> dict:
        async with self.lock("session", session_id):
            active, archived = lc.session_paths(session_id)
            previous = lc.state("session", session_id).get("operation")
            pending = bool(previous and previous.get("status") != "completed")
            if (
                not active.exists()
                and not archived.exists()
                and not (pending and action == "delete")
            ):
                raise lc.LifecycleError("NOT_FOUND", "session not found")
            # Batch callers already hold the project execution lock and pass
            # their inventory snapshot; only the single-session path re-reads.
            meta = (
                preloaded_meta
                if preloaded_meta is not None
                else lc.raw_metadata(session_id)
            )
            is_cron_session = bool(meta.get("cron_id")) or session_id.startswith(
                ("cron_", "heartbeat_")
            )
            # Subagent controls and session adapters are held per Agent, and
            # Agents are cached per channel: reach them through the Session's
            # own channel, not the one that happens to serve this request.
            owner_channel_id = str(meta.get("channel_id") or channel_id or "")
            # 两个方向都要重排置顶序号：归档后该会话离开活跃区（活跃区留空档），
            # 取消归档后它带着归档前的序号回到活跃区（可能与其他会话重复）。
            pin_reindex_required = action in {"archive", "unarchive"} and bool(
                meta.get("pinned")
            )
            if action != "delete" and is_cron_session:
                raise lc.LifecycleError(
                    "FORBIDDEN",
                    "cron and heartbeat sessions cannot be archived separately",
                )
            if action == "unarchive":
                return await self._unarchive_session(
                    session_id,
                    project_id,
                    active,
                    archived,
                    previous,
                    pending,
                    pin_reindex_required,
                )
            if action not in {"archive", "delete"}:
                raise lc.LifecycleError(
                    "BAD_REQUEST", "unknown session lifecycle action"
                )
            return await self._accept_session_cleanup(
                session_id,
                action,
                project_id,
                meta,
                active,
                archived,
                owner_channel_id,
                pin_reindex_required,
                previous,
                pending,
            )

    async def _accept_session_cleanup(
        self,
        session_id: str,
        action: str,
        project_id: str,
        meta: dict,
        active: Path,
        archived: Path,
        owner_channel_id: str,
        pin_reindex_required: bool,
        previous: dict | None,
        pending: bool,
    ) -> dict:
        """Persist the logical result and return without waiting for cleanup."""
        if action == "delete" and previous and previous.get("runtime_deleted"):
            project_id = str(previous.get("project_id") or project_id)
            owner_channel_id = str(previous.get("channel_id") or owner_channel_id)
        if pending and previous and previous.get("kind") != action:
            if (
                action == "delete"
                and previous.get("kind") == "archive"
                and previous.get("status") in {"failed", "abandoned"}
            ):
                # A failed archive still holds the logical "archived" result.
                # Deleting it must persist the new intent instead of bouncing
                # off the leftover operation; physical cleanup stays best-effort.
                lc.abandon_failed_operation("session", session_id, "archive")
                previous = lc.state("session", session_id).get("operation")
                pending = bool(previous and previous.get("status") != "completed")
            else:
                raise lc.LifecycleError(
                    "OPERATION_IN_PROGRESS",
                    "another lifecycle operation is pending",
                )
        mailbox = self._session_message_service() if action == "delete" else None
        if pending and previous and previous.get("status") == "running":
            # A duplicate click joins the in-flight cleanup.  Re-assert the
            # mailbox barrier synchronously in case this is a post-restart retry.
            if mailbox is not None:
                mailbox.block_target_delete(session_id)
            self._spawn_finalizer(
                session_id,
                action,
                owner_channel_id,
                project_id,
                previous.get("stop_generation"),
            )
            return self._accepted_payload(
                session_id, action, project_id, previous
            )
        if action == "archive" and not pending and not archived.exists():
            # Fence admission before checking Runtime state so a new task
            # cannot start between the idle check and the archive operation.
            lc.set_visibility(
                "session",
                session_id,
                blocked=True,
                write_blocked=False,
                deleted=False,
            )
            try:
                running_probe = getattr(self.runtime, "is_session_running", None)
                if not callable(running_probe):
                    raise lc.LifecycleError(
                        "RUNTIME_STATE_UNAVAILABLE",
                        "cannot verify whether the session has a running task",
                    )
                if running_probe(session_id):
                    raise lc.LifecycleError(
                        "SESSION_BUSY",
                        "session has a running task and cannot be archived",
                    )
            except Exception:
                lc.set_visibility(
                    "session",
                    session_id,
                    blocked=False,
                    write_blocked=False,
                    deleted=False,
                )
                raise
        if not pending and action == "archive" and archived.exists():
            return dict(
                session_id=session_id,
                ok=True,
                archived=True,
                archived_at=self.archive_time(session_id, archived, meta),
                accepted=True,
                finalizing=False,
                stop_pending=False,
                project_id=project_id,
            )
        operation = lc.begin(
            "session", session_id, action, block_execution=True
        )
        operation = lc.claim_operation(
            "session", session_id, self._owner_id
        )
        generation = self._capture_generation(session_id)
        # A retry after a failed pin reindex must not lose the requirement just
        # because metadata is temporarily unreadable.
        pin_reindex_required = bool(
            pin_reindex_required
            or (previous or {}).get("pin_reindex_required")
            or operation.get("pin_reindex_required")
        )
        lc.update(
            "session",
            session_id,
            project_id=project_id,
            channel_id=owner_channel_id,
            stop_generation=generation,
            pin_reindex_required=pin_reindex_required,
        )
        operation = lc.mark_finalizing(
            "session", session_id, deleted=(action == "delete")
        )
        if mailbox is not None:
            # Fast, non-blocking barrier.  Waiting for the consumer to exit is
            # part of the background finalizer, so the response is not delayed.
            mailbox.block_target_delete(session_id)
        self._spawn_finalizer(
            session_id, action, owner_channel_id, project_id, generation
        )
        return self._accepted_payload(session_id, action, project_id, operation)

    async def _unarchive_session(
        self,
        session_id: str,
        project_id: str,
        active: Path,
        archived: Path,
        previous: dict | None,
        pending: bool,
        pin_reindex_required: bool,
    ) -> dict:
        """Restore a Session synchronously; it never stops a live runtime."""
        took_over_failed_archive = False
        if pending and previous and previous.get("kind") == "archive":
            if previous.get("status") in {"failed", "abandoned"}:
                # A failed cleanup must not make the Session impossible to
                # unarchive.  Close the leftover operation, then take over.
                lc.abandon_failed_operation("session", session_id, "archive")
                previous = lc.state("session", session_id).get("operation")
                pending = bool(previous and previous.get("status") != "completed")
                took_over_failed_archive = True
            elif previous.get("status") != "completed":
                raise lc.LifecycleError(
                    "OPERATION_IN_PROGRESS",
                    "session archive cleanup is still running",
                )
        if not pending and active.exists():
            # An abandoned archive can leave the directory active while its
            # project is still hidden.  Undo is a restore operation, so bring
            # the project back too; otherwise the Session leaves the archive
            # list but remains invisible in a hidden project.
            await asyncio.to_thread(self._restore_hidden_project, project_id)
            # Already back in the active area: make sure a stale fence from an
            # earlier failure cannot keep it hidden from the workspace.
            lc.set_visibility("session", session_id, blocked=False, deleted=False)
            return dict(
                session_id=session_id,
                ok=True,
                restored=took_over_failed_archive,
                project_id=project_id,
            )
        operation = lc.begin(
            "session", session_id, "unarchive", block_execution=True
        )
        operation = lc.claim_operation("session", session_id, self._owner_id)
        pin_reindex_required = bool(
            pin_reindex_required or operation.get("pin_reindex_required")
        )
        lc.update(
            "session",
            session_id,
            project_id=project_id,
            pin_reindex_required=pin_reindex_required,
        )
        try:
            # 项目被移除时,归档区是它唯一还能被看到的内容。先把项目
            # 恢复出来,否则会话离开归档页后在工作区里也找不到。
            await asyncio.to_thread(self._restore_hidden_project, project_id)
            lc.update(
                "session",
                session_id,
                phase="move_directory",
                status="running",
                stop_pending=False,
            )
            # The move, its PermissionError backoff and the metadata rewrite
            # stay under the cross-process resource lock, but run in a worker
            # thread so the retry sleeps never stall the loop.
            await asyncio.to_thread(
                self._move_session_directory,
                session_id,
                archived,
                active,
                "unarchive",
                operation.get("archived_at") or time.time(),
            )
            from jiuwenswarm.server.runtime.session.session_metadata import (
                remove_session_metadata_cache,
            )

            remove_session_metadata_cache(session_id)
            if pin_reindex_required:
                # 刚搬回活跃区的会话仍在执行栅栏内，reindex_pins 默认跳过
                # blocked 会话，这里显式让它写回这一条的序号。
                await asyncio.to_thread(
                    self.reindex_pins, include_blocked=session_id
                )
            payload = dict(
                session_id=session_id,
                ok=True,
                restored=True,
                project_id=project_id,
            )
            lc.complete("session", session_id, result=payload)
            return payload
        except Exception as exc:
            lc.update(
                "session",
                session_id,
                status="failed",
                errors=[str(exc)],
                retryable=getattr(exc, "code", "")
                not in {
                    "SESSION_ID_CONFLICT",
                    "BAD_REQUEST",
                    "PROJECT_NAME_CONFLICT",
                },
            )
            # Visibility follows the directory's actual position: a failed
            # unarchive must never leave an active Session hidden.
            lc.set_visibility(
                "session",
                session_id,
                blocked=not active.exists(),
                deleted=False,
            )
            self._poke_recovery()
            if isinstance(exc, lc.LifecycleError):
                raise
            raise lc.LifecycleError("RESTORE_FAILED", str(exc)) from exc
        except BaseException:
            lc.update(
                "session",
                session_id,
                status="failed",
                errors=["operation cancelled"],
                retryable=True,
            )
            lc.set_visibility(
                "session",
                session_id,
                blocked=not active.exists(),
                deleted=False,
            )
            self._poke_recovery()
            raise

    @staticmethod
    def _restore_hidden_project(project_id: str) -> None:
        """Bring a removed project back before one of its sessions is unarchived.

        An archived session is the only part of a removed project still on
        screen, so undoing its archive restores the project as well.  Without
        that the session would leave the archive page and stay hidden in the
        workspace, since a hidden project's sessions are excluded everywhere.
        A name already taken by another project is reported instead of silently
        restoring under a different name, and the archive is left untouched.
        """
        if not project_id or is_default_project_id(project_id):
            return
        # Recheck atomically: visibility may change after a batch snapshot.
        try:
            project_store.restore_project(project_id)
        except project_store.ProjectNameConflict as exc:
            raise lc.LifecycleError(
                "PROJECT_NAME_CONFLICT",
                "a project with this name exists; rename it before restoring",
            ) from exc

    @staticmethod
    def _move_session_directory(
        session_id: str,
        source: Path,
        destination: Path,
        action: str,
        archived_at: float,
    ) -> None:
        """Move the session directory and rewrite its metadata.

        Runs in a worker thread: the PermissionError retry backoff sleeps must
        never stall the event loop.  The body is fully synchronous on purpose —
        the cross-process resource lock cannot be awaited under.
        """
        with lc.resource_lock("session", session_id):
            if source.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                for attempt in range(5):
                    try:
                        if destination.exists():
                            raise lc.LifecycleError(
                                "SESSION_ID_CONFLICT", "destination exists"
                            )
                        os.replace(source, destination)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        # Windows 句柄常被杀软/索引器多握几秒：线性 50ms 步进
                        # 累计约 0.5s 就放弃；指数退避把重试窗口放大到约 3s。
                        time.sleep(0.2 * 2 ** attempt)
            meta = lc.read_json(destination / "metadata.json")
            if action == "archive":
                # 置顶状态原样保留:归档只是离开活跃区,取消归档后会话要回到
                # 置顶区。活跃区置顶序号的紧凑化由调用方的 reindex_pins 负责,
                # 归档区会话不在其扫描范围内,因此不会占用活跃区序号。
                meta.update(
                    archived=True,
                    archived_at=archived_at,
                )
            else:
                meta.pop("archived", None)
                meta.pop("archived_at", None)
            try:
                lc.atomic_json(destination / "metadata.json", meta)
            except Exception:
                if not source.exists() and destination.exists():
                    try:
                        os.replace(destination, source)
                    except OSError:
                        logger.warning(
                            "session move rollback failed: %s",
                            session_id,
                            exc_info=True,
                        )
                raise

    @staticmethod
    def reindex_pins(*, include_blocked: str = "") -> None:
        """Renumber the active pinned sessions to a compact 1..N ordering.

        Sessions under a live lifecycle operation are skipped: their own
        operation owns the metadata until it completes.  ``include_blocked``
        names the one session that must be written anyway — an unarchive that
        just moved the directory back and is still inside its execution fence.
        """
        from jiuwenswarm.server.runtime.session.session_metadata import (
            remove_session_metadata_cache,
        )

        root = get_agent_sessions_dir()
        pinned = []
        for directory in root.iterdir() if root.exists() else ():
            if directory.is_dir():
                meta = lc.raw_metadata(directory.name)
                if meta.get("pinned"):
                    pinned.append((int(meta.get("pin_order", 0)), directory.name))
        for order, (_, sid) in enumerate(sorted(pinned), 1):
            with lc.resource_lock("session", sid):
                if sid != include_blocked and lc.state("session", sid).get("blocked"):
                    continue
                path = lc.resolve_session(sid)
                meta = lc.read_json(path / "metadata.json")
                meta["pin_order"] = order
                lc.atomic_json(path / "metadata.json", meta)
            remove_session_metadata_cache(sid)

    @staticmethod
    def archive_time(session_id: str, directory: Path, meta: dict) -> float:
        if meta.get("archived_at"):
            return float(meta["archived_at"])
        with lc.resource_lock("session", session_id):
            if lc.resolve_session(session_id) != directory:
                raise lc.LifecycleError(
                    "OPERATION_IN_PROGRESS", "session moved during archive query"
                )
            operation = lc.state("session", session_id).get("operation", {})
            value = lc.state("session", session_id)
            stamp = (
                operation.get("archived_at")
                or value.get("archive_time_repair")
                or directory.stat().st_mtime
            )
            value["archive_time_repair"] = stamp
            lc.save_locked("session", session_id, value)
            meta = lc.read_json(directory / "metadata.json")
            meta.update(archived=True, archived_at=stamp)
            lc.atomic_json(directory / "metadata.json", meta)
            return float(stamp)

    @staticmethod
    def _archived_entries() -> list[Path]:
        """Snapshot of the archived root; one retry absorbs transient failures.

        Lazy enumeration on Windows can fail mid-scan when a concurrent
        delete removes entries.  A persistent failure is a real problem.
        """
        root = get_agent_sessions_dir().parent / "sessions_archived"
        for _ in range(2):
            try:
                return list(root.iterdir()) if root.exists() else []
            except OSError:
                logger.warning(
                    "archived session enumeration failed; retrying", exc_info=True
                )
        raise lc.LifecycleError(
            "ARCHIVE_SCAN_FAILED", "archived session directory is unreadable"
        )

    @staticmethod
    def _listable_archived_directory(directory: Path, root_resolved: Path) -> bool:
        """Whether an archived-root entry is a plain directory inside the root.

        Symlinks are rejected directly; junctions (reparse points that
        ``is_symlink`` misses on Windows) are rejected by comparing the
        resolved parent against the resolved root, mirroring the
        ``session_paths`` escape guard.  Permission errors propagate —
        callers must keep this inside their per-item isolation.
        """
        if not directory.is_dir() or directory.is_symlink():
            return False
        return directory.resolve().parent == root_resolved

    def _backfill_archive_times(self) -> None:
        """One-shot startup migration: persist ``archived_at`` for legacy sessions.

        Listings resolve ``archived_at`` read-only, so sessions archived before
        the field existed must be repaired once here (locked writes belong to
        this migration and to the move transaction, never to a listing).
        """
        try:
            entries = self._archived_entries()
        except lc.LifecycleError:
            logger.warning("archived_at backfill scan failed", exc_info=True)
            return
        root_resolved = (
            get_agent_sessions_dir().parent / "sessions_archived"
        ).resolve()
        for directory in entries:
            sid = directory.name
            try:
                if not self._listable_archived_directory(directory, root_resolved):
                    continue
                meta = lc.read_json(directory / "metadata.json")
                if meta and not meta.get("archived_at"):
                    self.archive_time(sid, directory, meta)
            except Exception:
                logger.debug(
                    "archived_at backfill deferred: %s", sid, exc_info=True
                )

    @staticmethod
    def _listing_archived_at(directory: Path, meta: dict, value: dict) -> float:
        """Read-only ``archived_at`` for listings: no lock, no repair writes.

        The move transaction and the startup backfill persist the stamp in
        metadata.  A session that still lacks it falls back to the operation
        state, a previous repair marker, or the directory mtime — the request
        path never blocks on the session resource lock or fsyncs.
        """
        if meta.get("archived_at"):
            return float(meta["archived_at"])
        operation = value.get("operation") or {}
        stamp = (
            operation.get("archived_at")
            or value.get("archive_time_repair")
            or directory.stat().st_mtime
        )
        return float(stamp)

    def list_sessions(self, params: dict) -> dict:
        root = get_agent_sessions_dir().parent / "sessions_archived"
        # One projects.json read for the whole listing; the former per-session
        # cache-busted lookup was an N+1 of locked full-file reads.
        all_projects = project_store.list_projects(include_hidden=True, cache_bust=True)
        projects = {project.project_id: project for project in all_projects}
        project_lookup = None
        # One lifecycle state read per project, shared by all its sessions.
        project_states: dict[str, dict] = {}
        items = []
        # Snapshot before any per-item work; transient enumeration races on
        # Windows are retried inside the helper.
        entries = self._archived_entries()
        root_resolved = root.resolve()
        for directory in entries:
            sid = directory.name
            try:
                if not self._listable_archived_directory(directory, root_resolved):
                    # Symlinked or junctioned entries must never surface
                    # storage outside the managed root; destructive paths
                    # keep the session_paths guard.  The check itself may
                    # raise (e.g. permission errors) — hence inside the try.
                    continue
                # Read the enumerated directory directly instead of
                # re-resolving the session's current location: the resolve
                # re-stats both storage areas per item and turns a mid-scan
                # move into a hard failure for the whole listing.
                meta = lc.read_json(directory / "metadata.json")
                if not meta:
                    # Vanished mid-scan (unarchive/delete) or foreign junk.
                    continue
                if not meta.get("project_id") and meta.get("project_dir"):
                    if project_lookup is None:
                        project_lookup = lc.build_project_lookup()
                    pid = lc.project_id_for(meta, project_lookup=project_lookup)
                else:
                    pid = lc.project_id_for(meta)
                project = projects.get(pid)
                # One state read per session feeds both the archived_at
                # fallback and the lifecycle projection.
                value = lc.state("session", sid)
                if lc.logically_deleted(value):
                    # A delete accepted while the directory still lives here.
                    continue
                if pid not in project_states:
                    project_states[pid] = lc.state("project", pid)
                items.append(
                    {
                        **to_session_info(meta),
                        "session_id": sid,
                        "project_id": pid,
                        "archived": True,
                        "archived_at": self._listing_archived_at(
                            directory, meta, value
                        ),
                        # 展示语义:归档项不出现在置顶区。会话自身的置顶状态仍保存在
                        # metadata 中,取消归档时原样恢复(见 _move_session_directory)。
                        "pinned": False,
                        "pin_order": 0,
                        "project_name": project.name if project else None,
                        # 项目被移除后归档页是它唯一的展示位:前端据此提示用户,
                        # 并说明撤销归档会连带恢复该项目。
                        "project_hidden": bool(project.hidden) if project else False,
                        **lc.projection(
                            "session",
                            sid,
                            project_id=pid,
                            value=value,
                            archived=True,
                            project_value=project_states[pid],
                        ),
                    }
                )
            except Exception:
                # One moving or unusable entry must never fail the whole
                # listing: a mid-scan unarchive/delete raises NOT_FOUND /
                # OPERATION_IN_PROGRESS, stray directory names fail
                # validate_id, corrupt metadata fails to parse.
                logger.debug(
                    "archived listing skipped session %s", sid, exc_info=True
                )
                continue
        # Archive is accepted before its directory moves.  Surface those
        # sessions immediately so a Session never disappears from both the
        # active list and the archive page while cleanup runs in the
        # background.
        listed = {item.get("session_id") for item in items}
        active_root = get_agent_sessions_dir()
        for directory in active_root.iterdir() if active_root.exists() else ():
            sid = directory.name
            try:
                if sid in listed or not directory.is_dir() or directory.is_symlink():
                    continue
                value = lc.state("session", sid)
                if not lc.logically_archived(value):
                    continue
                meta = lc.read_json(directory / "metadata.json")
                if not meta:
                    continue
                if not meta.get("project_id") and meta.get("project_dir"):
                    if project_lookup is None:
                        project_lookup = lc.build_project_lookup()
                    pid = lc.project_id_for(meta, project_lookup=project_lookup)
                else:
                    pid = lc.project_id_for(meta)
                project = projects.get(pid)
                if pid not in project_states:
                    project_states[pid] = lc.state("project", pid)
                operation = value.get("operation") or {}
                items.append(
                    {
                        **to_session_info(meta),
                        "session_id": sid,
                        "project_id": pid,
                        "archived": True,
                        "archived_at": self._listing_archived_at(
                            directory, meta, value
                        ),
                        "pinned": False,
                        "pin_order": 0,
                        "project_name": project.name if project else None,
                        "project_hidden": bool(project.hidden) if project else False,
                        "finalizing": operation.get("status") == "running",
                        "cleanup_exhausted": bool(
                            operation.get("cleanup_exhausted")
                        ),
                        **lc.projection(
                            "session",
                            sid,
                            project_id=pid,
                            value=value,
                            archived=True,
                            project_value=project_states[pid],
                        ),
                    }
                )
            except Exception:
                logger.debug(
                    "pending archive listing skipped session %s", sid, exc_info=True
                )
                continue
        return lc.page(items, params, "sessions", 200)

    @staticmethod
    def project_session_inventory(project_id: str) -> list[ProjectSessionInventoryItem]:
        """Return a stable, operation-local project membership snapshot.

        The session directory is not partitioned by project.  A scan is still
        required, but metadata is read exactly once per directory and the
        legacy project lookup is built at most once for the entire scan.
        Callers must use this inventory for selection instead of re-reading
        metadata immediately after ``project_sessions``.
        """
        active = get_agent_sessions_dir()
        result: list[ProjectSessionInventoryItem] = []
        project_lookup = None
        scanned = 0
        started_at = time.perf_counter()
        for root in (active, active.parent / "sessions_archived"):
            for path in root.iterdir() if root.exists() else ():
                if not path.is_dir():
                    continue
                scanned += 1
                value = lc.state("session", path.name)
                if lc.logically_deleted(value):
                    # Already accepted for removal; do not select it again.
                    continue
                logically_archived = lc.logically_archived(value)
                metadata = lc.raw_metadata(path.name)
                if not metadata.get("project_id") and metadata.get("project_dir"):
                    if project_lookup is None:
                        project_lookup = lc.build_project_lookup()
                    resolved_project_id = lc.project_id_for(
                        metadata, project_lookup=project_lookup
                    )
                else:
                    resolved_project_id = lc.project_id_for(metadata)
                if resolved_project_id == project_id:
                    result.append(
                        ProjectSessionInventoryItem(
                            session_id=path.name,
                            metadata=metadata,
                            active=root == active,
                            logically_archived=logically_archived,
                        )
                    )
        logger.info(
            "project session inventory: project_id=%s scanned=%d matched=%d "
            "legacy_lookup=%s elapsed_ms=%.1f",
            project_id,
            scanned,
            len(result),
            project_lookup is not None,
            (time.perf_counter() - started_at) * 1000,
        )
        return result

    @staticmethod
    def project_sessions(project_id: str) -> list[str]:
        return [
            item.session_id
            for item in SessionArchiveService.project_session_inventory(project_id)
        ]

    @staticmethod
    def _cron_session_name_matches(session_id: str, cron_id: str) -> bool:
        # 共享实现见 common/cron_session.py（project.get_cron_sessions 的兜底
        # 匹配与此同源，避免两处约定漂移）。
        return cron_session_matches_job(session_id, cron_id)

    @staticmethod
    def cron_sessions(cron_id: str) -> list[str]:
        active = get_agent_sessions_dir()
        result = []
        for root in (active, active.parent / "sessions_archived"):
            for path in root.iterdir() if root.exists() else ():
                if not path.is_dir():
                    continue
                # Name match first: conventionally named cron_* sessions are
                # identified without touching their metadata on disk.
                if not SessionArchiveService._cron_session_name_matches(
                    path.name, cron_id
                ) and lc.raw_metadata(path.name).get("cron_id") != cron_id:
                    continue
                result.append(path.name)
        return result

    @staticmethod
    def _cron_session_entries(cron_id: str) -> list[tuple[str, dict]]:
        """Cron-bound sessions plus the metadata needed to group them by project."""
        active = get_agent_sessions_dir()
        result = []
        for root in (active, active.parent / "sessions_archived"):
            for path in root.iterdir() if root.exists() else ():
                if not path.is_dir():
                    continue
                meta = lc.raw_metadata(path.name)
                if SessionArchiveService._cron_session_name_matches(
                    path.name, cron_id
                ) or meta.get("cron_id") == cron_id:
                    result.append((path.name, meta))
        return result

    async def delete_cron_sessions(self, cron_id: str, channel_id: str) -> dict:
        lc.validate_id(cron_id)
        # Sessions of one cron job almost always share a project; take each
        # project's execution lock once per group instead of once per session,
        # and keep the directory-scan order in the returned results.
        entries = await asyncio.to_thread(self._cron_session_entries, cron_id)
        order = {sid: index for index, (sid, _) in enumerate(entries)}
        by_project: dict[str, list[tuple[str, dict]]] = {}
        project_lookup = None
        for sid, meta in entries:
            if not meta.get("project_id") and meta.get("project_dir"):
                if project_lookup is None:
                    project_lookup = lc.build_project_lookup()
                pid = lc.project_id_for(meta, project_lookup=project_lookup)
            else:
                pid = lc.project_id_for(meta)
            by_project.setdefault(pid, []).append((sid, meta))
        results: dict[str, dict] = {}
        for pid in sorted(by_project):
            group = by_project[pid]
            index = 0
            try:
                async with self.lock("project", pid):
                    parent = lc.state("project", pid).get("operation")
                    if parent and parent["status"] != "completed":
                        raise lc.LifecycleError(
                            "OPERATION_IN_PROGRESS", "project operation is pending"
                        )
                    while index < len(group):
                        sid, meta = group[index]
                        index += 1
                        try:
                            results[sid] = await self._session(
                                sid, "delete", channel_id, pid, preloaded_meta=meta
                            )
                        except lc.LifecycleError as exc:
                            results[sid] = dict(
                                session_id=sid, ok=False, code=exc.code, error=str(exc)
                            )
            except lc.LifecycleError as exc:
                # Lock entry or the parent-operation check failed for the
                # group; mirror the former per-session failure entries.
                for sid, _ in group[index:]:
                    results[sid] = dict(
                        session_id=sid, ok=False, code=exc.code, error=str(exc)
                    )
        ordered = [results[sid] for sid in sorted(results, key=order.__getitem__)]
        return dict(
            cron_id=cron_id,
            succeeded_count=sum(item["ok"] for item in ordered),
            failed_count=sum(not item["ok"] for item in ordered),
            results=ordered,
        )

    async def project_batch(
        self, project_id: str, action: str, channel_id: str
    ) -> dict:
        lc.validate_id(project_id)
        if action not in {"archive", "delete_archived"}:
            raise lc.LifecycleError("BAD_REQUEST", "unknown session batch operation")
        async with self.lock("project", project_id):
            lc.guard(project_id=project_id)
            default_project = is_default_project_id(project_id)
            project = (
                None
                if default_project
                else project_store.get_project_by_id(project_id, cache_bust=True)
            )
            if not default_project and project is None:
                raise lc.LifecycleError("NOT_FOUND", "project not found")
            # 隐藏项目对前端不可见,不该再接受新的批量归档(与 project.get_sessions
            # 对隐藏项目的 NOT_FOUND 一致)。delete_archived 保持放行:归档页是
            # 已移除项目归档会话的展示位,清空入口依赖它。
            if project is not None and project.hidden and action == "archive":
                raise lc.LifecycleError("NOT_FOUND", "project not found")
            inventory = await asyncio.to_thread(
                self.project_session_inventory, project_id
            )
            inventory_by_id = {item.session_id: item for item in inventory}
            ids = []
            for item in inventory:
                sid = item.session_id
                meta = item.metadata
                if action == "archive":
                    if (
                        item.active
                        and not item.logically_archived
                        and not (
                            meta.get("cron_id")
                            or sid.startswith(("cron_", "heartbeat_"))
                        )
                    ):
                        ids.append(sid)
                elif not item.active or item.logically_archived:
                    # "Clear archive" must include Sessions whose archive was
                    # accepted logically but whose directory never moved.
                    ids.append(sid)
            results = []
            for sid in sorted(set(ids)):
                try:
                    result = await self._session(
                        sid,
                        "archive" if action == "archive" else "delete",
                        channel_id,
                        project_id,
                        preloaded_meta=inventory_by_id[sid].metadata,
                    )
                    results.append(result)
                except lc.LifecycleError as exc:
                    results.append(
                        dict(
                            session_id=sid,
                            ok=False,
                            code=exc.code,
                            error=str(exc),
                            **exc.details,
                        )
                    )
            succeeded = sum(item["ok"] for item in results)
            return dict(
                project_id=project_id,
                succeeded_count=succeeded,
                failed_count=len(results) - succeeded,
                results=results,
            )
