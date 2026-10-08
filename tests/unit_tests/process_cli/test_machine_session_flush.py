"""One-shot Session shutdown waits for both independent persistence queues."""

from __future__ import annotations

import asyncio
import threading

import pytest

from jiuwenswarm.channels.process_cli import machine
from jiuwenswarm.server.runtime.session import session_history, session_metadata


@pytest.mark.asyncio
async def test_history_and_metadata_flush_start_within_one_cleanup_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    both_started = threading.Barrier(2, timeout=3)
    timeouts: list[float] = []

    def flush(timeout: float) -> bool:
        timeouts.append(timeout)
        both_started.wait()
        return True

    monkeypatch.setattr(session_history, "flush_pending_writes", flush)
    monkeypatch.setattr(session_metadata, "flush_pending_writes", flush)

    await asyncio.wait_for(machine._flush_session_writes(), timeout=4)

    assert timeouts == [machine.SHUTDOWN_TIMEOUT_SECONDS - 0.5] * 2


@pytest.mark.asyncio
async def test_short_cleanup_deadline_gives_writers_positive_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[float] = []

    def flush(timeout: float) -> bool:
        timeouts.append(timeout)
        return timeout > 0

    monkeypatch.setattr(machine, "SHUTDOWN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(session_history, "flush_pending_writes", flush)
    monkeypatch.setattr(session_metadata, "flush_pending_writes", flush)

    await machine._flush_session_writes()

    assert timeouts == pytest.approx([0.045, 0.045])


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_queue", ["history", "metadata"])
async def test_unfinished_queue_fails_the_session_write_step(
    monkeypatch: pytest.MonkeyPatch, failed_queue: str
) -> None:
    monkeypatch.setattr(
        session_history,
        "flush_pending_writes",
        lambda _timeout: failed_queue != "history",
    )
    monkeypatch.setattr(
        session_metadata,
        "flush_pending_writes",
        lambda _timeout: failed_queue != "metadata",
    )

    with pytest.raises(RuntimeError, match=failed_queue):
        await machine._flush_session_writes()
