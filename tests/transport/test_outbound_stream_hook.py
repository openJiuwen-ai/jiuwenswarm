# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""出站事件流扩展：注册、选择、改写、raw、失败降级与流水线接线。"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.extensions.sdk.outbound_stream import (
    OutboundStreamExtension,
    StreamMapContext,
)
from jiuwenswarm.server.transports import intercept
from jiuwenswarm.server.transports.intercept import (
    InterceptingSink,
    detect_transport,
    resolve_outbound_stream,
)
from jiuwenswarm.server.transports.sink import SSESink, UnaryHTTPSink, WSSink

CHANNEL = "agui"
NAME = "demo"


def _wire(*, event_type: str = "chat.delta", delta: str = "hi", is_final: bool = False) -> dict:
    return {
        "protocol_version": "1.0",
        "request_id": "r1",
        "sequence": 0,
        "is_final": is_final,
        "status": "in_progress",
        "response_kind": "e2a.chunk",
        "body": {"event_type": event_type, "delta": delta},
        "channel": CHANNEL,
    }


def _raw(payload: dict) -> dict:
    return {"type": "raw", "data": payload}


class _Ext(OutboundStreamExtension):
    def __init__(self, mapper=None) -> None:
        self.mapper = mapper or (lambda frame, ctx: [frame])
        self.opened = 0
        self.closed = 0
        self.errors: list[BaseException] = []
        self.error_frames: list[dict] = []

    async def initialize(self, config) -> None:  # noqa: ANN001
        return None

    async def shutdown(self) -> None:
        return None

    def map_frame(self, frame, ctx):  # noqa: ANN001
        return self.mapper(frame, ctx)

    def on_open(self, ctx) -> None:  # noqa: ANN001
        self.opened += 1

    def on_close(self, ctx) -> None:  # noqa: ANN001
        self.closed += 1

    def on_error(self, ctx, error):  # noqa: ANN001
        self.errors.append(error)
        return list(self.error_frames)


class _RecordingSink:
    def __init__(self, *, fail_raw: bool = False) -> None:
        self.chunks: list[tuple] = []
        self.wires: list[dict] = []
        self.raws: list[dict] = []
        self.unarys: list = []
        self.errors: list[tuple] = []
        self._fail_raw = fail_raw

    async def send_unary(self, resp, *, response_id=None):  # noqa: ANN001
        self.unarys.append((resp, response_id))
        return True

    async def send_chunk(self, chunk, *, sequence, response_id=None):  # noqa: ANN001
        self.chunks.append((chunk, sequence, response_id))
        return True

    async def send_wire(self, wire):  # noqa: ANN001
        self.wires.append(wire)
        return True

    async def send_error(self, request_id, message, *, code="INTERNAL_ERROR", channel_id=""):  # noqa: ANN001
        self.errors.append((request_id, message, code))
        return True

    async def send_raw(self, data):  # noqa: ANN001
        self.raws.append(data)
        return not self._fail_raw


def _ctx(extension: OutboundStreamExtension | None = None) -> StreamMapContext:
    return StreamMapContext(
        transport="ws",
        channel_id=CHANNEL,
        request_id="r1",
        session_id="s1",
        connection_id="c1",
        request=None,
    )


def _install_registry(extension: OutboundStreamExtension, name: str = NAME) -> None:
    ExtensionRegistry.reset_instance()
    registry = ExtensionRegistry.create_instance(None, {}, logging.getLogger("test"))
    registry.register_outbound_stream_extension(name, extension)


@pytest.fixture(autouse=True)
def stub_pipeline():
    """覆盖 tests/transport/conftest.py 的 autouse 桩：本模块要测真实流水线接线。"""
    yield None


@pytest.fixture(autouse=True)
def _reset_registry():
    ExtensionRegistry.reset_instance()
    yield
    ExtensionRegistry.reset_instance()


@pytest.fixture()
def stream_config(monkeypatch):
    monkeypatch.setattr(
        intercept,
        "get_config",
        lambda: {"outbound_stream": {"channels": {CHANNEL: NAME}}},
    )


# ── 注册表 ─────────────────────────────────────────────────────


def test_registry_register_get_and_duplicate() -> None:
    _install_registry(_Ext())
    registry = ExtensionRegistry.get_instance()

    assert registry.get_outbound_stream_extension(NAME) is not None
    assert registry.list_outbound_stream_extensions() == [NAME]
    with pytest.raises(ValueError):
        registry.register_outbound_stream_extension(NAME, _Ext())
    assert registry.get_outbound_stream_extension("missing") is None


def test_registry_reset_clears_extensions() -> None:
    _install_registry(_Ext())
    ExtensionRegistry.reset_instance()
    registry = ExtensionRegistry.create_instance(None, {}, logging.getLogger("test"))
    assert registry.list_outbound_stream_extensions() == []


# ── 选择 ───────────────────────────────────────────────────────


def _chat_request(*, is_stream: bool = True, method=ReqMethod.CHAT_SEND) -> AgentRequest:
    return AgentRequest(
        request_id="r1",
        channel_id=CHANNEL,
        req_method=method,
        is_stream=is_stream,
        params={},
    )


def test_resolve_returns_none_without_config(monkeypatch) -> None:
    monkeypatch.setattr(intercept, "get_config", lambda: {})
    _install_registry(_Ext())
    assert resolve_outbound_stream(_chat_request()) is None


def test_resolve_only_for_streaming_chat(stream_config) -> None:
    _install_registry(_Ext())
    assert resolve_outbound_stream(_chat_request()) is not None
    assert resolve_outbound_stream(_chat_request(is_stream=False)) is None
    assert (
        resolve_outbound_stream(_chat_request(method=ReqMethod.HISTORY_GET)) is None
    )


def test_resolve_unknown_extension_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(
        intercept,
        "get_config",
        lambda: {"outbound_stream": {"channels": {CHANNEL: "missing"}}},
    )
    _install_registry(_Ext())
    assert resolve_outbound_stream(_chat_request()) is None


def test_resolve_channel_id_normalizes_empty_to_web() -> None:
    request = _chat_request()
    request.channel_id = ""
    assert intercept.resolve_channel_id(request) == "web"


def test_resolve_uses_web_for_empty_channel(monkeypatch) -> None:
    monkeypatch.setattr(
        intercept,
        "get_config",
        lambda: {"outbound_stream": {"channels": {"web": NAME}}},
    )
    _install_registry(_Ext())
    request = _chat_request()
    request.channel_id = ""
    assert resolve_outbound_stream(request) is not None


def test_resolve_without_registry_instance(monkeypatch, stream_config) -> None:
    ExtensionRegistry.reset_instance()
    assert resolve_outbound_stream(_chat_request()) is None


# ── 拦截器 ─────────────────────────────────────────────────────


def _chunk() -> AgentResponseChunk:
    return AgentResponseChunk(
        request_id="r1",
        channel_id=CHANNEL,
        payload={"event_type": "chat.delta", "content": "hi"},
    )


def test_unchanged_frame_keeps_original_sink_path() -> None:
    inner = _RecordingSink()
    ext = _Ext()
    sink = InterceptingSink(inner, ext, _ctx(ext))

    assert asyncio.run(sink.send_chunk(_chunk(), sequence=1)) is True
    assert len(inner.chunks) == 1, "原样返回时必须走原 send_chunk（保留背压语义）"
    assert inner.wires == []


def test_modified_frame_sent_through_send_wire() -> None:
    inner = _RecordingSink()

    def _mapper(frame, ctx):
        changed = dict(frame)
        changed["body"] = {**frame.get("body", {}), "delta": "changed"}
        return [changed]

    ext = _Ext(_mapper)
    sink = InterceptingSink(inner, ext, _ctx(ext))

    assert asyncio.run(sink.send_chunk(_chunk(), sequence=1)) is True
    assert inner.chunks == []
    assert len(inner.wires) == 1
    assert inner.wires[0]["body"]["delta"] == "changed"


def test_dropped_frame_sends_nothing() -> None:
    inner = _RecordingSink()
    ext = _Ext(lambda frame, ctx: [])
    sink = InterceptingSink(inner, ext, _ctx(ext))

    assert asyncio.run(sink.send_chunk(_chunk(), sequence=1)) is True
    assert inner.chunks == [] and inner.wires == [] and inner.raws == []


def test_raw_frame_goes_through_send_raw() -> None:
    inner = _RecordingSink()
    ext = _Ext(lambda frame, ctx: [_raw({"type": "TEXT_MESSAGE_CONTENT", "delta": "hi"})])
    sink = InterceptingSink(inner, ext, _ctx(ext))

    assert asyncio.run(sink.send_chunk(_chunk(), sequence=1)) is True
    assert inner.raws == [{"type": "TEXT_MESSAGE_CONTENT", "delta": "hi"}]
    assert inner.wires == []


def test_drop_then_failure_falls_back_to_native() -> None:
    """丢帧（返回 []）不代表客户端收到过内容；随后失败仍应回落原生。"""
    inner = _RecordingSink()
    calls = {"n": 0}

    def _mapper(frame, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return []
        raise RuntimeError("boom after drop")

    ext = _Ext(_mapper)
    sink = InterceptingSink(inner, ext, _ctx(ext))

    assert asyncio.run(sink.send_chunk(_chunk(), sequence=1)) is True
    assert inner.chunks == [], "丢帧不应写出任何内容"
    assert asyncio.run(sink.send_chunk(_chunk(), sequence=2)) is True
    assert len(inner.chunks) == 1, "客户端仍是零字节，失败必须回落原生"


def test_failed_send_does_not_mark_engaged() -> None:
    """改造帧发送失败（如 WS 已关闭）也不代表客户端收到过，失败应回落原生。"""
    inner = _RecordingSink(fail_raw=True)
    calls = {"n": 0}

    def _mapper(frame, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return [_raw({"type": "TEXT_MESSAGE_CONTENT"})]
        raise RuntimeError("boom")

    ext = _Ext(_mapper)
    sink = InterceptingSink(inner, ext, _ctx(ext))

    assert asyncio.run(sink.send_chunk(_chunk(), sequence=1)) is False
    assert asyncio.run(sink.send_chunk(_chunk(), sequence=2)) is True
    assert len(inner.chunks) == 1, "改造帧没有送达，失败应回落原生"


def test_close_does_not_warn_for_raw_only_stream(caplog) -> None:
    """纯 raw 输出是主场景：终态由扩展负责，不应误报"缺终帧"。"""
    inner = _RecordingSink()
    ext = _Ext(lambda frame, ctx: [_raw({"type": "TEXT_MESSAGE_CONTENT"})])
    sink = InterceptingSink(inner, ext, _ctx(ext))

    asyncio.run(sink.send_chunk(_chunk(), sequence=1))
    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.server.transports.intercept"):
        sink.close()
    assert "no recognizable terminal frame" not in caplog.text


def test_failure_before_engagement_falls_back_to_native() -> None:
    inner = _RecordingSink()

    def _boom(frame, ctx):
        raise RuntimeError("boom")

    ext = _Ext(_boom)
    sink = InterceptingSink(inner, ext, _ctx(ext))

    assert asyncio.run(sink.send_chunk(_chunk(), sequence=1)) is True
    assert len(inner.chunks) == 1, "尚未发出改造帧，应回落原生 send_chunk"
    assert inner.wires == []
    # 后续帧继续走原生，不再调用扩展
    assert asyncio.run(sink.send_chunk(_chunk(), sequence=2)) is True
    assert len(inner.chunks) == 2


def test_failure_after_engagement_terminates_without_mixing() -> None:
    inner = _RecordingSink()
    calls = {"n": 0}

    def _mapper(frame, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return [_raw({"type": "TEXT_MESSAGE_CONTENT"})]
        raise RuntimeError("boom after engagement")

    ext = _Ext(_mapper)
    ext.error_frames = [_raw({"type": "RUN_ERROR"})]
    sink = InterceptingSink(inner, ext, _ctx(ext))

    assert asyncio.run(sink.send_chunk(_chunk(), sequence=1)) is True
    assert inner.raws == [{"type": "TEXT_MESSAGE_CONTENT"}]

    assert asyncio.run(sink.send_chunk(_chunk(), sequence=2)) is True
    assert inner.raws[-1] == {"type": "RUN_ERROR"}, "已进入扩展模式，失败应发扩展的终止帧"
    assert inner.chunks == [], "已进入扩展模式后不允许再混入原生 chunk"
    assert inner.wires == []
    assert ext.errors, "on_error 应被调用"

    # 终止后后续帧全部丢弃，且不再调用扩展
    assert asyncio.run(sink.send_chunk(_chunk(), sequence=3)) is False
    assert len(inner.raws) == 2


def test_default_on_error_terminates_silently() -> None:
    inner = _RecordingSink()
    calls = {"n": 0}

    def _mapper(frame, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return [_raw({"type": "TEXT_MESSAGE_CONTENT"})]
        raise RuntimeError("boom")

    ext = _Ext(_mapper)
    sink = InterceptingSink(inner, ext, _ctx(ext))

    asyncio.run(sink.send_chunk(_chunk(), sequence=1))
    assert asyncio.run(sink.send_chunk(_chunk(), sequence=2)) is True  # 空列表 = 停流
    assert inner.raws == [{"type": "TEXT_MESSAGE_CONTENT"}]
    assert asyncio.run(sink.send_chunk(_chunk(), sequence=3)) is False


def test_invalid_return_after_engagement_terminates() -> None:
    inner = _RecordingSink()
    calls = {"n": 0}

    def _mapper(frame, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return [_raw({"type": "TEXT_MESSAGE_CONTENT"})]
        return [{"not": "a frame"}]

    ext = _Ext(_mapper)
    sink = InterceptingSink(inner, ext, _ctx(ext))

    asyncio.run(sink.send_chunk(_chunk(), sequence=1))
    asyncio.run(sink.send_chunk(_chunk(), sequence=2))
    assert inner.wires == [] and inner.chunks == []


def test_non_serializable_raw_falls_back_before_engagement() -> None:
    """raw 的 data 不可 JSON 序列化 → 按"返回非法"处理：尚未 engaged 时回落原生。

    序列化发生在 sink（``WSSink.send_raw`` / SSE 泵）里，属于失败隔离之外；
    核心必须在 ``_validate`` 里提前发现，否则 TypeError 会击穿整条请求。
    """
    inner = _RecordingSink()
    ext = _Ext(lambda frame, ctx: [_raw({"set": {1, 2}, "obj": object()})])
    sink = InterceptingSink(inner, ext, _ctx(ext))

    assert asyncio.run(sink.send_chunk(_chunk(), sequence=1)) is True
    assert inner.raws == [], "不可序列化的 raw 不得进入传输层"
    assert len(inner.chunks) == 1, "尚未 engaged，客户端零字节，必须回落原生"
    assert ext.errors == [], "尚未 engaged 不走 on_error"


def test_non_serializable_raw_after_engagement_terminates() -> None:
    """已发出过改造帧后 raw 序列化失败 → 走 on_error 终止，不外溢异常、不混协议。"""
    inner = _RecordingSink()
    calls = {"n": 0}

    def _mapper(frame, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return [_raw({"type": "TEXT_MESSAGE_CONTENT"})]
        return [_raw({"obj": object()})]

    ext = _Ext(_mapper)
    ext.error_frames = [_raw({"type": "RUN_ERROR"})]
    sink = InterceptingSink(inner, ext, _ctx(ext))

    assert asyncio.run(sink.send_chunk(_chunk(), sequence=1)) is True
    assert asyncio.run(sink.send_chunk(_chunk(), sequence=2)) is True
    assert inner.raws == [
        {"type": "TEXT_MESSAGE_CONTENT"},
        {"type": "RUN_ERROR"},
    ], "序列化失败应按扩展终止帧收尾"
    assert inner.chunks == [] and inner.wires == [], "不得回落原生、不得混入 E2A"
    assert ext.errors, "on_error 应被调用"
    assert asyncio.run(sink.send_chunk(_chunk(), sequence=3)) is False


def test_raw_frame_without_data_is_invalid() -> None:
    """raw 帧缺 data 字段同属契约违规：按"返回非法"降级，不写出 null。"""
    inner = _RecordingSink()
    ext = _Ext(lambda frame, ctx: [{"type": "raw"}])
    sink = InterceptingSink(inner, ext, _ctx(ext))

    assert asyncio.run(sink.send_chunk(_chunk(), sequence=1)) is True
    assert inner.raws == []
    assert len(inner.chunks) == 1, "契约违规必须回落原生"


def test_native_error_frame_suppressed_after_engagement() -> None:
    inner = _RecordingSink()
    ext = _Ext(lambda frame, ctx: [_raw({"type": "TEXT_MESSAGE_CONTENT"})])
    sink = InterceptingSink(inner, ext, _ctx(ext))

    asyncio.run(sink.send_chunk(_chunk(), sequence=1))
    assert asyncio.run(sink.send_error("r1", "boom")) is False
    assert inner.errors == [], "扩展模式下的原生错误帧会混协议，必须丢弃"


def test_close_notifies_extension() -> None:
    inner = _RecordingSink()
    ext = _Ext()
    sink = InterceptingSink(inner, ext, _ctx(ext))
    sink.close()
    assert ext.closed == 1


def test_detect_transport() -> None:
    assert detect_transport(WSSink(object(), asyncio.Lock())) == "ws"
    assert detect_transport(SSESink()) == "http-sse"
    assert detect_transport(UnaryHTTPSink()) == "http-unary"


# ── sink.send_raw ──────────────────────────────────────────────


def test_ws_sink_send_raw_writes_json_text() -> None:
    class _Ws:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, text: str) -> None:
            self.sent.append(text)

    ws = _Ws()
    assert asyncio.run(WSSink(ws, asyncio.Lock()).send_raw({"type": "RUN_STARTED"})) is True
    assert json.loads(ws.sent[0]) == {"type": "RUN_STARTED"}


def test_sse_sink_send_raw_enqueues_marker() -> None:
    sink = SSESink(maxsize=4)

    async def _run() -> dict:
        await sink.send_raw({"type": "RUN_STARTED"})
        return sink.queue.get_nowait()

    assert asyncio.run(_run()) == {"type": "raw", "data": {"type": "RUN_STARTED"}}


def test_unary_http_sink_rejects_raw() -> None:
    assert asyncio.run(UnaryHTTPSink().send_raw({"type": "RUN_STARTED"})) is False


# ── 流水线接线 ─────────────────────────────────────────────────


def test_pipeline_wraps_sink_and_closes_extension(monkeypatch) -> None:
    # pipeline 依赖 openjiuwen dev-stable 的最新模块；本地 venv 落后时跳过，
    # CI（按 pyproject 重新装依赖）会正常执行。
    pytest.importorskip(
        "openjiuwen.rsi.harness_rsi.auto_harness",
        reason="openjiuwen version is older than the dev-stable API used by pipeline",
    )
    from jiuwenswarm.server import pipeline as pipeline_mod
    from jiuwenswarm.server.context import RequestContext

    inner = _RecordingSink()
    ext = _Ext()
    request = _chat_request()
    request.channel_id = ""  # 空渠道：扩展侧上下文应与选路一致，归一化为 web
    ctx = RequestContext(
        request=request,
        sink=inner,
        connection_id="c1",
        services=None,
    )

    async def _noop_hook(req):  # noqa: ANN001
        return None

    captured: dict = {}

    async def _fake_dispatch(ctx_, request_):  # noqa: ANN001
        captured["sink"] = ctx_.sink
        return True

    monkeypatch.setattr(pipeline_mod, "resolve_outbound_stream", lambda req: ext)
    monkeypatch.setattr(pipeline_mod, "_trigger_before_chat_request_hook", _noop_hook)
    monkeypatch.setattr(pipeline_mod, "dispatch_with_context", _fake_dispatch)

    asyncio.run(pipeline_mod.dispatch_parsed_request(ctx, request))

    assert isinstance(captured["sink"], InterceptingSink)
    assert captured["sink"]._inner is inner
    assert captured["sink"]._ctx.channel_id == "web"
    assert ext.opened == 1
    assert ext.closed == 1


def test_pipeline_setup_failure_falls_back_to_native(monkeypatch) -> None:
    pytest.importorskip(
        "openjiuwen.rsi.harness_rsi.auto_harness",
        reason="openjiuwen version is older than the dev-stable API used by pipeline",
    )
    from jiuwenswarm.server import pipeline as pipeline_mod
    from jiuwenswarm.server.context import RequestContext

    inner = _RecordingSink()
    request = _chat_request()
    ctx = RequestContext(request=request, sink=inner, connection_id="c1", services=None)
    captured: dict = {}

    async def _noop_hook(req):  # noqa: ANN001
        return None

    async def _fake_dispatch(ctx_, request_):  # noqa: ANN001
        captured["sink"] = ctx_.sink
        return True

    def _boom(_request):  # noqa: ANN001
        raise RuntimeError("config exploded")

    monkeypatch.setattr(pipeline_mod, "resolve_outbound_stream", _boom)
    monkeypatch.setattr(pipeline_mod, "_trigger_before_chat_request_hook", _noop_hook)
    monkeypatch.setattr(pipeline_mod, "dispatch_with_context", _fake_dispatch)

    asyncio.run(pipeline_mod.dispatch_parsed_request(ctx, request))

    assert captured["sink"] is inner, "扩展接入异常必须降级为原生 sink，不能逃出流水线"
