"""Worker-owned reconciliation of abandoned SwarmChat organizations."""

import asyncio
import copy
import logging
import time

from jiuwenswarm.server.runtime import group_service as groups
from jiuwenswarm.server.runtime.session import lifecycle

logger = logging.getLogger(__name__)
_enabled = False
TERMINAL = {"completed", "failed", "cancelled", "interrupted", "timed_out", "stopped"}


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
    from jiuwenswarm.agents.swarm.group_org_bootstrap import (
        organization_member_runs, owner_run_is_active, queue_run_is_active,
    )

    if owner_run_is_active(session_id, plan_id) or queue_run_is_active(session_id, plan_id):
        return True
    for (sid, pid, _), task in organization_member_runs().items():
        if sid == session_id and pid == plan_id and not task.done():
            return True
    return False


async def _pool_for(plan: dict):
    """Use Core's shared database, without launching any team to clean old rows."""
    from openjiuwen.agent_teams.organization.task_pool import OrgTaskManager
    from openjiuwen.agent_teams.spawn.shared_resources import get_shared_db
    from jiuwenswarm.agents.harness.team.config_loader import resolve_team_runtime_database_config

    config = resolve_team_runtime_database_config()
    if config is None:
        raise RuntimeError("No verified organization database for offline cleanup")
    db = get_shared_db(config)
    await db.initialize()
    pool = OrgTaskManager(db=db, organization_id=plan["organization_id"])
    await pool.initialize()
    return pool


async def _runtime_has_binding(runtime, session_id: str, plan: dict, pool) -> bool:
    """A live old binding is evidence of possible recovery, not permission to stop it."""
    from jiuwenswarm.agents.swarm.group_org_bootstrap import (
        bound_organization_id, instance_team_id, live_team_backend,
    )

    teams = {instance_team_id(session_id, name) for name in plan.get("members", [])}
    teams.update(row["team_id"] for row in groups.group_organization_teams(session_id, plan["organization_id"]))
    summary = await pool.get_summary_team()
    if summary is not None and summary.summary_team_id:
        teams.add(summary.summary_team_id)
    for team_id in teams:
        backend = await live_team_backend(runtime, team_id, session_id)
        if backend is not None and bound_organization_id(backend) == plan["organization_id"]:
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

    group = groups.read_group(session_id)
    if not group or group.get("status") == "deleting":
        return
    if not group.get("organization_plans"):
        return
    runtime, _, _ = boot.load_organization_runtime()
    # Same lock as instance binding, plus the cross-process group write lock.
    async with boot.session_organization_lock(session_id):
        async with groups.group_lock(session_id):
            for saved in groups.group_organization_plans(session_id):
                plan_id = saved["plan_id"]
                if saved.get("cleanup_status") == "done" or _active(session_id, plan_id):
                    continue
                store = GroupExecutions(session_id)
                records = store.read()
                cards = []
                for record in records.values():
                    if record.get("execution_kind") != "organization":
                        continue
                    if record.get("organization_plan_id") != plan_id:
                        continue
                    cards.append(record)
                owner = saved.get("run_owner")
                if not owner:
                    for record in cards:
                        if record.get("owner"):
                            owner = record.get("owner")
                            break
                if not owner:
                    continue
                try:
                    if lifecycle.runtime_owner_alive(owner):
                        # A terminal run in this Worker may have finished its
                        # cleanup only partially. Other live Workers are opaque.
                        if (saved.get("status") in {"pending", "running"}
                                or owner != lifecycle.current_runtime_owner()):
                            continue
                    pool = await _pool_for(saved)
                    if await _runtime_has_binding(runtime, session_id, saved, pool):
                        continue
                except Exception:
                    logger.warning(
                        "Organization recovery ownership unavailable session=%s plan=%s",
                        session_id, plan_id, exc_info=True,
                    )
                    continue
                # Re-read under the binding lock. A recovered generation must
                # never be fenced by an earlier snapshot.
                current = boot.organization_plan_record(session_id, plan_id)
                if current != saved or _active(session_id, plan_id):
                    continue
                reason = "服务重启或执行进程退出，组织任务已中断，请重新发起。"
                interrupted = saved.get("status") in {"pending", "running"} or saved.get("runtime_interrupted")
                fields = dict(cleanup_status="pending", cleanup_error=None)
                if interrupted:
                    fields.update(status="failed", runtime_interrupted=True, error=reason)
                groups.update_group_organization_plan(session_id, plan_id, **fields)
                changed = []
                for card in cards:
                    if card.get("status") == "completed":
                        continue
                    status = "interrupted" if interrupted else "failed"
                    if close_card(card, status, reason if interrupted else None):
                        changed.append(copy.deepcopy(card))
                store.save(records)
                # State is durable before cleanup or notification. A crash at
                # any subsequent point leaves a retryable cleanup marker.
                try:
                    await asyncio.wait_for(pool.dissolve_organization(), 15)
                    groups.clear_group_organization_binding(session_id, saved["organization_id"])
                    groups.update_group_organization_plan(
                        session_id, plan_id, cleanup_status="done", cleanup_error=None,
                    )
                except Exception as exc:
                    groups.update_group_organization_plan(session_id, plan_id,
                                                          cleanup_status="pending", cleanup_error=str(exc)[:500])
                    logger.warning(
                        "Organization cleanup pending session=%s plan=%s",
                        session_id, plan_id, exc_info=True,
                    )
                boot.organization_plan_event(session_id, plan_id).set()
                for record in changed:
                    try:
                        await store.publish(record, "updated")
                    except Exception:
                        logger.warning("Organization recovery publication failed session=%s", session_id, exc_info=True)
        # Release deferred requests after leaving the group write lock.
        for plan in groups.group_organization_plans(session_id):
            if plan.get("cleanup_status") == "done":
                boot.release_deferred_direct(session_id, plan["plan_id"])


async def recovery_loop(stop_event: asyncio.Event) -> None:
    """Startup scan and bounded periodic retries, owned by WorkerRuntime."""
    global _enabled
    _enabled = True
    try:
        while not stop_event.is_set():
            from jiuwenswarm.common.utils import get_agent_sessions_dir

            for path in get_agent_sessions_dir().glob("*/group.json"):
                if stop_event.is_set():
                    return
                try:
                    await reconcile_group(path.parent.name)
                except Exception:
                    logger.warning("Organization recovery deferred session=%s", path.parent.name, exc_info=True)
                await asyncio.sleep(0)
            try:
                await asyncio.wait_for(stop_event.wait(), 30)
            except asyncio.TimeoutError:
                pass
    finally:
        _enabled = False
