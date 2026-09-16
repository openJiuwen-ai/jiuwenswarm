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

    async def _session(
        self,
        session_id: str,
        action: str,
        channel_id: str,
        project_id: str,
        *,
        pre_stopped: bool = False,
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
            if action == "archive" and self.runtime.is_session_running(session_id):
                raise lc.LifecycleError(
                    "SESSION_BUSY",
                    "Session is running; finish it before archiving",
                    {"stop_pending": False},
                )
            operation = lc.begin(
                "session", session_id, action, block_execution=action != "archive"
            )
            operation = lc.claim_operation("session", session_id, self._owner_id)
            lc.update("session", session_id, project_id=project_id)
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
                    )
                )
                lc.complete(
                    "session", session_id, archived=action == "archive", result=payload
                )
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
                    **lc.projection("session", sid, project_id=pid),
                }
            )
        return lc.page(items, params, "sessions", 200)

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
            ids = []
            for sid in self.project_sessions(project_id):
                active, archived = lc.session_paths(sid)
                meta = lc.raw_metadata(sid)
                if action == "archive":
                    if active.exists() and not (
                        meta.get("cron_id") or sid.startswith(("cron_", "heartbeat_"))
                    ):
                        ids.append(sid)
                elif archived.exists():
                    ids.append(sid)
            results = []
            for sid in sorted(set(ids)):
                try:
                    results.append(
                        await self._session(
                            sid,
                            "archive" if action == "archive" else "delete",
                            channel_id,
                            project_id,
                        )
                    )
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
            try:
                ids = list(
                    dict.fromkeys(
                        [
                            *operation.get("session_ids", []),
                            *self.project_sessions(project_id),
                        ]
                    )
                )
                operation = lc.update(
                    "project",
                    project_id,
                    session_ids=ids,
                    phase="stop_sessions",
                    status="running",
                )
                # Stop every producer before deleting any child data.
                stopped_ids: set[str] = set()
                for sid in ids:
                    if lc.state("session", sid).get("deleted"):
                        continue
                    if not lc.session_paths(sid)[0].exists():
                        # 归档区会话没有运行时生产者，无需停止。
                        continue
                    await self.stop(
                        sid,
                        str(lc.raw_metadata(sid).get("channel_id") or channel_id),
                    )
                    stopped_ids.add(sid)
                lc.fence_writes("project", project_id)
                lc.update("project", project_id, phase="delete_sessions")
                completed = dict(operation.get("completed_items", {}))
                done_ids = list(completed.get("sessions", []))
                for sid in ids:
                    if sid in done_ids:
                        continue
                    child = lc.state("session", sid)
                    if (
                        child.get("deleted")
                        and child.get("operation", {}).get("status") == "completed"
                    ):
                        done_ids.append(sid)
                        completed["sessions"] = done_ids
                        lc.update("project", project_id, completed_items=completed)
                        continue
                    await self.session(
                        sid,
                        "delete",
                        channel_id,
                        parent_operation=operation["operation_id"],
                        pre_stopped=sid in stopped_ids,
                    )
                    if sid not in done_ids:
                        done_ids.append(sid)
                    completed["sessions"] = done_ids
                    lc.update("project", project_id, completed_items=completed)
                if self.project_sessions(project_id):
                    raise lc.LifecycleError("DELETE_FAILED", "project sessions remain")
                result = dict(
                    project_id=project_id,
                    deleted=True,
                    deleted_sessions=len(done_ids),
                    deleted_cron_jobs=params.get("deleted_cron_jobs", 0),
                )
                lc.update("project", project_id, phase="delete_project", result=result)
                project_store.delete_project(project_id)
                lc.complete("project", project_id, deleted=True, result=result)
                return result
            except Exception as exc:
                operation = lc.update(
                    "project",
                    project_id,
                    status="failed",
                    errors=[str(exc)],
                    stop_pending=getattr(exc, "code", "")
                    in {"STOP_TIMEOUT", "STOP_SUBMIT_FAILED"},
                    retryable=getattr(exc, "code", "")
                    not in {"SESSION_ID_CONFLICT", "BAD_REQUEST"},
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
