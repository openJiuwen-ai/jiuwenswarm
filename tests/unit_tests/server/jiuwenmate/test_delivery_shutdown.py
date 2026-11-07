"""A completed scan must not keep a stopped delivery worker alive."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("scan_method", ["dispatch_pending", "reconcile_welink_deliveries"])
async def test_close_finishes_when_scan_completion_races_cancellation(tmp_path, monkeypatch, scan_method):
    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    service = ResultDeliveryService(
        repository, namespace_id="test", send_delivery=AsyncMock(), poll_interval=30,
    )
    loop = asyncio.get_running_loop()
    shutdown_requested = loop.create_future()

    def request_shutdown():
        shutdown_requested.set_result(asyncio.create_task(service.aclose()))

    async def complete_scan():
        # Schedule shutdown before wait_for observes its completed inner task.
        # Python 3.11 can consume that cancellation while returning the result.
        loop.call_soon(request_shutdown)
        return 0

    async def scan():
        return await asyncio.wait_for(complete_scan(), timeout=5)

    monkeypatch.setattr(service, "dispatch_pending", AsyncMock(return_value=0))
    monkeypatch.setattr(service, "reconcile_welink_deliveries", AsyncMock())
    monkeypatch.setattr(service, scan_method, scan)
    await service.start()
    workers = (service._task, service._welink_task)
    shutdown = None
    try:
        shutdown = await asyncio.wait_for(shutdown_requested, timeout=5)
        done, _pending = await asyncio.wait({shutdown}, timeout=1)
        assert shutdown in done, "stopped workers must not enter another polling wait"
        await shutdown
        assert all(worker.done() for worker in workers)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if shutdown is not None:
            await asyncio.gather(shutdown, return_exceptions=True)
        else:
            await service.aclose()
