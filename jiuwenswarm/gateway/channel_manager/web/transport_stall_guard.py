# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Abort server transports whose peer stopped reading (write-stall watchdog).

A client that triggers a server response and then stops reading fills the
transport write buffer; TCP keeps ACKing with a zero window, so the socket
never errors out. ``transport.close()`` then waits for the buffer to drain
forever:

- uvicorn's HTTP protocols have nothing that aborts such a connection — the
  socket sits in CLOSE-WAIT for the life of the process, holding an fd plus
  every buffered byte (issue #4946 measured 1888 of them on the WebChannel
  port), and a stalled transport also pins uvicorn's graceful shutdown;
- the websockets implementation aborts only ~30s after a FIN, and its
  keepalive cannot fire at all once writes stall (``await self.ping()``
  blocks on the same undrained buffer).

``pause_writing`` fires when the write buffer crosses the high-water mark,
and ``resume_writing`` when it drains below the low-water mark. A transport
that stays paused for ``_STALL_ABORT_SECONDS`` is aborted. On peer EOF, an
independent close deadline also covers buffers that never crossed the high
water mark, or resumed with a small undrained remainder. ``connection_lost``
disarms both deadlines once the socket has actually closed.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("jiuwenswarm.gateway.web")

# Bound write stalls and incomplete transport closure after peer EOF.
_STALL_ABORT_SECONDS = 60.0


class TransportStallGuardMixin:
    """Abort transports stuck writing or closing after peer EOF.

    The host class must be an asyncio protocol created with a live event loop
    (uvicorn's HTTP/WS protocols qualify: they set ``self.loop`` and expose
    ``self.transport``). Timer state is per connection instance;
    ``connection_lost`` disarms it.
    """

    _stall_abort_handle: asyncio.TimerHandle | None = None
    _eof_abort_handle: asyncio.TimerHandle | None = None

    def pause_writing(self) -> None:
        super().pause_writing()
        if self._stall_abort_handle is None:
            self._stall_abort_handle = self.loop.call_later(
                _STALL_ABORT_SECONDS, self._abort_stalled_transport
            )

    def resume_writing(self) -> None:
        self._cancel_stall_abort()
        super().resume_writing()

    def eof_received(self) -> bool | None:
        keep_open = super().eof_received()
        # Returning false/None makes asyncio close the transport, but close()
        # still waits for pending writes, even below the flow-control limits.
        if not keep_open and self._eof_abort_handle is None:
            self._eof_abort_handle = self.loop.call_later(
                _STALL_ABORT_SECONDS, self._abort_after_eof
            )
        return keep_open

    def connection_lost(self, exc: Exception | None) -> None:
        self._cancel_stall_abort()
        self._cancel_eof_abort()
        super().connection_lost(exc)

    def _cancel_stall_abort(self) -> None:
        if self._stall_abort_handle is not None:
            self._stall_abort_handle.cancel()
            self._stall_abort_handle = None

    def _abort_stalled_transport(self) -> None:
        self._abort_transport("write buffer stalled without a drain")

    def _cancel_eof_abort(self) -> None:
        if self._eof_abort_handle is not None:
            self._eof_abort_handle.cancel()
            self._eof_abort_handle = None

    def _abort_after_eof(self) -> None:
        self._abort_transport("transport did not close after peer EOF")

    def _abort_transport(self, reason: str) -> None:
        self._cancel_stall_abort()
        self._cancel_eof_abort()
        transport = getattr(self, "transport", None)
        if transport is None:
            return
        peer = getattr(self, "client", None) or getattr(self, "remote_address", None)
        logger.warning(
            "[stall-guard] %s for %.0fs; aborting transport peer=%s",
            reason,
            _STALL_ABORT_SECONDS,
            peer,
        )
        transport.abort()


_guarded_http_class: type | None = None
_guarded_websockets_class: type | None = None


def guarded_http_protocol() -> type:
    """uvicorn HTTP protocol (auto-selected impl) with the stall guard."""
    global _guarded_http_class
    if _guarded_http_class is None:
        from uvicorn.protocols.http.auto import AutoHTTPProtocol

        class GuardedHTTPProtocol(TransportStallGuardMixin, AutoHTTPProtocol):
            pass

        _guarded_http_class = GuardedHTTPProtocol
    return _guarded_http_class


def guarded_websockets_protocol() -> type:
    """uvicorn WebSocket protocol (websockets impl) with the stall guard."""
    global _guarded_websockets_class
    if _guarded_websockets_class is None:
        from uvicorn.protocols.websockets.websockets_impl import WebSocketProtocol

        class GuardedWebSocketProtocol(TransportStallGuardMixin, WebSocketProtocol):
            pass

        _guarded_websockets_class = GuardedWebSocketProtocol
    return _guarded_websockets_class
