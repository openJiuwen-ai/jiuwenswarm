"""Worker-owned reconciliation of abandoned SwarmChat organizations."""

import asyncio
import copy
import logging
import time
from collections.abc import Callable
from functools import partial
from typing import TypeVar

from jiuwenswarm.server.runtime import group_service as groups
from jiuwenswarm.server.runtime.session import lifecycle

logger = logging.getLogger(__name__)
_enabled = False
_T = TypeVar("_T")
TERMINAL = {"completed", "failed", "cancelled", "interrupted", "timed_out", "stopped"}


async def _file_io(operation: Callable[[], _T]) -> _T:
    """Finish file operations before releasing their caller's ownership locks."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    cancellation = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancellation = exc
        except Exception:
            if cancellation is not None:
                raise cancellation
            raise
    if cancellation is not None:
        raise cancellation
    return result


def _scan_sessions() -> list[str]:
    """Consume the lazy glob in the file worker, including exists/stat calls."""
    from jiuwenswarm.common.utils import get_agent_sessions_dir

    return [path.parent.name for path in get_agent_sessions_dir().glob("*/group.json")]


def close_card(record: dict, status: str, reason: str | None = None) -> bool:
    """Close unfinished rows without discarding completed work or artifacts."""
    before = copy.deepcopy(record)
    record.update(status=status, phase=status, organization_stage=None)
    if reason:
        record.update(error_code="ORGANIZATION_RUNTIME_LOST", error=reason)
    for task_id, value in record.get("tasks", {}).items():
        if value not in TERMINAL:
            record["tasks"][task_id] = status
    for detail in record.get("task_details", {}).values():
        if detail.get("status") not in TERMINAL:
            detail["status"] = status
        for child in detail.get("expert_tasks", []):
            if child.get("status") not in TERMINAL:
                child["status"] = status
    tasks = record.get("tasks", {})
    record["progress"] = {"completed": sum(v == "completed" for v in tasks.values()), "total": len(tasks)}
    if record == before:
        return False
    record["revision"] = record.get("revision", 0) + 1
    record["updated_at"] = time.time()
    return True


def _active(session_id: str, plan_id: str) -> bool:
    from jiuwenswarm.agents.swarm import group_org_bootstrap as boot

    return (boot._owner_run_active(session_id, plan_id) or boot._queue_run_active(session_id, plan_id)
            or any(sid == session_id and pid == plan_id and not task.done()
                   for (sid, pid, _), task in boot._member_runs.items()))


async def _pool_for(plan: dict):
    """Use Core's shared database, without launching any team to clean old rows."""
    from openjiuwen.agent_teams.organization.task_pool import OrgTaskManager
    from openjiuwen.agent_teams.spawn.shared_resources import get_shared_db
    from openjiuwen.agent_teams.tools.database.config import DatabaseConfig
    from jiuwenswarm.agents.harness.team.config_loader import resolve_team_sqlite_db_path

    path = await _file_io(resolve_team_sqlite_db_path)
    if path is None:
        raise RuntimeError("No verified organization database for offline cleanup")
    db = get_shared_db(DatabaseConfig(connection_string=str(path)))
    await db.initialize()
    pool = OrgTaskManager(db=db, organization_id=plan["organization_id"])
    await pool.initialize()
    return pool


async def _runtime_has_binding(runtime, session_id: str, plan: dict, pool) -> bool:
    """A live old binding is evidence of possible recovery, not permission to stop it."""
    from jiuwenswarm.agents.swarm import group_org_bootstrap as boot

    teams = {boot.instance_team_id(session_id, name) for name in plan.get("members", [])}
    bindings = await _file_io(partial(groups.group_organization_teams, session_id, plan["organization_id"]))
    teams.update(row["team_id"] for row in bindings)
    summary = await pool.get_summary_team()
    if summary is not None and summary.summary_team_id:
        teams.add(summary.summary_team_id)
    for team_id in teams:
        backend = await boot._live_backend(runtime, team_id, session_id)
        if backend is not None and boot._bound_organization(backend) == plan["organization_id"]:
            return True
        # A Summary Team belongs only to this organization; do not delete its
        # backing rows while it is loaded, even if it lacks an org manager.
        if summary is not None and team_id == summary.summary_team_id:
            entry = await runtime.pool.get(team_id)
            if entry is not None and entry.current_session_id == session_id:
                return True
    return False


async def reconcile_group(session_id: str) -> None:
    """Run only inside the owning Worker; uncertain ownership never authorizes cleanup."""
    if not _enabled:
        return
    from jiuwenswarm.agents.swarm import group_org_bootstrap as boot
    from jiuwenswarm.server.runtime.group_execution import GroupExecutions

    group = await _file_io(partial(groups.read_group, session_id))
    if not group or group.get("status") == "deleting":
        return
    if not group.get("organization_plans"):
        return
    runtime, _, _ = boot._organization_runtime()
    # Same lock as instance binding, plus the cross-process group write lock.
    async with boot._lock_for(session_id):
        async with groups.group_lock(session_id):
            plans = await _file_io(partial(groups.group_organization_plans, session_id))
            for saved in plans:
                plan_id = saved["plan_id"]
                if saved.get("cleanup_status") == "done" or _active(session_id, plan_id):
                    continue
                store = GroupExecutions(session_id)
                records = await _file_io(store.read)
                cards = [r for r in records.values() if r.get("execution_kind") == "organization"
                         and r.get("organization_plan_id") == plan_id]
                owner = saved.get("run_owner") or next((r.get("owner") for r in cards if r.get("owner")), None)
                if not owner:
                    continue
                try:
                    if await _file_io(partial(lifecycle.runtime_owner_alive, owner)):
                        # A terminal run in this Worker may have finished its
                        # cleanup only partially. Other live Workers are opaque.
                        if (saved.get("status") in {"pending", "running"}
                                or owner != await _file_io(lifecycle.current_runtime_owner)):
                            continue
                    pool = await _pool_for(saved)
                    if await _runtime_has_binding(runtime, session_id, saved, pool):
                        continue
                except Exception:
                    logger.warning("Organization recovery ownership unavailable session=%s plan=%s", session_id, plan_id,
                                   exc_info=True)
                    continue
                # Re-read under the binding lock. A recovered generation must
                # never be fenced by an earlier snapshot.
                current = await _file_io(partial(boot._plan_record, session_id, plan_id))
                if current != saved or _active(session_id, plan_id):
                    continue
                reason = "服务重启或执行进程退出，组织任务已中断，请重新发起。"
                interrupted = saved.get("status") in {"pending", "running"} or saved.get("runtime_interrupted")
                fields = dict(cleanup_status="pending", cleanup_error=None)
                if interrupted:
                    fields.update(status="failed", runtime_interrupted=True, error=reason)
                await _file_io(partial(groups.update_group_organization_plan, session_id, plan_id, **fields))
                changed = []
                for card in cards:
                    if card.get("status") == "completed":
                        continue
                    status = "interrupted" if interrupted else "failed"
                    if close_card(card, status, reason if interrupted else None):
                        changed.append(copy.deepcopy(card))
                await _file_io(partial(store.save, records))
                # State is durable before cleanup or notification. A crash at
                # any subsequent point leaves a retryable cleanup marker.
                try:
                    await asyncio.wait_for(pool.dissolve_organization(), 15)
                    await _file_io(partial(groups.clear_group_organization_binding, session_id, saved["organization_id"]))
                    await _file_io(partial(groups.update_group_organization_plan, session_id, plan_id,
                                           cleanup_status="done", cleanup_error=None))
                except Exception as exc:
                    await _file_io(partial(groups.update_group_organization_plan, session_id, plan_id,
                                           cleanup_status="pending", cleanup_error=str(exc)[:500]))
                    logger.warning("Organization cleanup pending session=%s plan=%s", session_id, plan_id, exc_info=True)
                boot._plan_event(session_id, plan_id).set()
                for record in changed:
                    try:
                        await store.publish(record, "updated")
                    except Exception:
                        logger.warning("Organization recovery publication failed session=%s", session_id, exc_info=True)
        # Check and persist expiration under the same cross-process lock as writers.
        expired = []
        async with groups.group_lock(session_id):
            plans = await _file_io(partial(groups.group_organization_plans, session_id))
            if (any(plan.get("cleanup_status") == "done" for plan in plans)
                    and any(key[0] == session_id and not job.get("started")
                            for key, job in boot._deferred_direct.items())):
                for plan in plans:
                    plan_id = str(plan.get("plan_id") or "")
                    if (plan.get("status") not in {"pending", "running"}
                            or _active(session_id, plan_id)
                            or boot._now_ms() - int(plan.get("created_at") or 0)
                            <= int(boot.PLAN_STALE_SECONDS * 1000)):
                        continue
                    owner = plan.get("run_owner")
                    if not owner:
                        continue
                    try:
                        alive = await _file_io(partial(lifecycle.runtime_owner_alive, owner))
                        if alive and owner != await _file_io(lifecycle.current_runtime_owner):
                            continue
                        current = await _file_io(partial(boot._plan_record, session_id, plan_id))
                    except Exception:
                        logger.warning("Organization expiration ownership unavailable session=%s plan=%s",
                                       session_id, plan_id, exc_info=True)
                        continue
                    if current != plan or _active(session_id, plan_id):
                        continue
                    await _file_io(partial(groups.update_group_organization_plan, session_id,
                                           plan_id, status="expired"))
                    expired.append(plan_id)
                    boot._plan_event(session_id, plan_id).set()
                if expired:
                    plans = await _file_io(partial(groups.group_organization_plans, session_id))
        # Schedule direct replies only after releasing the cross-process lock.
        for plan in plans:
            if plan.get("cleanup_status") == "done" or plan["plan_id"] in expired:
                boot.release_deferred_direct(session_id, plan["plan_id"], plans=plans)


async def recovery_loop(stop_event: asyncio.Event) -> None:
    """Startup scan and bounded periodic retries, owned by WorkerRuntime."""
    global _enabled
    _enabled = True
    try:
        while not stop_event.is_set():
            try:
                sessions = await asyncio.to_thread(_scan_sessions)
            except OSError:
                logger.warning("Organization recovery scan deferred", exc_info=True)
                sessions = []
            for session_id in sessions:
                if stop_event.is_set():
                    return
                try:
                    await reconcile_group(session_id)
                except Exception:
                    logger.warning("Organization recovery deferred session=%s", session_id, exc_info=True)
                await asyncio.sleep(0)
            try:
                await asyncio.wait_for(stop_event.wait(), 30)
            except asyncio.TimeoutError:
                pass
    finally:
        _enabled = False
