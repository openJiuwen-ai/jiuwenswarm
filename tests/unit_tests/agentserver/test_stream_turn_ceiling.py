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

import asyncio
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
    setting that needs a restart is one people stop changing.
    """
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
    the ones who already knew to look for it.
    """
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


class _NeverStops:
    """An adapter whose model reasons forever: always yielding, never complete."""

    def __init__(self) -> None:
        self.closed = False

    async def create_instance(self, config=None):
        return None

    async def reload_agent_config(self, *_a, **_k):
        return None

    async def process_message_stream_impl(self, request, _inputs):
        from jiuwenswarm.common.schema.agent import AgentResponseChunk

        try:
            while True:
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={"event_type": "chat.reasoning", "content": "…"},
                    is_complete=False,
                )
                await asyncio.sleep(0)
        finally:
            self.closed = True


@pytest.mark.asyncio
async def test_a_stream_that_never_stops_is_cut_off_and_says_why(monkeypatch):
    """The shape of the live failure, through the production facade: a producer that
    keeps yielding and never completes. The turn must end, with an error that tells
    the person what to try, and the producer must be closed.
    """
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    adapter = _NeverStops()
    facade = JiuWenSwarm()
    monkeypatch.setattr(facade, "_adapter", adapter)
    monkeypatch.setattr(facade, "_sdk_name", "harness")
    monkeypatch.setattr(interface, "append_history_record", lambda **_kw: None)
    monkeypatch.setattr(interface, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface, "get_memory_mode", lambda _cfg: "off")
    monkeypatch.setattr(interface, "build_user_prompt", lambda q, **_kw: q)
    monkeypatch.setattr(interface, "_stream_turn_ceiling", lambda: 0.2)

    request = AgentRequest(
        request_id="req-ceiling", channel_id="web", session_id="ceiling_sess",
        params={"query": "hello", "mode": "agent"},
    )
    chunks = []

    async def _drain():
        async for chunk in facade.process_message_stream(request):
            chunks.append(chunk)

    await asyncio.wait_for(_drain(), timeout=10.0)

    reasoning = [c for c in chunks if (c.payload or {}).get("event_type") == "chat.reasoning"]
    assert reasoning, "在上限之前的块应当照常送达"
    errors = [c for c in chunks if (c.payload or {}).get("event_type") == "chat.error"]
    assert errors, "无休止的流必须以错误结束，而不是等它自己停"
    message = str(errors[-1].payload.get("error"))
    assert "已中止" in message and "反复推理" in message, "提示要说明原因和该怎么做"
    assert chunks[-1].is_complete, "回合必须有终止帧"
    assert adapter.closed, "生产流必须被关闭"


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
    copy of the wall-clock ceiling.
    """
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


def _stalled_server(closed: list):
    """The real stream handler over a runtime that never yields anything."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from jiuwenswarm.server import agent_ws_server as ws_server

    server = object.__new__(ws_server.AgentWebSocketServer)
    server._session_stream_tasks = {}
    server._open_session_message_resume = AsyncMock(return_value=None)
    server._finalize_session_message_resume = AsyncMock()

    async def stalled(*_a, **_k):
        try:
            await asyncio.Event().wait()
            yield None  # pragma: no cover - never reached
        finally:
            closed.append(True)

    server._execution_runtime = lambda: SimpleNamespace(stream=stalled)
    return server


class _CapturingWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, text):
        import json

        self.sent.append(json.loads(text))


def _request():
    from jiuwenswarm.common.schema.agent import AgentRequest

    return AgentRequest(
        request_id="r-idle", channel_id="web", session_id="s-idle", is_stream=True,
    )


@pytest.mark.asyncio
async def test_an_abandoned_stream_ends_with_one_terminal_error_the_gateway_stops_on():
    """Cancelling the host task frees the runtime, but nobody else answers the request:
    the gateway reads its queue without a timeout until a chunk says ``is_complete``.
    The handler therefore sends that chunk itself, and the gateway's own parser must
    read it as the end of the stream.
    """
    import asyncio

    from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_chunk
    from jiuwenswarm.server import agent_ws_server as ws_server

    closed: list = []
    server = _stalled_server(closed)
    ws = _CapturingWS()
    with (
        mock.patch.object(ws_server, "_STREAM_KEEPALIVE_INTERVAL_SECONDS", 0.01),
        mock.patch.object(interface, "_stream_turn_ceiling", lambda: 0.01),
        mock.patch.object(ws_server.AgentRuntime, "uses_session_runtime", return_value=False),
    ):
        task = asyncio.create_task(server._handle_stream_impl(ws, _request(), asyncio.Lock()))
        await asyncio.wait_for(task, timeout=2.0)

    assert not task.cancelled(), "空闲中止由处理器自己收口，不应以取消告终"
    assert closed, "Runtime 必须被关闭，资源才会释放"
    assert len(ws.sent) == 1, "恰好一个终止帧"
    chunk = parse_agent_server_wire_chunk(ws.sent[0])
    assert chunk.is_complete, "Gateway 只在 is_complete 时停止等待"
    assert chunk.payload["event_type"] == "chat.error"
    assert "没有任何输出" in chunk.payload["error"]
    kwargs = server._finalize_session_message_resume.await_args.kwargs
    assert kwargs["outcome"] == "failed"


@pytest.mark.asyncio
async def test_a_user_cancel_still_propagates_and_sends_nothing():
    """Only the watchdog's own cancel is answered; a user cancel has its own reply."""
    import asyncio

    from jiuwenswarm.server import agent_ws_server as ws_server

    closed: list = []
    server = _stalled_server(closed)
    ws = _CapturingWS()
    with mock.patch.object(ws_server.AgentRuntime, "uses_session_runtime", return_value=False):
        task = asyncio.create_task(server._handle_stream_impl(ws, _request(), asyncio.Lock()))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)

    assert closed, "用户取消同样要释放 Runtime"
    assert ws.sent == [], "用户取消由中断请求自己回包，这里不应再发终止帧"
