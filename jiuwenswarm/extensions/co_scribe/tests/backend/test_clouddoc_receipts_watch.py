"""The ledger-file watcher: one broadcast per observed change, none on baseline."""
from __future__ import annotations

import asyncio
import os

import pytest

from jiuwenswarm.extensions.co_scribe.backend.host.panel.receipts_watch import watch_receipts_file


@pytest.mark.asyncio
async def test_a_change_broadcasts_once_and_the_baseline_does_not(tmp_path):
    path = tmp_path / "r.json"
    path.write_text("{}")
    events: list[dict] = []

    async def broadcast(event, payload):
        assert event == "clouddoc.receipts_changed"
        events.append(payload)

    task = asyncio.create_task(
        watch_receipts_file(broadcast, interval_s=0.01, path=path, max_ticks=4)
    )
    await asyncio.sleep(0.025)
    assert events == []  # the first observation is the baseline
    os.utime(path, (1, 2_000_000_000))
    await task
    assert len(events) == 1


@pytest.mark.asyncio
async def test_a_missing_file_is_quiet_and_a_failed_broadcast_does_not_kill_the_watch(tmp_path):
    path = tmp_path / "absent.json"
    calls = {"n": 0}

    async def broadcast(event, payload):
        calls["n"] += 1
        raise RuntimeError("socket gone")

    task = asyncio.create_task(
        watch_receipts_file(broadcast, interval_s=0.01, path=path, max_ticks=6)
    )
    await asyncio.sleep(0.02)
    path.write_text("{}")          # appears: baseline, no broadcast
    await asyncio.sleep(0.02)
    os.utime(path, (1, 2_000_000_000))  # changes: one broadcast, which raises
    await task                      # the watch still ran to its tick budget
    assert calls["n"] == 1
