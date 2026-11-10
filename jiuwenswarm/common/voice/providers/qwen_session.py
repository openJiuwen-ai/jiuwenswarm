"""A caller-owned Qwen connection with serialized access to its event stream.

This layer owns transport lifecycle and JSON events only. Consumers supply
session configuration, input, response policy, and output processing.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import json

from . import qwen_omni


class QwenSessionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class QwenEventChannel:
    """One event reader, used exclusively by the current session transaction."""

    def __init__(self, socket, api_key: str = ""):
        self._socket = socket
        self._events = socket.__aiter__()
        self._owner = asyncio.current_task()
        self._api_key = api_key

    def _check_owner(self) -> None:
        if self._owner is None or self._owner is not asyncio.current_task():
            raise QwenSessionError("SESSION_BUSY", "Use Qwen events only inside the owning transaction")

    async def send(self, event: dict) -> None:
        self._check_owner()
        try:
            await self._socket.send(json.dumps(event, ensure_ascii=False))
        except Exception as exc:
            raise QwenSessionError("PROVIDER_ERROR", qwen_omni._safe_upstream_error(exc, self._api_key)) from None

    async def receive(self) -> dict:
        self._check_owner()
        try:
            raw = await self._events.__anext__()
        except StopAsyncIteration:
            raise QwenSessionError("INCOMPLETE_RESPONSE", "Qwen connection ended before the operation completed") from None
        except Exception as exc:
            raise QwenSessionError("PROVIDER_ERROR", qwen_omni._safe_upstream_error(exc, self._api_key)) from None
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            raise QwenSessionError("INVALID_RESPONSE", "Qwen returned invalid JSON") from None
        if not isinstance(event, dict):
            raise QwenSessionError("INVALID_RESPONSE", "Qwen returned an invalid event")
        if event.get("type") in {"error", "session.closed"}:
            raise QwenSessionError("PROVIDER_ERROR", "Qwen rejected the operation or closed the session")
        return event


class QwenOmniSession:
    """Keep one connection until close or failure; never replay a failed turn.

    ``transaction`` serializes writers and the sole reader together. An error or
    cancellation discards the connection before another transaction can start,
    so a late response cannot become the next caller's output. Consumers must
    bound their transactions with a timeout and call ``aclose`` on teardown.
    """

    def __init__(self, config: qwen_omni.QwenOmniRealtimeConfig):
        config.validate()
        self.config = config
        self.generation = 0
        self._lock = asyncio.Lock()
        self._stack: AsyncExitStack | None = None
        self._channel: QwenEventChannel | None = None
        self._owner: asyncio.Task | None = None
        self._closed = False
        self._closing: set[asyncio.Task] = set()

    async def _disconnect(self) -> None:
        stack, self._stack = self._stack, None
        if self._channel is not None:
            self._channel._owner = None
        self._channel = None
        if stack is not None:
            task = asyncio.create_task(stack.aclose())
            self._closing.add(task)

            def finished(done):
                self._closing.discard(done)
                if not done.cancelled():
                    done.exception()  # Retrieve failures even if our waiter was cancelled.

            task.add_done_callback(finished)
            try:
                await asyncio.shield(task)
            except Exception as exc:
                raise QwenSessionError("PROVIDER_ERROR", qwen_omni._safe_upstream_error(exc, self.config.api_key)) from None

    async def _drain_closing(self) -> None:
        if self._closing:
            results = await asyncio.gather(*tuple(self._closing), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise QwenSessionError("PROVIDER_ERROR", qwen_omni._safe_upstream_error(result, self.config.api_key)) from None

    @asynccontextmanager
    async def transaction(self):
        async with self._lock:
            if self._closed:
                raise QwenSessionError("SESSION_CLOSED", "Qwen session has been closed")
            self._owner = asyncio.current_task()
            try:
                if self._channel is None:
                    await self._drain_closing()
                    self._stack = AsyncExitStack()
                    try:
                        socket = await self._stack.enter_async_context(qwen_omni.open_qwen_omni_connection(self.config))
                    except Exception as exc:
                        raise QwenSessionError("PROVIDER_ERROR", qwen_omni._safe_upstream_error(exc, self.config.api_key)) from None
                    self._channel = QwenEventChannel(socket, self.config.api_key)
                    event = await self._channel.receive()
                    if event.get("type") != "session.created":
                        raise QwenSessionError("INVALID_RESPONSE", "Qwen did not create a session")
                    self.generation += 1
                self._channel._owner = asyncio.current_task()
                yield self._channel
            except BaseException:
                await self._disconnect()
                raise
            finally:
                if self._channel is not None:
                    self._channel._owner = None
                self._owner = None

    async def aclose(self) -> None:
        if self._owner is asyncio.current_task():
            raise QwenSessionError("SESSION_BUSY", "Close the Qwen session outside its active transaction")
        self._closed = True
        owner = self._owner
        if owner is not None and owner is not asyncio.current_task():
            owner.cancel()
        async with self._lock:
            await self._disconnect()
        await self._drain_closing()
