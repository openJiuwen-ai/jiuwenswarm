"""Bounded async history flushing for model-error recovery."""

import asyncio
import threading

import pytest

from jiuwenswarm.server.runtime.session import session_history


@pytest.mark.asyncio
async def test_async_flush_times_out_without_blocking_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    worker_entered = threading.Event()
    ticks: list[int] = []

    def blocked_flush() -> None:
        worker_entered.set()
        release.wait(2)

    monkeypatch.setattr(session_history, "flush_history_writes", blocked_flush)

    async def ticker() -> None:
        for index in range(5):
            ticks.append(index)
            await asyncio.sleep(0.01)

    ticker_task = asyncio.create_task(ticker())
    try:
        result = await session_history.flush_history_writes_async(timeout=0.05)
        assert await asyncio.to_thread(worker_entered.wait, 1)
        assert result is False
        assert ticks
    finally:
        release.set()
        await ticker_task


@pytest.mark.asyncio
async def test_rewind_fallback_skips_sync_read_when_flush_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interface_deep = pytest.importorskip(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep"
    )

    async def timed_out() -> bool:
        return False

    def unexpected_read(_session_id: str):
        raise AssertionError("history must not be read before the writer flush completes")

    monkeypatch.setattr(interface_deep, "flush_history_writes_async", timed_out)
    monkeypatch.setattr(interface_deep, "load_history_records", unexpected_read)

    adapter = object.__new__(interface_deep.JiuWenSwarmDeepAdapter)
    await adapter._rewind_history_fallback("session-1", "stream_error", object())
