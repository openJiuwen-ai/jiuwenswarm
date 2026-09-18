# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Generic streaming responder (E1 connector SDK).

Harvested from ``feishu_streaming_card.py`` (``FeishuStreamingSession``):
accumulate model output and edit one already-sent message at a bounded
frequency (debounce), so platform APIs never rate-limit the bot. The
debounce/flush scheduling mirrors the proven Feishu implementation
(``_schedule_flush`` / ``_flush_after_delay``).

Capability-aware degradation: on channels whose
``capabilities.streaming`` is False, nothing is sent until
:meth:`finalize`, which delivers one complete message — instead of a
dropped or API-hammering reply.
"""

import asyncio
from typing import Awaitable, Callable

DEFAULT_DEBOUNCE_MS = 150  # same default as the harvested Feishu session

SendFn = Callable[[str], Awaitable[str]]
EditFn = Callable[[str, str], Awaitable[None]]


class StreamingResponder:
    """Accumulates text and edits a single message at a throttled pace.

    Args:
        send: coroutine ``send(text) -> message_id`` — sends the first message.
        edit: coroutine ``edit(message_id, text)`` — replaces its content.
        supports_streaming: value of ``channel.capabilities.streaming``.
        debounce_ms: minimum milliseconds between two edits.
    """

    def __init__(
        self,
        send: SendFn,
        edit: EditFn,
        supports_streaming: bool,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
    ) -> None:
        self._send = send
        self._edit = edit
        self._streaming = supports_streaming
        self._debounce_ms = debounce_ms
        self._buffer = ""
        self._message_id: str | None = None
        self._flush_task: asyncio.Task | None = None
        self._last_sent: str | None = None
        self._lock = asyncio.Lock()

    @property
    def rendered_text(self) -> str:
        """Text accumulated so far."""
        return self._buffer

    async def append(self, token: str) -> None:
        """Add model output; schedules a throttled flush when streaming."""
        self._buffer += token
        if self._streaming:
            self._schedule_flush()

    async def finalize(self) -> str:
        """Deliver the final, complete text; returns it."""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            self._flush_task = None
        await self._flush()
        return self._buffer

    # ------------------------------------------------------------- internals
    def _schedule_flush(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_after_delay())

    async def _flush_after_delay(self) -> None:
        await asyncio.sleep(self._debounce_ms / 1000)
        await self._flush()

    async def _flush(self) -> None:
        async with self._lock:
            text = self._buffer
            if not text:
                return
            if self._message_id is None:
                self._message_id = await self._send(text)
            elif text != self._last_sent:
                await self._edit(self._message_id, text)
            self._last_sent = text