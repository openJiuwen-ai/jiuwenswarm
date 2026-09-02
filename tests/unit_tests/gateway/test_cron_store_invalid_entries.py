"""A cron job that fails to parse must be reported, not silently removed.

``list_jobs`` is the single read path for the scheduler, the web panel and the
TUI, and ``get_job`` walks it. An entry ``CronJob.from_dict`` rejects therefore
stops firing, vanishes from both UIs and cannot be repaired through either of
them, and nothing anywhere reports it: the read logs no count, so a store of
nine entries serves eight with no trace at any level.

These pin the warning that makes the loss findable, and the existing property
that keeps the rejected entry on disk so it stays repairable by hand.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path

import pytest

from jiuwenswarm.gateway.cron.store import CronJobStore

_STORE_LOGGER = "jiuwenswarm.gateway.cron.store"


@contextmanager
def _store_logs(level: int = logging.DEBUG):
    """Collect records from the store logger itself, not through ``caplog``.

    ``setup_logger`` sets ``propagate = False`` on the ``jiuwenswarm`` logger at
    import time, so records never reach the root logger that ``caplog`` attaches
    its handler to; asserting through ``caplog`` would see nothing on the pytest
    version this project pins. Attaching to the emitting logger depends on
    neither propagation nor the pytest version.
    """
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(_STORE_LOGGER)
    handler = _Collect()
    handler.setLevel(level)
    previous_level = logger.level
    logger.setLevel(level)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _job(job_id: str, **overrides) -> dict:
    """A raw entry that parses. ``work_mode`` is set so the lazy migration in
    ``list_jobs`` short-circuits and the test never consults real project state.
    """
    base = {
        "id": job_id,
        "name": "nightly digest",
        "enabled": True,
        "cron_expr": "0 9 * * *",
        "timezone": "Asia/Shanghai",
        "description": "post the nightly digest",
        "targets": "web",
        "mode": "agent",
        "project_id": "",
        "work_mode": "work",
        "wake_offset_seconds": 300,
    }
    base.update(overrides)
    return base


def _write(path: Path, jobs: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "jobs": jobs}, ensure_ascii=False), encoding="utf-8"
    )
    return path


def _read(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["jobs"]


@pytest.mark.asyncio
async def test_rejected_entry_warns_and_names_the_job(tmp_path):
    """The bug: two entries on disk, one returned, nothing logged even at DEBUG.
    The id is what makes the offending line findable in the file."""
    path = _write(
        tmp_path / "cron_jobs.json",
        [_job("job-ok"), _job("job-broken", cron_expr="every night at nine")],
    )

    with _store_logs() as records:
        jobs = await CronJobStore(path=path).list_jobs()

    assert [j.id for j in jobs] == ["job-ok"]
    warnings = [r.getMessage() for r in records if r.levelname == "WARNING"]
    assert any(
        "job-broken" in message and "cron_expr" in message for message in warnings
    ), warnings


@pytest.mark.asyncio
async def test_entry_rejected_for_its_id_is_still_reported(tmp_path):
    """A missing id is itself a rejection reason, so the warning cannot rely on
    having one; it must still say a job was dropped."""
    path = _write(tmp_path / "cron_jobs.json", [_job("job-ok"), _job("")])

    with _store_logs() as records:
        jobs = await CronJobStore(path=path).list_jobs()

    assert [j.id for j in jobs] == ["job-ok"]
    assert any(
        r.levelname == "WARNING" and "<missing>" in r.getMessage() for r in records
    ), [r.getMessage() for r in records]


@pytest.mark.asyncio
async def test_a_rejected_entry_does_not_fail_the_whole_read(tmp_path):
    """Reporting the loss must not turn one bad entry into a dead scheduler."""
    path = _write(
        tmp_path / "cron_jobs.json",
        [_job("job-a"), _job("job-bad", description=""), _job("job-b")],
    )

    jobs = await CronJobStore(path=path).list_jobs()

    assert sorted(j.id for j in jobs) == ["job-a", "job-b"]


@pytest.mark.asyncio
async def test_a_rejected_entry_survives_a_write_to_another_job(tmp_path):
    """The mitigating property to keep: no write path rebuilds the file from the
    parsed list, so an entry the reader dropped is still there to be fixed by
    hand. Losing this would turn a warning into permanent data loss."""
    path = _write(
        tmp_path / "cron_jobs.json",
        [_job("job-ok"), _job("job-broken", cron_expr="every night at nine")],
    )
    store = CronJobStore(path=path)

    assert await store.delete_job("job-ok") is True

    remaining = _read(path)
    assert [item["id"] for item in remaining] == ["job-broken"]
    assert remaining[0]["cron_expr"] == "every night at nine"
