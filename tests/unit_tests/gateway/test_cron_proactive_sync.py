"""Proactive cron registration must not fan out admission queries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.gateway.cron.proactive_cron_sync import PROACTIVE_JOB_ID, _find_job


@pytest.mark.asyncio
async def test_find_job_reads_store_without_admission_gate() -> None:
    store_job = SimpleNamespace(
        id=PROACTIVE_JOB_ID, to_dict=lambda: {"id": PROACTIVE_JOB_ID}
    )
    controller = SimpleNamespace(
        get_job=AsyncMock(return_value=None),
        list_jobs=AsyncMock(side_effect=AssertionError("must not gate")),
        store=SimpleNamespace(list_jobs=AsyncMock(return_value=[store_job])),
    )

    assert await _find_job(controller) == {"id": PROACTIVE_JOB_ID}
    controller.list_jobs.assert_not_awaited()


@pytest.mark.asyncio
async def test_find_job_without_store_listing_returns_none() -> None:
    controller = SimpleNamespace(get_job=AsyncMock(return_value=None))

    assert await _find_job(controller) is None
