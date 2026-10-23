# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Write stalls and pending closure after peer EOF have bounded lifetimes."""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.gateway.channel_manager.web import transport_stall_guard as tsg


class _FakeTimer:
    def __init__(self, delay: float, callback, timers: list) -> None:
        self.delay = delay
        self.callback = callback
        self.cancelled = False
        timers.append(self)

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        assert not self.cancelled, "a disarmed timer must never fire"
        self.callback()


class _FakeLoop:
    def __init__(self) -> None:
        self.timers: list[_FakeTimer] = []

    def call_later(self, delay: float, callback) -> _FakeTimer:
        return _FakeTimer(delay, callback, self.timers)


class _BaseProtocol:
    """Minimal asyncio protocol the mixin can be composed onto."""

    def __init__(self) -> None:
        self.loop = _FakeLoop()
        self.paused = False
        self.aborted = False
        self.eof = False
        self.keep_open_on_eof = None
        self.transport = SimpleNamespace(abort=self._abort)

    def _abort(self) -> None:
        self.aborted = True

    def pause_writing(self) -> None:
        self.paused = True

    def resume_writing(self) -> None:
        self.paused = False

    def eof_received(self):
        self.eof = True
        return self.keep_open_on_eof

    def connection_lost(self, exc) -> None:
        self.exc = exc


class _GuardedProtocol(tsg.TransportStallGuardMixin, _BaseProtocol):
    pass


def _protocol() -> _GuardedProtocol:
    proto = _GuardedProtocol()
    assert proto.loop.timers == []
    return proto


def test_pause_arms_the_abort_timer_with_the_configured_delay():
    proto = _protocol()

    proto.pause_writing()

    assert proto.paused
    assert len(proto.loop.timers) == 1
    assert proto.loop.timers[0].delay == tsg._STALL_ABORT_SECONDS
    assert not proto.aborted


def test_resume_cancels_the_timer_and_rearms_on_the_next_stall():
    proto = _protocol()

    proto.pause_writing()
    proto.resume_writing()

    assert not proto.paused
    assert proto.loop.timers[0].cancelled
    assert not proto.aborted

    # A slow-but-flowing peer keeps oscillating; each drain resets the timer.
    proto.pause_writing()
    assert len(proto.loop.timers) == 2
    assert not proto.loop.timers[1].cancelled


def test_stalled_transport_is_aborted():
    proto = _protocol()

    proto.pause_writing()
    proto.loop.timers[0].fire()

    assert proto.aborted
    # The fired handle is consumed: it cannot fire twice.
    assert proto._stall_abort_handle is None


def test_connection_lost_disarms_the_timer():
    proto = _protocol()

    proto.pause_writing()
    proto.eof_received()
    proto.connection_lost(None)

    assert len(proto.loop.timers) == 2
    assert all(timer.cancelled for timer in proto.loop.timers)
    assert not proto.aborted
    assert proto.exc is None  # super() chain stays intact


def test_eof_aborts_pending_close_without_crossing_high_water():
    proto = _protocol()

    assert proto.eof_received() is None

    assert proto.eof  # the underlying protocol must still receive EOF
    assert not proto.paused
    assert len(proto.loop.timers) == 1
    assert proto.loop.timers[0].delay == tsg._STALL_ABORT_SECONDS
    proto.loop.timers[0].fire()

    assert proto.aborted
    assert proto._eof_abort_handle is None


def test_resume_after_eof_does_not_cancel_the_close_deadline():
    proto = _protocol()
    proto.pause_writing()
    proto.eof_received()

    proto.resume_writing()

    assert proto.loop.timers[0].cancelled
    assert not proto.loop.timers[1].cancelled
    proto.loop.timers[1].fire()
    assert proto.aborted


def test_eof_respects_protocols_that_keep_the_transport_open():
    proto = _protocol()
    proto.keep_open_on_eof = True

    assert proto.eof_received() is True

    assert proto.eof
    assert proto.loop.timers == []


def test_guarded_protocol_factories_compose_the_mixin():
    from uvicorn.protocols.http.auto import AutoHTTPProtocol
    from uvicorn.protocols.websockets.websockets_impl import (
        WebSocketProtocol as UvicornWebSocketProtocol,
    )

    http_cls = tsg.guarded_http_protocol()
    assert issubclass(http_cls, tsg.TransportStallGuardMixin)
    assert issubclass(http_cls, AutoHTTPProtocol)

    ws_cls = tsg.guarded_websockets_protocol()
    assert issubclass(ws_cls, tsg.TransportStallGuardMixin)
    assert issubclass(ws_cls, UvicornWebSocketProtocol)

    # Class creation is cached: uvicorn sees one stable class per process.
    assert tsg.guarded_http_protocol() is http_cls
    assert tsg.guarded_websockets_protocol() is ws_cls
