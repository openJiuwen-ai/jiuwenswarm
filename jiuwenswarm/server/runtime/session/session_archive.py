"""Archive operations owned by the AgentServer runtime, never Gateway fallback."""

from __future__ import annotations

import asyncio
import os
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from jiuwenswarm.common.work_mode import is_default_project_id
from jiuwenswarm.server.runtime.session import lifecycle as lc, project_store
from jiuwenswarm.server.runtime.session.session_info import to_session_info

logger = logging.getLogger(__name__)


def get_agent_sessions_dir():
    return lc.get_agent_sessions_dir()


class SessionArchiveService:
    def __init__(self, runtime):
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
                        lc.renew_operation(kind, resource_id, self._owner_id)

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
        while True:
            directory = lc.resource_path("session", "_scan").parent
            for path in directory.glob("session_*.json") if directory.exists() else ():
                operation = {}
                try:
                    operation = lc.read_json(path).get("operation") or {}
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
            await asyncio.sleep(1)

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
                return await self._session(session_id, action, channel_id, project_id)
        return await self._session(session_id, action, channel_id, project_id)

    async def _session(
        self, session_id: str, action: str, channel_id: str, project_id: str
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
            meta = lc.raw_metadata(session_id)
            if action != "delete" and (
                meta.get("cron_id") or session_id.startswith(("cron_", "heartbeat_"))
            ):
                raise lc.LifecycleError(
                    "FORBIDDEN",
                    "cron and heartbeat sessions cannot be archived separately",
                )
            parent = project_store.get_project_by_id(project_id, cache_bust=True)
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
                        project_archived=bool(parent and parent.hidden),
                    )
            if action == "archive" and self.runtime.is_session_running(session_id):
                raise lc.LifecycleError(
                    "SESSION_BUSY", "Session is running; finish it before archiving",
                    {"stop_pending": False},
                )
            operation = lc.begin("session", session_id, action, block_execution=action != "archive")
            operation = lc.claim_operation("session", session_id, self._owner_id)
            lc.update("session", session_id, project_id=project_id)
            try:
                if action == "delete":
                    lc.update(
                        "session", session_id, phase="stop_sessions", status="running"
                    )
                    await self.stop(
                        session_id, str(meta.get("channel_id") or channel_id)
                    )
                    lc.fence_writes("session", session_id)
                elif action == "archive":
                    # Flush accepted writes, but do not stop or close the runtime.
                    from jiuwenswarm.server.runtime.session import session_metadata, session_history

                    flushed = await asyncio.gather(
                        asyncio.to_thread(session_metadata.flush_pending_writes, 10),
                        asyncio.to_thread(session_history.flush_pending_writes, 10),
                    )
                    if not all(flushed):
                        raise lc.LifecycleError("ARCHIVE_FAILED", "Session writes are still pending")
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
                    payload = dict(session_id=session_id, ok=True, project_id=project_id)
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
                                # Keep the cross-process lock while retrying; no await under it.
                                import time as time_module

                                time_module.sleep(0.05 * (attempt + 1))
                    meta = lc.read_json(destination / "metadata.json")
                    if action == "archive":
                        meta.update(
                            archived=True,
                            archived_at=operation["archived_at"],
                            pinned=False,
                            pin_order=0,
                        )
                    else:
                        meta.pop("archived", None)
                        meta.pop("archived_at", None)
                    lc.atomic_json(destination / "metadata.json", meta)
                from jiuwenswarm.server.runtime.session.session_metadata import (
                    remove_session_metadata_cache,
                )

                remove_session_metadata_cache(session_id)
                if action == "archive":
                    self.reindex_pins()
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
                        project_archived=bool(parent and parent.hidden),
                    )
                )
                lc.complete(
                    "session", session_id, archived=action == "archive", result=payload
                )
                return payload
            except Exception as exc:
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
        items = []
        for directory in root.iterdir() if root.exists() else ():
            if not directory.is_dir():
                continue
            sid = directory.name
            lc.resolve_session(sid)
            meta = lc.raw_metadata(sid)
            pid = lc.project_id_for(meta)
            project = project_store.get_project_by_id(pid, cache_bust=True)
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
                    "project_archived": bool(project and project.hidden),
                    **lc.projection("session", sid, project_id=pid),
                }
            )
        return lc.page(items, params, "sessions", 200)

    @staticmethod
    def list_projects(params: dict) -> dict:
        project_fields = (
            "project_id",
            "name",
            "project_dir",
            "work_mode",
            "hidden",
            "archived_at",
            "created_at",
            "updated_at",
        )
        items = []
        for p in project_store.list_projects(include_hidden=True, cache_bust=True):
            if not p.hidden:
                continue
            project_data = p.to_dict()
            item = {k: project_data[k] for k in project_fields}
            item.update(lc.projection("project", p.project_id))
            items.append(item)
        return lc.page(items, params, "projects", 100)

    @staticmethod
    def project_sessions(project_id: str) -> list[str]:
        active = get_agent_sessions_dir()
        result = []
        for root in (active, active.parent / "sessions_archived"):
            for path in root.iterdir() if root.exists() else ():
                if (
                    path.is_dir()
                    and lc.project_id_for(lc.raw_metadata(path.name)) == project_id
                ):
                    result.append(path.name)
        return result

    async def project(
        self, project_id: str, action: str, channel_id: str, params: dict
    ) -> dict:
        lc.validate_id(project_id)
        if is_default_project_id(project_id):
            raise lc.LifecycleError(
                "FORBIDDEN", "default project cannot be archived or deleted"
            )
        async with self.lock("project", project_id):
            project = project_store.get_project_by_id(project_id, cache_bust=True)
            old = lc.state("project", project_id).get("operation", {})
            if not project:
                if (
                    action == "delete"
                    and old.get("status") == "completed"
                    and old.get("kind") == "delete"
                ):
                    return old["result"]
                if (
                    action == "delete"
                    and old.get("phase") == "delete_project"
                    and old.get("result")
                ):
                    lc.complete(
                        "project", project_id, deleted=True, result=old["result"]
                    )
                    return old["result"]
                raise lc.LifecycleError("NOT_FOUND", "project not found")
            if action == "delete" and not project.hidden:
                raise lc.LifecycleError(
                    "CONFLICT", "archive project before permanent deletion"
                )
            cleanup_pending = (
                action == "unarchive"
                and bool(old)
                and old.get("status") != "completed"
                and old.get("kind") != action
            )
            if cleanup_pending:
                raise lc.LifecycleError(
                    "OPERATION_IN_PROGRESS", "archive cleanup is pending"
                )
            if action == "unarchive" and not project.hidden:
                if old and old.get("kind") == "unarchive" and old.get("status") != "completed":
                    # Crash between restore_project() and the lifecycle commit:
                    # the project is visible again but its execution fence is
                    # still up. Finish the interrupted operation before
                    # reporting idempotency, otherwise the fence never lifts.
                    ids = self.project_sessions(project_id)
                    lc.complete(
                        "project",
                        project_id,
                        result=dict(
                            project_id=project_id,
                            restored=True,
                            work_mode=project.work_mode,
                            affected_sessions=sum(
                                lc.session_paths(sid)[0].exists() for sid in ids
                            ),
                        ),
                    )
                return dict(
                    project_id=project_id,
                    restored=False,
                    work_mode=project.work_mode,
                    affected_sessions=0,
                )
            # Archive is a point-in-time check, not an admission barrier. Only
            # the initial request checks; Gateway finish does not stop new work.
            if action == "archive" and params.get("_lifecycle_stage") != "finish":
                busy = [sid for sid in self.project_sessions(project_id)
                        if self.runtime.is_session_running(sid)]
                if busy:
                    raise lc.LifecycleError(
                        "PROJECT_BUSY", "Project has running sessions; finish them before archiving",
                        {"project_id": project_id, "running_session_ids": busy, "stop_pending": False},
                    )
            operation = lc.begin("project", project_id, action, block_execution=action != "archive")
            operation = lc.claim_operation("project", project_id, self._owner_id)
            # Gateway owns cron: prepare persists the operation (and, only for
            # deletion, an execution fence). finish requires the same token.
            stage = params.get(
                "_lifecycle_stage", "finish" if action == "unarchive" else "prepare"
            )
            if stage == "prepare":
                return dict(
                    project_id=project_id,
                    operation_id=operation["operation_id"],
                    generation=operation["generation"],
                )
            if action != "unarchive" and (
                params.get("operation_id") != operation["operation_id"]
                or params.get("generation") != operation["generation"]
            ):
                raise lc.LifecycleError(
                    "OPERATION_IN_PROGRESS", "stale project lifecycle generation"
                )
            try:
                ids = self.project_sessions(project_id)
                if action == "delete":
                    # Save the input set before deleting any child, so an absent
                    # directory on retry still runs the child's pending cleanup.
                    ids = list(dict.fromkeys([*operation.get("session_ids", []), *ids]))
                    operation = lc.update("project", project_id, session_ids=ids)
                active_count = sum(lc.session_paths(sid)[0].exists() for sid in ids)
                if action == "unarchive":
                    project_store.restore_project(project_id)
                    result = dict(
                        project_id=project_id,
                        restored=True,
                        work_mode=project.work_mode,
                        affected_sessions=active_count,
                    )
                else:
                    lc.update("project", project_id, phase="stop_sessions" if action == "delete" else "update_project")
                    semaphore = asyncio.Semaphore(8)

                    async def stop_one(sid):
                        async with semaphore:
                            await self.stop(
                                sid,
                                str(
                                    lc.raw_metadata(sid).get("channel_id") or channel_id
                                ),
                            )

                    outcomes = await asyncio.gather(
                        *(stop_one(sid) for sid in ids), return_exceptions=True
                    ) if action == "delete" else []
                    failures = [
                        (sid, outcome)
                        for sid, outcome in zip(ids, outcomes)
                        if isinstance(outcome, BaseException)
                    ]
                    if failures:
                        raise lc.LifecycleError(
                            "STOP_TIMEOUT",
                            "; ".join(f"{sid}: {exc}" for sid, exc in failures),
                            {
                                "failed_items": [
                                    dict(
                                        resource_type="session",
                                        resource_id=sid,
                                        code=getattr(exc, "code", "STOP_SUBMIT_FAILED"),
                                        error=str(exc),
                                    )
                                    for sid, exc in failures
                                ],
                                "stop_pending": any(
                                    getattr(exc, "code", "") == "STOP_TIMEOUT"
                                    for _, exc in failures
                                ),
                            },
                        )
                    lc.fence_writes("project", project_id)
                    if action == "archive":
                        project_store.hide_project(
                            project_id, archived_at=operation["archived_at"]
                        )
                        project_store.reindex_project_pin_orders()
                        result = dict(
                            project_id=project_id,
                            archived=True,
                            hidden=True,
                            archived_at=project.archived_at
                            if project.hidden
                            else operation["archived_at"],
                            affected_sessions=0 if project.hidden else active_count,
                            stopped_cron_jobs=params.get("stopped_cron_jobs", 0),
                            stop_pending=False,
                        )
                    else:
                        lc.update("project", project_id, phase="delete_sessions")
                        completed = dict(operation.get("completed_items", {}))
                        done_ids = list(completed.get("sessions", []))
                        for sid in ids:
                            if sid in done_ids:
                                continue
                            child = lc.state("session", sid)
                            if (
                                child.get("deleted")
                                and child.get("operation", {}).get("status")
                                == "completed"
                            ):
                                done_ids.append(sid)
                                completed["sessions"] = done_ids
                                lc.update(
                                    "project", project_id, completed_items=completed
                                )
                                continue
                            await self.session(
                                sid,
                                "delete",
                                channel_id,
                                parent_operation=operation["operation_id"],
                            )
                            if sid not in done_ids:
                                done_ids.append(sid)
                            completed["sessions"] = done_ids
                            lc.update("project", project_id, completed_items=completed)
                        if self.project_sessions(project_id):
                            raise lc.LifecycleError(
                                "DELETE_FAILED", "project sessions remain"
                            )
                        result = dict(
                            project_id=project_id,
                            deleted=True,
                            deleted_sessions=len(done_ids),
                            deleted_cron_jobs=params.get("deleted_cron_jobs", 0),
                        )
                        lc.update(
                            "project", project_id, phase="delete_project", result=result
                        )
                        project_store.delete_project(project_id)
                lc.complete(
                    "project",
                    project_id,
                    archived=action == "archive",
                    deleted=action == "delete",
                    result=result,
                )
                return result
            except Exception as exc:
                operation = lc.update(
                    "project",
                    project_id,
                    status="failed",
                    errors=[str(exc)],
                    stop_pending=isinstance(exc, lc.LifecycleError)
                    and exc.code == "STOP_TIMEOUT",
                    retryable=getattr(exc, "code", "")
                    not in {"SESSION_ID_CONFLICT", "BAD_REQUEST"},
                )
                code = (
                    (
                        "CONFLICT"
                        if isinstance(exc, project_store.ProjectNameConflict)
                        else "RESTORE_FAILED"
                    )
                    if action == "unarchive"
                    else "PARTIAL_PROJECT_DELETE_FAILED"
                    if action == "delete"
                    else "PARTIAL_PROJECT_ARCHIVE_FAILED"
                )
                details = dict(
                    operation_id=operation["operation_id"],
                    project_id=project_id,
                    phase=operation["phase"],
                    retryable=operation["retryable"],
                    completed_session_ids=operation.get("completed_items", {}).get(
                        "sessions", []
                    ),
                    completed_cron_job_ids=params.get("completed_cron_job_ids", []),
                    failed_items=[
                        dict(
                            resource_type="project",
                            resource_id=project_id,
                            code=getattr(exc, "code", code),
                            error=str(exc),
                        )
                    ],
                    **lc.projection("project", project_id),
                )
                if isinstance(exc, lc.LifecycleError):
                    details.update(exc.details)
                raise lc.LifecycleError(code, str(exc), details) from exc
