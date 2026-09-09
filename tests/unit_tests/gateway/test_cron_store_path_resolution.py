"""``get_cron_jobs_path`` must follow wherever a workspace keeps its jobs.

``_migrate_legacy_workspace`` relocates the file to ``gateway/`` while the getter
pointed at ``agent/home/``, so after a migration the scheduler read a missing
path and every schedule stopped firing silently. These pin the resolution order
that repairs it without stranding anyone.
"""

from __future__ import annotations

import contextlib
import json
import logging

import pytest

from jiuwenswarm.common.utils import get_cron_jobs_path


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path
    )
    return tmp_path


def _write(path, jobs=("job-1",)):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "jobs": [{"id": j} for j in jobs]}),
        encoding="utf-8",
    )
    return path


def test_fresh_workspace_adopts_the_gateway_layout(workspace):
    """Nothing on disk: use the new location, so agent/home -- the marker that
    makes a workspace look 'legacy' -- is never created."""
    assert get_cron_jobs_path() == workspace / "gateway" / "cron_jobs.json"


def test_existing_deployment_keeps_its_legacy_file(workspace):
    """The upgrade case that must not break: repointing unconditionally would
    empty the schedule of a deployment that never migrated."""
    legacy = _write(workspace / "agent" / "home" / "cron_jobs.json")
    assert get_cron_jobs_path() == legacy


def test_migrated_deployment_uses_gateway(workspace):
    """The bug: once the migration relocated the file, the reader must follow."""
    new = _write(workspace / "gateway" / "cron_jobs.json")
    assert get_cron_jobs_path() == new


def test_gateway_wins_when_both_exist(workspace):
    """The migration can leave the old file behind; the relocated copy wins."""
    _write(workspace / "agent" / "home" / "cron_jobs.json", jobs=("stale",))
    new = _write(workspace / "gateway" / "cron_jobs.json", jobs=("current",))
    assert get_cron_jobs_path() == new


def test_an_empty_agent_home_does_not_count(workspace):
    """A leftover lock file is not a store, or a workspace whose cron_jobs.json
    was removed would pin itself to the legacy path forever."""
    lock_dir = workspace / "agent" / "home"
    lock_dir.mkdir(parents=True)
    (lock_dir / "cron_jobs.json.lock").write_text("", encoding="utf-8")
    assert get_cron_jobs_path() == workspace / "gateway" / "cron_jobs.json"


_JOB = {
    "id": "job-1",
    "name": "Example job",
    "enabled": True,
    "expired": False,
    "cron_expr": "0 0 8 * * ? *",
    "timezone": "UTC",
    "description": "Example scheduled job.",
    "targets": "web",
    "session_id": "web_session_1",
    "mode": "agent",
}


def _write_job(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "jobs": [_JOB]}), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_the_real_migration_no_longer_orphans_the_store(workspace):
    """Run the actual migration, not an imitation of it.

    A hand-rolled copy+unlink would keep passing if the migration ever changed
    destination or stopped deleting the source -- the very drift that caused this.
    """
    from jiuwenswarm.common.utils import _migrate_legacy_workspace
    from jiuwenswarm.gateway.cron.store import CronJobStore

    legacy = workspace / "agent" / "home" / "cron_jobs.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"version": 1, "jobs": [_JOB]}), encoding="utf-8")

    assert len(await CronJobStore(path=get_cron_jobs_path()).list_jobs()) == 1

    _migrate_legacy_workspace(workspace)

    # The migration did what it always did: moved the file and removed the dir.
    assert not (workspace / "agent" / "home").exists()
    assert (workspace / "gateway" / "cron_jobs.json").exists()

    # ...and the job is still reachable, which is the part that used to fail.
    jobs = await CronJobStore(path=get_cron_jobs_path()).list_jobs()
    assert len(jobs) == 1, "the migration orphaned the store again"
    assert jobs[0].id == "job-1"


@pytest.mark.asyncio
async def test_relocating_the_store_no_longer_loses_jobs(workspace):
    """Reproduces the original failure: relocate, then read. Before the fix the
    store read a missing path and reported zero jobs."""
    from jiuwenswarm.gateway.cron.store import CronJobStore

    legacy = workspace / "agent" / "home" / "cron_jobs.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "job-1",
                        "name": "Example job",
                        "enabled": True,
                        "expired": False,
                        "cron_expr": "0 0 8 * * ? *",
                        "timezone": "UTC",
                        "description": "Example scheduled job.",
                        "targets": "web",
                        "session_id": "web_session_1",
                        "mode": "agent",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert len(await CronJobStore(path=get_cron_jobs_path()).list_jobs()) == 1

    # What the migration does: copy to gateway/, then remove agent/home.
    new = workspace / "gateway" / "cron_jobs.json"
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
    legacy.unlink()

    jobs = await CronJobStore(path=get_cron_jobs_path()).list_jobs()
    assert len(jobs) == 1, "the relocated store must still be found"
    assert jobs[0].id == "job-1"


@contextlib.contextmanager
def _scheduler_logs(level=logging.INFO):
    """Collect what the scheduler logs, from the logger that emits it.

    ``caplog`` attaches to the root logger, and ``setup_logger`` sets
    ``propagate = False`` on ``jiuwenswarm`` when it is imported, so these
    records only reach the root on pytest 9.1+, which also attaches to
    non-propagating loggers. Attaching here holds on every version.
    """
    logger = logging.getLogger("jiuwenswarm.gateway.cron.scheduler")
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collect(level)
    original = logger.level
    logger.setLevel(level)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original)


def _scheduler(store):
    """Minimal scheduler for the logging paths: reload() touches only the store
    and its own bookkeeping, so the client and handler are never called."""
    from jiuwenswarm.gateway.cron.scheduler import CronSchedulerService

    return CronSchedulerService(
        store=store, agent_client=None, message_handler=None
    )


@pytest.mark.asyncio
async def test_reload_reports_how_many_jobs_it_loaded(workspace):
    """Say what was loaded and from where: "scheduler started" alone reads the
    same with one job or none."""
    from jiuwenswarm.gateway.cron.store import CronJobStore

    path = _write_job(workspace / "gateway" / "cron_jobs.json")
    with _scheduler_logs() as records:
        await _scheduler(CronJobStore(path=path)).reload()

    messages = [r.getMessage() for r in records]
    assert any(
        "loaded 1 job(s)" in m and str(path) in m for m in messages
    ), messages


@pytest.mark.asyncio
async def test_reload_warns_when_it_loads_nothing(workspace):
    """Zero jobs is a warning, not silence: it is the symptom of the bug."""
    from jiuwenswarm.gateway.cron.store import CronJobStore

    missing = workspace / "gateway" / "cron_jobs.json"
    with _scheduler_logs(logging.DEBUG) as records:
        await _scheduler(CronJobStore(path=missing)).reload()

    warnings = [r.getMessage() for r in records if r.levelname == "WARNING"]
    assert any("loaded 0 jobs" in m for m in warnings), warnings


@pytest.mark.asyncio
async def test_a_store_vanishing_under_us_warns_by_name(workspace):
    """Losing a populated store is not routine housekeeping: the old INFO line
    read the same whether the file was edited or had vanished with the schedules."""
    from jiuwenswarm.gateway.cron.store import CronJobStore

    path = _write_job(workspace / "gateway" / "cron_jobs.json")
    scheduler = _scheduler(CronJobStore(path=path))
    await scheduler.reload()
    scheduler._sync_store_mtime()

    path.unlink()
    with _scheduler_logs() as records:
        assert await scheduler._check_store_changed() is True

    warnings = [r.getMessage() for r in records if r.levelname == "WARNING"]
    assert any(
        "disappeared while holding 1 job(s)" in m for m in warnings
    ), warnings
