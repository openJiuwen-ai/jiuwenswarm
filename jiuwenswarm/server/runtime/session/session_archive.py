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
        self.runtime = runtime
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._stopping: dict[str, asyncio.Task] = {}
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
                    lc.renew_operation(kind, resource_id, self._owner_id, release=True)
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
                    if not operation or operation.get("status") == "completed":
                        continue
                    if operation.get("kind") == "archive":
                        # 归档不设执行栅栏，卡死的 operation 只挡住反向操作；
                        # 重放搬目录等于替用户做决定，按目录实际位置收尾即可。
                        await asyncio.to_thread(
                            self._finalize_abandoned_archive, operation
                        )
                        continue
                    if not operation.get("retryable", True):
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

    def _finalize_abandoned_archive(self, operation: dict) -> None:
        """Finalize an abandoned archive operation by the directory's position.

        A cancelled or crashed archive leaves a pending operation that
        ``begin`` rejects with a kind mismatch, deadlocking the opposite
        action.  The move is never replayed here — that would relocate the
        session on the user's behalf — the operation is closed out to match
        where the directory actually sits.  Runs in a worker thread: the
        cross-process locks must never block the recovery loop.
        """
        sid = operation.get("resource_id") or ""
        if not sid or operation.get("status") == "completed":
            return
        lease = float(operation.get("lease_expires_at") or 0)
        if not operation.get("owner_id") or lease >= time.time():
            # Cheap pre-filter on the scan snapshot; a snapshot alone must
            # never decide.  A manual retry reuses this operation_id and
            # refreshes the lease, so the authoritative recheck below runs
            # under the execution-owner lock against the current state.
            return
        # Order matches a live execution (service.lock -> lc.begin/complete):
        # execution-owner lock first, resource lock second.  Holding the
        # owner lock also means no retry can sit between begin and claim.
        owner_path = (
            lc.resource_path("session", sid).parent.parent
            / "owners"
            / f"session_{sid}.json"
        )
        with lc.file_lock(owner_path):
            with lc.resource_lock("session", sid):
                value = lc.state("session", sid)
                current = value.get("operation") or {}
                if (
                    current.get("operation_id") != operation.get("operation_id")
                    or current.get("status") == "completed"
                ):
                    return
                if (
                    not current.get("owner_id")
                    or float(current.get("lease_expires_at") or 0) >= time.time()
                ):
                    # The lease is alive NOW (a retry re-claimed after the
                    # scan): never finalize out from under a live execution.
                    return
                if lc.session_paths(sid)[1].exists():
                    if current.get("pin_reindex_required"):
                        # The archive intent persisted this requirement before
                        # the move so retries retain it; finalization is the
                        # last repair window — after completion a manual
                        # archive short-circuits on "already archived" and
                        # never reindexes.  reindex only touches sessions in
                        # the active area, never this archived one, so the
                        # per-session locks it takes cannot nest with ours.
                        try:
                            self.reindex_pins()
                        except Exception:
                            lc.update(
                                "session",
                                sid,
                                status="failed",
                                errors=["pin reindex failed during recovery"],
                                retryable=True,
                            )
                            return
                    lc.complete(
                        "session",
                        sid,
                        archived=True,
                        result=dict(
                            session_id=sid,
                            ok=True,
                            archived=True,
                            archived_at=current.get("archived_at") or time.time(),
                            stop_pending=False,
                            project_id=current.get("project_id", ""),
                        ),
                    )
                else:
                    # Still in the active area (or gone): the move never happened;
                    # keep the session where it is and simply clear the operation.
                    lc.complete(
                        "session",
                        sid,
                        archived=False,
                        result=dict(
                            session_id=sid,
                            ok=True,
                            archived=False,
                            aborted=True,
                            project_id=current.get("project_id", ""),
                        ),
                    )

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
            session_id, action, channel_id, project_id,
            pre_stopped=pre_stopped,
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
            # persistent leader stream keeps them alive.  Archive leaves that
            # stream alone; delete proceeds and its stop path tears the team
            # runtime (and with it the stream) down.
            return False
        # is_cron_session 不再豁免 busy 检查：running 的 cron/heartbeat session
        # 同样必须先停止才能 delete，避免僵尸流永久锁定会话。
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
            if action in {"archive", "delete"} and self.runtime.is_session_running(
                session_id
            ):
                probe = getattr(self.runtime, "has_parked_team_streams", None)
                parked_team_streams = callable(probe) and bool(probe(session_id))
            if self._session_is_busy_for_action(
                session_id,
                action,
                active,
                is_cron_session,
                parked_team_streams=parked_team_streams,
            ):
                details = {"stop_pending": False}
                message = f"Session is running; stop it before {action}"
                probe = getattr(self.runtime, "is_team_round_finishing", None)
                if callable(probe) and probe(session_id):
                    # swarmflow.stop/自然完成后 leader 仍在收尾汇报：会话正在
                    # 自行结束，引导稍后重试，而不是让用户先停止一个无活可停的
                    # 会话。finishing 标记供前端区分两种 busy 文案。
                    details["finishing"] = True
                    message = "swarm flow 已结束，会话回合收尾中，请稍后重试"
                raise lc.LifecycleError("SESSION_BUSY", message, details)
            operation = lc.begin(
                "session", session_id, action, block_execution=action != "archive"
            )
            operation = lc.claim_operation("session", session_id, self._owner_id)
            # 置顶会话跨归档边界后活跃区序号必然失真（归档留空档、取消归档带
            # 回旧序号），重排需求在搬目录前写入状态：重排失败时重试（含新服务
            # 实例接管的重试）仍能读到它。
            pin_reindex_required = action in {"archive", "unarchive"} and (
                pin_reindex_required or bool(operation.get("pin_reindex_required"))
            )
            lc.update(
                "session", session_id, project_id=project_id,
                pin_reindex_required=pin_reindex_required,
            )
            mailbox = self._session_message_service() if action == "delete" else None
            try:
                if mailbox is not None:
                    # 删除屏障先于 stop 生效：阻止信箱新执行并取消目标消费者。
                    # 必须在 try 内建立：取消发生在这个 await 上时，下面的
                    # except 分支才有机会恢复信箱消费。
                    await mailbox.begin_target_delete(session_id)
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
                if action == "unarchive":
                    # 项目被移除时,归档区是它唯一还能被看到的内容。先把项目
                    # 恢复出来,否则会话离开归档页后在工作区里也找不到。
                    await asyncio.to_thread(
                        self._restore_hidden_project, project_id
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
                deferred_pin_reindex = action == "archive" and defer_pin_reindex
                if not deferred_pin_reindex and pin_reindex_required:
                    if action == "unarchive":
                        # 刚搬回活跃区的会话仍在执行栅栏内，reindex_pins 默认跳过
                        # blocked 会话，这里显式让它写回这一条的序号。
                        await asyncio.to_thread(
                            self.reindex_pins, include_blocked=session_id
                        )
                    else:
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
                    # 项目名冲突只能由用户先重命名占用方再重试,自动重放会一直
                    # 撞同一堵墙,因此不标记 retryable;用户手动重试仍然有效。
                    retryable=getattr(exc, "code", "")
                    not in {
                        "SESSION_ID_CONFLICT",
                        "BAD_REQUEST",
                        "PROJECT_NAME_CONFLICT",
                    },
                )
                self._poke_recovery()
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
            except BaseException:
                # 取消（WS 断开/服务关停）不走 except Exception：CancelledError
                # 继承 BaseException。栅栏必须立刻标记 failed/retryable 并尽力
                # 恢复信箱删除屏障，否则会以 running 状态遗留，挡住该会话的
                # 所有后续请求，只能等 10s 轮询兜底。
                lc.update(
                    "session",
                    session_id,
                    status="failed",
                    errors=["operation cancelled"],
                    retryable=True,
                )
                if mailbox is not None:
                    try:
                        await mailbox.abort_target_delete(session_id)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "session.cancel failed to resume mailbox consumer: "
                            "session_id=%s",
                            session_id,
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
