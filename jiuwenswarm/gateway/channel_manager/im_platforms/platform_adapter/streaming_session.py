# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Accumulate streamed model output onto one editable surface.

Every IM platform that can show a reply while it is still being generated works
the same way: create one thing, edit it repeatedly, close it once. Only the API
calls differ -- a Feishu CardKit card, a Slack message edited with
``chat.update``, a bubble replaced over a websocket.

What is *not* platform specific is the part that is easy to get wrong: keeping a
snapshot rather than appending fragments, debouncing so a token-per-frame model
does not become a request-per-token, numbering writes monotonically so a
reordered update cannot resurrect stale text, and making sure the closing write
observes everything that arrived while an earlier write was in flight.

That logic lives here once. A platform supplies a :class:`StreamingSurface`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol


class StreamingSurface(Protocol):
    """One editable thing on an IM platform: a card, a message, a bubble.

    ``handle`` is whatever the platform uses to address it again -- a card id, a
    message timestamp. The session treats it as opaque.
    """

    async def open(self, text: str) -> str:
        """Create the surface and return its handle.

        Platforms that cannot create a surface with content ignore ``text``; the
        session writes it on the first flush instead.
        """

    async def write(self, handle: str, text: str, sequence: int) -> None:
        """Replace the surface's contents with ``text``.

        ``sequence`` increases by one per write for the whole session, for
        platforms that reject out-of-order updates.
        """

    async def close(self, handle: str, text: str, sequence: int) -> None:
        """Mark the surface finished. ``text`` is the final contents."""


class StreamingSession:
    """Accumulate model output and keep exactly one surface up to date."""

    def __init__(
        self,
        surface: StreamingSurface,
        *,
        announce: Callable[[str], Awaitable[None]] | None = None,
        debounce_ms: int = 150,
    ) -> None:
        """``announce`` is called once with the handle after the surface opens.

        It exists for platforms where creating the surface and putting it in
        front of the user are two different API calls; if it fails the surface is
        closed again rather than left dangling, and the failure propagates.
        """
        self._surface = surface
        self._announce = announce
        self._debounce_ms = debounce_ms
        self._handle = ""
        self._text = ""
        self._sequence = 0
        self._closed = False
        self._closing = False
        self._flush_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

    @property
    def handle(self) -> str:
        return self._handle

    @property
    def rendered_text(self) -> str:
        return self._text

    @property
    def is_active(self) -> bool:
        return bool(self._handle) and not self._closed and not self._closing

    async def start(self, initial_text: str = "") -> None:
        self._handle = await self._surface.open(initial_text)
        self._text = initial_text
        if self._announce is None:
            return
        try:
            await self._announce(self._handle)
        except Exception:
            try:
                await self._surface.close(self._handle, "", self._next_sequence())
            except Exception:  # noqa: BLE001
                # Rolling back is best effort: the caller needs the reason the
                # surface never reached the user, not the reason cleaning up
                # after it also failed.
                pass
            raise

    def replace(self, text: str) -> None:
        """Set the current snapshot. Synchronous: the write happens later."""
        if self.is_active:
            self._text = text
            self._schedule_flush()

    async def finalize(self, final_text: str = "") -> str:
        """Write the last snapshot, close the surface, and return its text."""
        if self._closed:
            return self._text
        self._closing = True
        if final_text:
            self._text = merge_streamed_and_final(self._text, final_text)
        if self._flush_task is not None:
            await asyncio.shield(self._flush_task)
        try:
            await self._write_snapshot(self._text)
            await self._surface.close(
                self._handle,
                self._text,
                self._next_sequence(),
            )
            return self._text
        finally:
            self._closed = True

    async def stop(self) -> str:
        """Stop streaming and hand the handle back without writing or closing.

        For platforms where the closing write is the delivery itself and has to
        be made by the caller -- because it carries the caller's error handling,
        or because the final text does not fit the surface and has to be split
        across several. Waits for an in-flight write so a late snapshot cannot
        land on top of whatever the caller writes next.
        """
        if self._closed:
            return self._handle
        self._closing = True
        if self._flush_task is not None:
            try:
                await asyncio.shield(self._flush_task)
            except Exception:  # noqa: BLE001
                # A failed intermediate write is not the caller's problem: it is
                # about to overwrite the surface anyway.
                pass
        self._closed = True
        return self._handle

    def _schedule_flush(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_after_delay())

    async def _flush_after_delay(self) -> None:
        await asyncio.sleep(self._debounce_ms / 1000)
        if self.is_active:
            snapshot = self._text
            await self._write_snapshot(snapshot)
            # Text that arrived while that write was in flight would otherwise
            # sit unsent until the next delta, and the last one never comes.
            if self.is_active and snapshot != self._text:
                self._flush_task = None
                self._schedule_flush()

    async def _write_snapshot(self, text: str) -> None:
        async with self._write_lock:
            await self._surface.write(
                self._handle,
                text,
                self._next_sequence(),
            )

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence


def merge_streamed_and_final(streamed: str, final: str) -> str:
    """Pick whichever of the two carries more of the answer.

    The terminal event usually repeats the whole reply, but not always: some
    runtimes send an empty one, some a summary shorter than what streamed.
    Preferring the longer side keeps the surface from losing text it already
    showed the user.
    """
    if not final.strip():
        return streamed
    if not streamed.strip() or final.startswith(streamed):
        return final
    if streamed.startswith(final):
        return streamed
    return final if len(final) >= len(streamed) else streamed
