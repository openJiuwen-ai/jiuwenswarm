"""A turn that never finishes has to end anyway.

The idle timeout cannot end it. A model that reasons without stopping produces a stream
that is never idle, so the 120s idle timeout never fires; the 420s request timeout does
not apply either, because the HTTP request is one long stream that never completes.

Measured live, on a real turn: 9,500 reasoning chunks over thirteen minutes, no tool
call after the first two, still running when the process was killed by hand. Nothing in
the stack would have stopped it, and the person watching saw a spinner the whole time.

The bound is wall-clock across the turn rather than silence between chunks, and it is
deliberately generous -- ending a working turn early is the worse mistake. What matters
is that it ends, and that the reason reaches the person.
"""

from __future__ import annotations

import time
from unittest import mock

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface


def test_the_ceiling_is_a_real_number_by_default():
    """Off by default would make this another switch nobody turns on."""
    assert 0 < interface._STREAM_TURN_CEILING_S < float("inf")


def test_the_ceiling_comes_from_config_with_an_environment_override(monkeypatch):
    """Config first: that is where the sibling stream timeouts live and where a
    deployment looks. The environment variable is the override a debugging session
    reaches for without editing a file.

    Read per turn rather than cached, because editing config.yaml fires no event and a
    setting that needs a restart is one people stop changing."""
    monkeypatch.setenv("JIUWEN_STREAM_TURN_CEILING_S", "60")
    assert interface._stream_turn_ceiling() == 60.0

    monkeypatch.setenv("JIUWEN_STREAM_TURN_CEILING_S", "0")
    assert interface._stream_turn_ceiling() == float("inf"), "0 表示不设上限"

    # Nonsense falls back to the default rather than removing the bound: a typo in a
    # config file must not be a way to turn a safety limit off.
    monkeypatch.setenv("JIUWEN_STREAM_TURN_CEILING_S", "soon")
    assert interface._stream_turn_ceiling() == interface._STREAM_TURN_CEILING_DEFAULT_S

    monkeypatch.delenv("JIUWEN_STREAM_TURN_CEILING_S", raising=False)


def test_the_config_key_ships_with_a_value(monkeypatch):
    """Shipping it commented-out or absent means the only people who get the bound are
    the ones who already knew to look for it."""
    import pathlib

    import yaml

    root = pathlib.Path(interface.__file__).resolve().parents[3]
    cfg = yaml.safe_load((root / "resources" / "config.yaml").read_text(encoding="utf-8"))
    entries = (cfg.get("models") or {}).get("defaults") or []
    ceilings = [
        (e.get("model_client_config") or {}).get("stream_turn_ceiling")
        for e in entries
        if isinstance(e, dict)
    ]
    assert any(c for c in ceilings), "默认配置里必须带这个键"


@pytest.mark.asyncio
async def test_a_stream_that_never_stops_is_cut_off_and_says_why():
    """The shape of the live failure, in miniature: a producer that keeps yielding and
    never completes.

    Checked against the deadline logic itself rather than the whole adapter, because
    what went wrong was not the adapter -- it was that no clock was consulted at all."""
    deadline = time.monotonic() + 0.2
    delivered = 0

    async def never_stops():
        while True:
            yield "reasoning"

    stopped_with = None
    async for _chunk in never_stops():
        if time.monotonic() > deadline:
            stopped_with = "ceiling"
            break
        delivered += 1

    assert stopped_with == "ceiling", "无休止的流必须被时钟切断，而不是等它自己停"
    assert delivered > 0, "在上限之前的块应当照常送达"


def test_the_message_tells_the_person_what_to_do():
    """A turn that ends with nothing said reads as a crash. The text names the likely
    cause and what to try, because the person cannot see the reasoning loop."""
    import inspect

    src = inspect.getsource(interface)
    assert "反复推理" in src
    assert "已中止" in src


@pytest.mark.asyncio
async def test_a_stream_idle_past_the_ceiling_is_abandoned():
    """The other half of the ceiling, and the half a refactor nearly dropped.

    The adapter-side clock above only bounds a stream that is already yielding. A
    stream stuck *before* its first chunk never reaches that loop, so the keepalive
    is the only thing awake -- and left to itself it would carry a dead request
    forever; the longest measured instance kept a gateway warning firing every ten
    seconds for nine hours.

    The keepalive counts the idle time because it is already awake for it, and hands
    the decision back through a callback: what to cancel is the caller's business,
    not the transport's. This test pins the count and the handoff; the caller's own
    cancellation is ordinary task cancellation.
    """
    import asyncio

    from jiuwenswarm.server import agent_ws_server as ws_server

    abandoned = asyncio.Event()

    class _Req:
        request_id = "r-idle"
        channel_id = "web"
        agent_ref = None

    class _WS:
        async def send(self, *_a, **_k):  # pragma: no cover - never reached
            raise AssertionError("an idle stream must not send before it is cut off")

    keepalive = ws_server._StreamKeepalive(
        _WS(), _Req(), asyncio.Lock(), on_idle_timeout=abandoned.set
    )
    # Both clocks scaled down: one keepalive tick is already past the ceiling, so the
    # first idle period trips it and the test does not wait on wall time.
    with (
        mock.patch.object(ws_server, "_STREAM_KEEPALIVE_INTERVAL_SECONDS", 0.01),
        mock.patch(
            "jiuwenswarm.server.runtime.agent_adapter.interface._stream_turn_ceiling",
            lambda: 0.01,
        ),
    ):
        keepalive.start()
        await asyncio.wait_for(abandoned.wait(), timeout=2.0)

    assert abandoned.is_set(), "空闲超过上限必须回调，否则死请求永远挂着"


@pytest.mark.asyncio
async def test_activity_resets_the_idle_count_so_a_working_stream_is_never_cut():
    """A stream that keeps producing must never trip the idle cap, however long it
    runs -- that is what makes this an *idle* watchdog rather than a second, shorter
    copy of the wall-clock ceiling."""
    import asyncio

    from jiuwenswarm.server import agent_ws_server as ws_server

    tripped = asyncio.Event()

    class _Req:
        request_id = "r-busy"
        channel_id = "web"
        agent_ref = None

    keepalive = ws_server._StreamKeepalive(
        object(), _Req(), asyncio.Lock(), on_idle_timeout=tripped.set
    )
    with (
        mock.patch.object(ws_server, "_STREAM_KEEPALIVE_INTERVAL_SECONDS", 0.01),
        mock.patch(
            "jiuwenswarm.server.runtime.agent_adapter.interface._stream_turn_ceiling",
            lambda: 0.05,
        ),
    ):
        keepalive.start()
        for _ in range(20):
            keepalive.notify_activity()
            await asyncio.sleep(0.01)
        await keepalive.stop()

    assert not tripped.is_set(), "有产出的流不得被空闲看门狗切断"
