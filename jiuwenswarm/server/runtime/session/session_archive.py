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

from jiuwenswarm.common.work_mode import is_default_project_id
from jiuwenswarm.server.runtime.session import lifecycle as lc, project_store
from jiuwenswarm.server.runtime.session.session_info import to_session_info

logger = logging.getLogger(__name__)

# Project deletion checkpoints progress every K processed sessions instead of
# after each one.  Session state files remain the crash-recovery truth; the
# project file is only a fast-path cache, so periodic refreshes suffice.
_PROJECT_DELETE_CHECKPOINT_EVERY = 10


def get_agent_sessions_dir():
    return lc.get_agent_sessions_dir()


@dataclass(frozen=True)
class ProjectSessionInventoryItem:
    """One session found while scanning the active and archived roots."""

    session_id: str
    metadata: dict
    active: bool


class SessionArchiveService:
    def __init__(self, runtime):
        try:
            lc.migrate_project_archives()
        except Exception:
            # 一次性数据迁移不得阻断服务启动；marker 未写入时会于下次启动重试。
            logger.exception("project archive migration failed; retry on next start")
        self.runtime = runtime
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._stopping: dict[str, asyncio.Task] = {}
        self._recovery_task: asyncio.Task | None = None
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
                    lc.renew_operation(kind, resource_id, self._owner_id, release=True)
                finally:
                    owner.__exit__(None, None, None)

    def start_recovery(self):
        if self._recovery_task is None:
            self._recovery_task = asyncio.create_task(
                self._recover(), name="session-lifecycle-recovery"
            )

    async def close(self):
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            await asyncio.gather(self._recovery_task, return_exceptions=True)
            self._recovery_task = None
        tasks = list(self._stopping.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
                    if not operation or not operation.get("retryable", True):
                        continue
                    if (
                        operation.get("status") == "completed"
                        or operation.get("kind") == "archive"
                    ):
                        continue
                    sid = operation.get("resource_id")
                    attempts, next_try = failures.get(sid, (0, 0))
                    if time.monotonic() < next_try:
                        continue
                    await self.session(sid, operation.get("kind", ""), "")
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
            await asyncio.sleep(10)

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
        pre_stopped: bool = False,
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
                    session_id, action, channel_id, project_id,
                    pre_stopped=pre_stopped,
                )
        return await self._session(
            session_id, action, channel_id, project_id, pre_stopped=pre_stopped
        )

    def _session_message_service(self):
        """Return the Runtime-attached mailbox, or None when messaging is off."""

        service = getattr(self.runtime, "session_message_service", None)
        return service if service is not None else None

    def _session_is_busy_for_action(
        self,
        session_id: str,
        action: str,
        active: Path,
        is_cron_session: bool,
        *,
        parked_team_streams: bool = False,
    ) -> bool:
        """Whether this lifecycle action must wait for an active session to stop."""
        if action not in {"archive", "delete"}:
            return False
        if not active.exists():
            return False
        if parked_team_streams:
            # Parked Team stream handlers no longer own team work; only the
            # persistent leader stream keeps them alive.  The archive proceeds
            # and leaves that stream alone.
            return False
        if action == "delete" and is_cron_session:
            return False
        return self.runtime.is_session_running(session_id)

    async def _session(
        self,
        session_id: str,
        action: str,
        channel_id: str,
        project_id: str,
        *,
        pre_stopped: bool = False,
        defer_pin_reindex: bool = False,
        preloaded_meta: dict | None = None,
    ) -> dict:
        async with self.lock("session", session_id):
            active, archived = lc.session_paths(session_id)
            previous = lc.state("session", session_id).get("operation")
            pending = previous and previous["status"] != "completed"
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
            pin_reindex_required = action == "archive" and bool(meta.get("pinned"))
            if action != "delete" and is_cron_session:
                raise lc.LifecycleError(
                    "FORBIDDEN",
                    "cron and heartbeat sessions cannot be archived separately",
                )
            if not pending:
                if action == "archive" and archived.exists():
                    return dict(
                        session_id=session_id,
                        ok=True,
                        archived=True,
                        archived_at=self.archive_time(session_id, archived, meta),
                        stop_pending=False,
                        project_id=project_id,
                    )
                if action == "unarchive" and active.exists():
                    return dict(
                        session_id=session_id,
                        ok=True,
                        restored=False,
                        project_id=project_id,
                    )
            parked_team_streams = False
            if action == "archive" and self.runtime.is_session_running(session_id):
                probe = getattr(self.runtime, "has_parked_team_streams", None)
                parked_team_streams = callable(probe) and bool(probe(session_id))
            if self._session_is_busy_for_action(
                session_id,
                action,
                active,
                is_cron_session,
                parked_team_streams=parked_team_streams,
            ):
                raise lc.LifecycleError(
                    "SESSION_BUSY",
                    f"Session is running; stop it before {action}",
                    {"stop_pending": False},
                )
            operation = lc.begin(
                "session", session_id, action, block_execution=action != "archive"
            )
            operation = lc.claim_operation("session", session_id, self._owner_id)
            # Moving an archive clears metadata.pinned before reindexing can
            # fail. Persist the requirement before the move so retries retain
            # it, including retries handled by a new service instance.
            pin_reindex_required = action == "archive" and (
                pin_reindex_required or bool(operation.get("pin_reindex_required"))
            )
            lc.update(
                "session", session_id, project_id=project_id,
                pin_reindex_required=pin_reindex_required,
            )
            mailbox = self._session_message_service() if action == "delete" else None
            if mailbox is not None:
                # 删除屏障先于 stop 生效：阻止信箱新执行并取消目标消费者。
                await mailbox.begin_target_delete(session_id)
            try:
                if action == "delete":
                    lc.update(
                        "session", session_id, phase="stop_sessions", status="running"
                    )
                    # 归档区会话没有运行时生产者，无需 stop 与全局 flush 屏障；
                    # 项目级预停止过的会话也不再重复停止，避免二次排队等待。
                    if active.exists() and not pre_stopped:
                        await self.stop(
                            session_id, str(meta.get("channel_id") or channel_id)
                        )
                    lc.fence_writes("session", session_id)
                elif action == "archive":
                    # Flush accepted writes, but do not stop or close the runtime.
                    from jiuwenswarm.server.runtime.session import (
                        session_metadata,
                        session_history,
                    )

                    flushed = await asyncio.gather(
                        asyncio.to_thread(session_metadata.flush_pending_writes, 10),
                        asyncio.to_thread(session_history.flush_pending_writes, 10),
                    )
                    if not all(flushed):
                        raise lc.LifecycleError(
                            "ARCHIVE_FAILED", "Session writes are still pending"
                        )
                if action == "delete":
                    lc.update("session", session_id, phase="delete_directory")
                    result = await self.runtime.delete_session(
                        channel_id=channel_id, session_id=session_id
                    )
                    if not result.ok:
                        raise lc.LifecycleError(
                            result.error_code or "DELETE_FAILED",
                            result.error_message or "delete failed",
                        )
                    if mailbox is not None:
                        # queued/running/waiting_user/unknown 统一记 cancelled 并清正文。
                        await mailbox.on_target_deleted(session_id)
                    payload = dict(
                        session_id=session_id, ok=True, project_id=project_id
                    )
                    lc.complete("session", session_id, deleted=True, result=payload)
                    return payload
                source, destination = (
                    (active, archived) if action == "archive" else (archived, active)
                )
                lc.update(
                    "session",
                    session_id,
                    phase="move_directory",
                    status="running",
                    stop_pending=False,
                )
                # The move, its PermissionError backoff and the metadata
                # rewrite stay under the cross-process resource lock, but run
                # in a worker thread so the retry sleeps never stall the loop.
                await asyncio.to_thread(
                    self._move_session_directory,
                    session_id,
                    source,
                    destination,
                    action,
                    operation["archived_at"],
                )
                from jiuwenswarm.server.runtime.session.session_metadata import (
                    remove_session_metadata_cache,
                )

                remove_session_metadata_cache(session_id)
                if (
                    action == "archive"
                    and not defer_pin_reindex
                    and pin_reindex_required
                ):
                    await asyncio.to_thread(self.reindex_pins)
                payload = (
                    dict(
                        session_id=session_id,
                        ok=True,
                        archived=True,
                        archived_at=operation["archived_at"],
                        stop_pending=False,
                        project_id=project_id,
                    )
                    if action == "archive"
                    else dict(
                        session_id=session_id,
                        ok=True,
                        restored=True,
                        project_id=project_id,
                    )
                )
                deferred_pin_reindex = action == "archive" and defer_pin_reindex
                if deferred_pin_reindex:
                    # The batch owner consumes this private marker before
                    # returning its public result.  A pinned session was
                    # removed from the active ordering and requires one
                    # reindex after the whole batch, not one per session.
                    deferred_pin_reindex_required = pin_reindex_required
                lc.complete(
                    "session", session_id, archived=action == "archive", result=payload
                )
                if deferred_pin_reindex:
                    payload["_pins_reindex_required"] = deferred_pin_reindex_required
                return payload
            except Exception as exc:
                if mailbox is not None:
                    # 删除失败：解除屏障并恢复该目标的信箱队列消费。
                    try:
                        await mailbox.abort_target_delete(session_id)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "session.delete failed to resume mailbox consumer: "
                            "session_id=%s",
                            session_id,
                        )
                lc.update(
                    "session",
                    session_id,
                    status="failed",
                    stop_pending=isinstance(exc, lc.LifecycleError)
                    and exc.code == "STOP_TIMEOUT",
                    errors=[str(exc)],
                    retryable=getattr(exc, "code", "")
                    not in {"SESSION_ID_CONFLICT", "BAD_REQUEST"},
                )
                if isinstance(exc, lc.LifecycleError):
                    if exc.code in {"STOP_TIMEOUT", "STOP_SUBMIT_FAILED"}:
                        raise lc.LifecycleError(
                            "ARCHIVE_FAILED"
                            if action == "archive"
                            else "DELETE_FAILED",
                            "runtime or queued writers could not be isolated: "
                            + str(exc),
                            {
                                "stop_pending": exc.code == "STOP_TIMEOUT",
                                "warnings": [
                                    {
                                        "session_id": session_id,
                                        "code": exc.code,
                                        "message": str(exc),
                                    }
                                ],
                            },
                        ) from exc
                    raise
                raise lc.LifecycleError(
                    "ARCHIVE_FAILED"
                    if action == "archive"
                    else "RESTORE_FAILED"
                    if action == "unarchive"
                    else "DELETE_FAILED",
                    str(exc),
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
                        time.sleep(0.05 * (attempt + 1))
            meta = lc.read_json(destination / "metadata.json")
            if action == "archive":
                meta.update(
                    archived=True,
                    archived_at=archived_at,
                    pinned=False,
                    pin_order=0,
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
    def reindex_pins():
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
                if lc.state("session", sid).get("blocked"):
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

    def list_sessions(self, params: dict) -> dict:
        root = get_agent_sessions_dir().parent / "sessions_archived"
        # One projects.json read for the whole listing; the former per-session
        # cache-busted lookup was an N+1 of locked full-file reads.
        all_projects = project_store.list_projects(include_hidden=True, cache_bust=True)
        projects = {project.project_id: project for project in all_projects}
        project_lookup = None
        items = []
        for directory in root.iterdir() if root.exists() else ():
            if not directory.is_dir():
                continue
            sid = directory.name
            meta = lc.raw_metadata(sid)
            if not meta.get("project_id") and meta.get("project_dir"):
                if project_lookup is None:
                    project_lookup = lc.build_project_lookup()
                pid = lc.project_id_for(meta, project_lookup=project_lookup)
            else:
                pid = lc.project_id_for(meta)
            project = projects.get(pid)
            items.append(
                {
                    **to_session_info(meta),
                    "session_id": sid,
                    "project_id": pid,
                    "archived": True,
                    "archived_at": self.archive_time(sid, directory, meta),
                    "pinned": False,
                    "pin_order": 0,
                    "project_name": project.name if project else None,
                    **lc.projection("session", sid, project_id=pid),
                }
            )
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
        if session_id == f"cron_{cron_id}":
            return True
        return session_id.startswith("cron_") and session_id.endswith(f"_{cron_id}")

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
            if not is_default_project_id(
                project_id
            ) and not project_store.get_project_by_id(project_id, cache_bust=True):
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
                    if item.active and not (
                        meta.get("cron_id") or sid.startswith(("cron_", "heartbeat_"))
                    ):
                        ids.append(sid)
                elif not item.active:
                    ids.append(sid)
            results = []
            pins_reindex_required = False
            for sid in sorted(set(ids)):
                try:
                    result = await self._session(
                        sid,
                        "archive" if action == "archive" else "delete",
                        channel_id,
                        project_id,
                        defer_pin_reindex=action == "archive",
                        preloaded_meta=inventory_by_id[sid].metadata,
                    )
                    session_pin_reindex_required = bool(
                        result.pop("_pins_reindex_required", False)
                    )
                    pins_reindex_required = (
                        pins_reindex_required or session_pin_reindex_required
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
            if action == "archive" and pins_reindex_required:
                await asyncio.to_thread(self.reindex_pins)
            succeeded = sum(item["ok"] for item in results)
            return dict(
                project_id=project_id,
                succeeded_count=succeeded,
                failed_count=len(results) - succeeded,
                results=results,
            )

    async def project(
        self, project_id: str, action: str, channel_id: str, params: dict
    ) -> dict:
        lc.validate_id(project_id)
        if action != "delete":
            raise lc.LifecycleError("BAD_REQUEST", "projects only support deletion")
        if is_default_project_id(project_id):
            raise lc.LifecycleError("FORBIDDEN", "default project cannot be deleted")
        async with self.lock("project", project_id):
            project = project_store.get_project_by_id(project_id, cache_bust=True)
            old = lc.state("project", project_id).get("operation", {})
            if not project:
                finalized = (
                    old.get("status") == "completed"
                    or old.get("phase") == "delete_project"
                )
                if old.get("kind") == "delete" and old.get("result") and finalized:
                    if old.get("status") != "completed":
                        lc.complete(
                            "project", project_id, deleted=True, result=old["result"]
                        )
                    return old["result"]
                raise lc.LifecycleError("NOT_FOUND", "project not found")
            # 两阶段契约必须显式声明阶段：无阶段调用会在 begin 时立起
            # 执行栅栏却永不提交，遗留 pending operation 阻塞整个项目。
            stage = params.get("_lifecycle_stage")
            if stage not in {"prepare", "finish"}:
                raise lc.LifecycleError(
                    "BAD_REQUEST", "project deletion requires an explicit lifecycle stage"
                )
            operation = lc.begin("project", project_id, "delete")
            operation = lc.claim_operation("project", project_id, self._owner_id)
            if stage == "prepare":
                return dict(
                    project_id=project_id,
                    operation_id=operation["operation_id"],
                    generation=operation["generation"],
                )
            if (
                params.get("operation_id") != operation["operation_id"]
                or params.get("generation") != operation["generation"]
            ):
                raise lc.LifecycleError(
                    "OPERATION_IN_PROGRESS", "stale project lifecycle generation"
                )
            completed: dict = {}
            done_ids: list[str] = []
            try:
                inventory = await asyncio.to_thread(
                    self.project_session_inventory, project_id
                )
                inventory_by_id = {item.session_id: item for item in inventory}
                ids = list(
                    dict.fromkeys(
                        [
                            *operation.get("session_ids", []),
                            *inventory_by_id,
                        ]
                    )
                )
                conversation_ids = list(operation.get("conversation_session_ids", []))
                for sid in ids:
                    item = inventory_by_id.get(sid)
                    if item is None:
                        # A retry can contain an item saved by a previous
                        # operation snapshot. Re-check it individually rather
                        # than assuming the current inventory still has it.
                        active, archived = lc.session_paths(sid)
                        if not (active.exists() or archived.exists()):
                            continue
                        meta = lc.raw_metadata(sid)
                    else:
                        meta = item.metadata
                    if not (
                        meta.get("cron_id")
                        or sid.startswith(("cron_", "heartbeat_"))
                    ):
                        conversation_ids.append(sid)
                conversation_ids = list(dict.fromkeys(conversation_ids))
                operation = lc.update(
                    "project",
                    project_id,
                    session_ids=ids,
                    conversation_session_ids=conversation_ids,
                    phase="stop_sessions",
                    status="running",
                )
                lc.update("project", project_id, phase="delete_sessions")
                completed = dict(operation.get("completed_items", {}))
                done_ids = list(completed.get("sessions", []))
                busy_ids: list[str] = []
                # Checkpointing after every deletion rewrites a linearly
                # growing completed_items list (O(N^2) write amplification).
                # Session state files remain the recovery truth, so periodic
                # refreshes plus one forced write per exit path suffice.
                uncheckpointed = 0
                for sid in ids:
                    if sid in done_ids:
                        continue
                    child = lc.state("session", sid)
                    if (
                        child.get("deleted")
                        and child.get("operation", {}).get("status") == "completed"
                    ):
                        done_ids.append(sid)
                        uncheckpointed += 1
                        continue
                    # The project execution lock makes the inventory snapshot
                    # stable; reuse it instead of re-reading metadata per
                    # session.  Snapshot sids already validated by the scan.
                    item = inventory_by_id.get(sid)
                    if item is not None:
                        meta = item.metadata
                        pid = project_id
                        was_active = item.active
                    else:
                        meta = lc.raw_metadata(sid)
                        pid = lc.project_id_for(meta)
                        was_active = lc.session_paths(sid)[0].exists()
                    is_cron = bool(meta.get("cron_id")) or sid.startswith(("cron_", "heartbeat_"))
                    if was_active and not is_cron and self.runtime.is_session_running(sid):
                        busy_ids.append(sid)
                        continue
                    try:
                        # The project cascade already owns the project lock;
                        # go straight to _session with the snapshot's project.
                        await self._session(
                            sid, "delete", channel_id, pid, preloaded_meta=meta
                        )
                    except lc.LifecycleError as exc:
                        if exc.code == "SESSION_BUSY":
                            busy_ids.append(sid)
                            continue
                        raise
                    if sid not in done_ids:
                        done_ids.append(sid)
                    uncheckpointed += 1
                    if uncheckpointed >= _PROJECT_DELETE_CHECKPOINT_EVERY:
                        uncheckpointed = 0
                        completed["sessions"] = done_ids
                        lc.update("project", project_id, completed_items=completed)
                if busy_ids:
                    result = dict(
                        project_id=project_id,
                        deleted=False,
                        deleted_sessions=len(done_ids),
                        deleted_conversation_sessions=len(set(done_ids) & set(conversation_ids)),
                        deleted_cron_jobs=params.get("deleted_cron_jobs", 0),
                        skipped_running_session_ids=busy_ids,
                    )
                    completed["sessions"] = done_ids
                    lc.update("project", project_id, completed_items=completed)
                    lc.complete("project", project_id, result=result)
                    return result
                if await asyncio.to_thread(self.project_sessions, project_id):
                    raise lc.LifecycleError("DELETE_FAILED", "project sessions remain")
                result = dict(
                    project_id=project_id,
                    deleted=True,
                    deleted_sessions=len(done_ids),
                    deleted_conversation_sessions=len(set(done_ids) & set(conversation_ids)),
                    deleted_cron_jobs=params.get("deleted_cron_jobs", 0),
                )
                completed["sessions"] = done_ids
                lc.update(
                    "project",
                    project_id,
                    phase="delete_project",
                    result=result,
                    completed_items=completed,
                )
                project_store.delete_project(project_id)
                lc.complete("project", project_id, deleted=True, result=result)
                return result
            except Exception as exc:
                failure_changes = dict(
                    status="failed",
                    errors=[str(exc)],
                    stop_pending=getattr(exc, "code", "")
                    in {"STOP_TIMEOUT", "STOP_SUBMIT_FAILED"},
                    retryable=getattr(exc, "code", "")
                    not in {"SESSION_ID_CONFLICT", "BAD_REQUEST"},
                )
                if done_ids:
                    # Persist the accurate in-memory progress; recovery also
                    # re-derives it from per-session state files.
                    completed["sessions"] = done_ids
                    failure_changes["completed_items"] = completed
                operation = lc.update("project", project_id, **failure_changes)
                details = dict(
                    operation_id=operation["operation_id"],
                    project_id=project_id,
                    phase=operation["phase"],
                    retryable=operation["retryable"],
                    completed_session_ids=operation.get("completed_items", {}).get(
                        "sessions", []
                    ),
                    completed_conversation_session_ids=list(
                        set(operation.get("completed_items", {}).get("sessions", []))
                        & set(operation.get("conversation_session_ids", []))
                    ),
                    completed_cron_job_ids=params.get("completed_cron_job_ids", []),
                    failed_items=[
                        dict(
                            resource_type="project",
                            resource_id=project_id,
                            code=getattr(exc, "code", "DELETE_FAILED"),
                            error=str(exc),
                        )
                    ],
                    **lc.projection("project", project_id),
                )
                if isinstance(exc, lc.LifecycleError):
                    details.update(exc.details)
                raise lc.LifecycleError(
                    "PARTIAL_PROJECT_DELETE_FAILED", str(exc), details
                ) from exc
