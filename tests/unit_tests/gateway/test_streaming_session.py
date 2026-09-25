"""Unit tests for the platform-agnostic streaming session."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.gateway.channel_manager.im_platforms.platform_adapter.streaming_session import (
    StreamingSession,
    merge_streamed_and_final,
)


class _FakeSurface:
    def __init__(self) -> None:
        self.opened_with = ""
        self.writes: list[tuple[str, str, int]] = []
        self.closed: tuple[str, str, int] | None = None

    async def open(self, text: str) -> str:
        self.opened_with = text
        return "handle-1"

    async def write(self, handle: str, text: str, sequence: int) -> None:
        self.writes.append((handle, text, sequence))

    async def close(self, handle: str, text: str, sequence: int) -> None:
        self.closed = (handle, text, sequence)


@pytest.mark.asyncio
async def test_start_seeds_the_snapshot_with_the_opening_text() -> None:
    # A surface that can be created with content must not have to wait a whole
    # debounce window to show it.
    surface = _FakeSurface()
    session = StreamingSession(surface, debounce_ms=0)

    await session.start("hello")

    assert surface.opened_with == "hello"
    assert session.rendered_text == "hello"
    assert surface.writes == []


@pytest.mark.asyncio
async def test_writes_are_debounced_and_numbered_monotonically() -> None:
    surface = _FakeSurface()
    session = StreamingSession(surface, debounce_ms=0)
    await session.start()

    session.replace("a")
    session.replace("ab")
    await asyncio.sleep(0.05)
    session.replace("abc")
    await session.finalize()

    # The two replaces inside one window collapse into the latest snapshot.
    assert [text for _, text, _ in surface.writes] == ["ab", "abc"]
    sequences = [sequence for _, _, sequence in surface.writes]
    assert sequences == sorted(sequences)
    assert surface.closed is not None
    assert surface.closed[1] == "abc"
    assert surface.closed[2] > sequences[-1]


@pytest.mark.asyncio
async def test_announce_failure_closes_the_surface_and_propagates() -> None:
    surface = _FakeSurface()

    async def announce(_handle: str) -> None:
        raise RuntimeError("cannot show it")

    session = StreamingSession(surface, announce=announce, debounce_ms=0)

    with pytest.raises(RuntimeError, match="cannot show it"):
        await session.start()

    # Left open, the surface would be a card or message nobody can ever see.
    assert surface.closed is not None
    assert surface.closed[0] == "handle-1"


@pytest.mark.asyncio
async def test_stop_waits_for_the_in_flight_write_and_writes_nothing_itself() -> None:
    """The caller's own closing write must not be clobbered by a late flush."""
    release = asyncio.Event()
    write_started = asyncio.Event()

    class _SlowSurface(_FakeSurface):
        async def write(self, handle: str, text: str, sequence: int) -> None:
            write_started.set()
            await release.wait()
            await super().write(handle, text, sequence)

    surface = _SlowSurface()
    session = StreamingSession(surface, debounce_ms=0)
    await session.start()
    session.replace("partial")
    await write_started.wait()

    stopping = asyncio.create_task(session.stop())
    await asyncio.sleep(0)
    assert not stopping.done()

    release.set()
    handle = await stopping

    assert handle == "handle-1"
    assert [text for _, text, _ in surface.writes] == ["partial"]
    assert surface.closed is None


@pytest.mark.asyncio
async def test_stop_ignores_a_failed_intermediate_write() -> None:
    class _FailingSurface(_FakeSurface):
        async def write(self, handle: str, text: str, sequence: int) -> None:
            raise RuntimeError("edit rejected")

    session = StreamingSession(_FailingSurface(), debounce_ms=0)
    await session.start()
    session.replace("partial")
    await asyncio.sleep(0.01)

    assert await session.stop() == "handle-1"


@pytest.mark.parametrize(
    ("streamed", "final", "expected"),
    [
        ("hello wor", "hello world", "hello world"),
        ("hello world", "", "hello world"),
        ("hello world", "   ", "hello world"),
        ("", "hello world", "hello world"),
        ("the whole answer", "answer", "the whole answer"),
    ],
)
def test_merge_streamed_and_final_keeps_the_fuller_side(
    streamed: str, final: str, expected: str
) -> None:
    assert merge_streamed_and_final(streamed, final) == expected
