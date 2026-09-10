# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import asyncio
import json

import pytest

from jiuwenswarm.server.transports.push_registry import PushRegistry


class _RecordingSink:

    def __init__(self, *, ok: bool = True, boom: bool = False) -> None:
        self.wires: list[dict] = []
        self._ok = ok
        self._boom = boom

    async def send_wire(self, wire: dict) -> bool:
        if self._boom:
            raise RuntimeError("sink exploded")
        self.wires.append(wire)
        return self._ok


def _wire(*, session_id: str | None = None, channel: str | None = None) -> dict:
    return {"request_id": "r1", "session_id": session_id, "channel": channel, "body": {}}


def test_push_fans_out_to_all_unfiltered_subscribers() -> None:
    reg = PushRegistry()
    a, b = _RecordingSink(), _RecordingSink()
    reg.register("a", a)
    reg.register("b", b)

    delivered = asyncio.run(reg.push(_wire()))

    assert delivered == 2
    assert len(a.wires) == 1 and len(b.wires) == 1


def test_session_filter_narrows_delivery() -> None:
    reg = PushRegistry()
    all_events, only_s1 = _RecordingSink(), _RecordingSink()
    reg.register("all", all_events)
    reg.register("s1", only_s1, session_id="s1")

    asyncio.run(reg.push(_wire(session_id="s1")))
    asyncio.run(reg.push(_wire(session_id="s2")))

    assert len(all_events.wires) == 2, "未声明过滤条件的订阅者应全收"
    assert len(only_s1.wires) == 1, "声明了 session 的订阅者不应收到别的会话"
    assert only_s1.wires[0]["session_id"] == "s1"


def test_session_filtered_subscriber_skips_frames_without_session() -> None:
    reg = PushRegistry()
    sink = _RecordingSink()
    reg.register("s1", sink, session_id="s1")

    assert asyncio.run(reg.push(_wire(session_id=None))) == 0
    assert sink.wires == []


def test_channel_filter_matches_wire_channel_field() -> None:
    reg = PushRegistry()
    sink = _RecordingSink()
    reg.register("web", sink, channel_id="web")

    asyncio.run(reg.push(_wire(channel="web")))
    asyncio.run(reg.push(_wire(channel="tui")))

    assert len(sink.wires) == 1


def test_failing_subscriber_is_isolated_and_unregistered() -> None:
    reg = PushRegistry()
    bad, good = _RecordingSink(boom=True), _RecordingSink()
    reg.register("bad", bad)
    reg.register("good", good)

    delivered = asyncio.run(reg.push(_wire()))

    assert delivered == 1, "好的订阅者仍应收到"
    assert len(good.wires) == 1
    assert reg.subscriber_count() == 1, "故障订阅者应已被注销"


def test_sink_returning_false_is_not_counted_as_delivered() -> None:
    reg = PushRegistry()
    reg.register("x", _RecordingSink(ok=False))
    assert asyncio.run(reg.push(_wire())) == 0


def test_unregister_is_idempotent() -> None:
    reg = PushRegistry()
    reg.register("a", _RecordingSink())
    reg.unregister("a")
    reg.unregister("a")
    assert reg.subscriber_count() == 0


def test_register_same_id_replaces() -> None:
    reg = PushRegistry()
    old, new = _RecordingSink(), _RecordingSink()
    reg.register("dup", old)
    reg.register("dup", new)

    asyncio.run(reg.push(_wire()))

    assert reg.subscriber_count() == 1
    assert old.wires == [] and len(new.wires) == 1


def test_push_with_no_subscribers_is_noop() -> None:
    assert asyncio.run(PushRegistry().push(_wire())) == 0


def test_reverse_rpc_uses_only_declared_owner() -> None:
    reg = PushRegistry()
    ordinary, gateway = _RecordingSink(), _RecordingSink()
    reg.register("ordinary", ordinary)
    reg.register("gateway", gateway, reverse_rpc_capable=True)

    assert reg.reverse_rpc_ready() is True
    assert asyncio.run(reg.push_reverse_rpc(_wire())) == 1
    assert ordinary.wires == []
    assert len(gateway.wires) == 1


def test_reverse_rpc_owner_replacement_fails_existing_requests() -> None:
    reg = PushRegistry()
    owner_losses = []
    reg.set_reverse_rpc_owner_lost_callback(lambda: owner_losses.append(True))
    first, second = _RecordingSink(), _RecordingSink()
    reg.register("first", first, reverse_rpc_capable=True)
    reg.register("second", second, reverse_rpc_capable=True)

    assert owner_losses == [True]
    assert asyncio.run(reg.push_reverse_rpc(_wire())) == 1
    assert first.wires == []
    assert len(second.wires) == 1


def test_ordinary_subscriber_does_not_make_reverse_rpc_ready() -> None:
    reg = PushRegistry()
    reg.register("ordinary", _RecordingSink())

    assert reg.reverse_rpc_ready() is False
    assert asyncio.run(reg.push_reverse_rpc(_wire())) == 0


def test_send_push_reaches_http_subscriber_without_ws() -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.transports.push_registry import get_push_registry

    # 不存在「没有 WS 连接」这个独立状态 —— WS 也是注册表里的订阅者。
    # 「无 WS」等价于「注册表里没有 WS 那个固定 id」，这里本来就没注册过。
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)

    reg = get_push_registry()
    sink = _RecordingSink()
    reg.register("test-http", sink)
    try:
        asyncio.run(server.send_push({"request_id": "r1", "channel_id": "web", "payload": {"a": 1}}))
    finally:
        reg.unregister("test-http")

    assert len(sink.wires) == 1, "无 WS 连接时 HTTP 订阅者仍应收到推送"
    assert sink.wires[0].get("request_id") == "r1"


def test_send_push_without_ws_and_without_subscribers_is_quiet() -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.transports.push_registry import get_push_registry

    assert get_push_registry().subscriber_count() == 0, "前置：注册表应是干净的"
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)

    delivered = asyncio.run(
        server.send_push({"request_id": "r1", "channel_id": "web", "payload": {}})
    )
    assert delivered == 0


def test_events_stream_route_is_registered(http_server) -> None:
    from jiuwenswarm.server.agent_http_server import API_PREFIX

    app = http_server.build_app()
    routes = {
        (getattr(r, "path", None), tuple(sorted(getattr(r, "methods", None) or ())))
        for r in app.routes
    }
    target = f"{API_PREFIX}/events/stream"
    assert any(p == target and "GET" in m for p, m in routes), (
        f"未注册 {target}（GET）；现有路由：{sorted(p for p, _ in routes if p)}"
    )


def test_events_stream_delivers_push_end_to_end(http_server) -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.transports.push_registry import get_push_registry

    app = http_server.build_app()

    async def scenario() -> tuple[list[dict], int]:
        sent: list[dict] = []
        disconnect = asyncio.Event()

        async def receive() -> dict:
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(msg: dict) -> None:
            sent.append(msg)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": "/api/v1/events/stream",
            "raw_path": b"/api/v1/events/stream", "query_string": b"", "root_path": "",
            "headers": [(b"host", b"t"), (b"accept", b"text/event-stream")],
            "client": ("127.0.0.1", 1234), "server": ("t", 80),
        }
        task = asyncio.create_task(app(scope, receive, send))

        for _ in range(100):
            if get_push_registry().subscriber_count() > 0:
                break
            await asyncio.sleep(0.02)

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        await server.send_push(
            {"request_id": "e2e-1", "channel_id": "web", "payload": {"hello": "sse"}}
        )
        await asyncio.sleep(0.3)

        disconnect.set()
        try:
            await asyncio.wait_for(task, timeout=3)
        except asyncio.TimeoutError:
            task.cancel()
        return sent, get_push_registry().subscriber_count()

    sent, remaining = asyncio.run(scenario())

    start = [m for m in sent if m.get("type") == "http.response.start"]
    assert start and start[0]["status"] == 200
    headers = {k.decode().lower(): v.decode() for k, v in start[0].get("headers", [])}
    assert "text/event-stream" in headers.get("content-type", ""), headers

    payloads = [
        m["body"].decode("utf-8") for m in sent
        if m.get("type") == "http.response.body" and m.get("body", b"").strip()
    ]
    data_frames = [p for p in payloads if "data:" in p]
    assert data_frames, f"未收到任何 SSE data 帧；已发消息={[m.get('type') for m in sent]}"

    frame = data_frames[0]
    assert "event: " in frame and "id: e2e-1" in frame, frame[:200]
    body = json.loads(frame.split("data:", 1)[1].strip().splitlines()[0])
    assert body["request_id"] == "e2e-1"

    assert remaining == 0, "客户端断开后订阅者必须被摘除，否则 registry 会堆积死连接"


def test_ws_unique_subscriber_ids_survive_short_connection_disconnect() -> None:
    """短连接断开不得清空仍存活长连接的推送订阅（旧固定 gateway-ws 单槽的根因）。"""
    from jiuwenswarm.server.transports.push_registry import get_push_registry

    reg = get_push_registry()
    long_lived, short_lived = _RecordingSink(), _RecordingSink()
    long_id, short_id = "gateway-ws:long", "gateway-ws:short"
    reg.register(long_id, long_lived, drop_on_stall=False)
    reg.register(short_id, short_lived, drop_on_stall=False)
    try:
        assert reg.subscriber_count() == 2
        # 短连接断开只清自己
        reg.unregister(short_id)
        assert reg.subscriber_count() == 1
        asyncio.run(reg.push({"request_id": "r1"}))
    finally:
        reg.unregister(long_id)
        reg.unregister(short_id)

    assert short_lived.wires == [], "已断开的短连接不应再收到推送"
    assert len(long_lived.wires) == 1, "长连接在短连接断开后仍应收到 send_push"


def test_ws_push_sink_keeps_connection_registered_when_send_fails() -> None:
    from jiuwenswarm.server.agent_ws_server import _GatewayWSPushSink
    from jiuwenswarm.server.transports.push_registry import get_push_registry

    class _BoomWs:
        async def send(self, _payload):
            raise RuntimeError("socket 断了")

    reg = get_push_registry()
    sub_id = "gateway-ws:boom-test"
    reg.register(sub_id, _GatewayWSPushSink(_BoomWs(), asyncio.Lock()), drop_on_stall=False)
    try:
        delivered = asyncio.run(reg.push({"request_id": "r1"}))
        assert delivered == 0, "发送失败不应计入送达数"
        assert reg.subscriber_count() == 1, (
            "发送失败不得注销 WS 订阅者 —— 否则一次瞬时失败会让 Gateway 直到重连才恢复推送"
        )
    finally:
        reg.unregister(sub_id)


def test_all_sinks_share_one_send_budget_semantics() -> None:
    from jiuwenswarm.common.ws_limits import AGENT_WS_SEND_BUDGET_BYTES
    from jiuwenswarm.server.transports.sink import SSESink, UnaryHTTPSink, WSSink

    small = {"request_id": "ok", "payload": {"a": "x"}}
    huge = {"request_id": "big", "payload": {"a": "x" * (AGENT_WS_SEND_BUDGET_BYTES + 1000)}}

    class _Ws:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, text: str) -> None:
            self.sent.append(text)

    async def _run(sink, wire):
        return await sink.send_wire(wire)

    for name, make in (
        ("WSSink", lambda: WSSink(_Ws(), asyncio.Lock())),
        ("UnaryHTTPSink", UnaryHTTPSink),
        ("SSESink", SSESink),
    ):
        assert asyncio.run(_run(make(), small)) is True, f"{name}: 正常帧应返回 True"
        assert asyncio.run(_run(make(), huge)) is False, (
            f"{name}: 超预算帧必须返回 False（已降级），否则流式 handler 不会中止后续发送"
        )


def test_all_sinks_enforce_budget_on_object_entrypoints() -> None:
    from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
    from jiuwenswarm.common.ws_limits import AGENT_WS_SEND_BUDGET_BYTES
    from jiuwenswarm.server.transports.sink import SSESink, UnaryHTTPSink, WSSink

    huge = "x" * (AGENT_WS_SEND_BUDGET_BYTES + 1000)

    class _Ws:
        async def send(self, text: str) -> None:
            pass

    def _resp(payload):
        return AgentResponse(request_id="r", channel_id="web", ok=True, payload=payload)

    def _chunk(payload):
        return AgentResponseChunk(request_id="r", channel_id="web", payload=payload)

    for name, make in (
        ("WSSink", lambda: WSSink(_Ws(), asyncio.Lock())),
        ("UnaryHTTPSink", UnaryHTTPSink),
        ("SSESink", SSESink),
    ):
        assert asyncio.run(make().send_unary(_resp({"a": "x"}))) is True, (
            f"{name}.send_unary: 正常帧应返回 True"
        )
        assert asyncio.run(make().send_unary(_resp({"a": huge}))) is False, (
            f"{name}.send_unary: 超预算帧必须返回 False（已降级）"
        )
        assert asyncio.run(make().send_chunk(_chunk({"a": "x"}), sequence=0)) is True, (
            f"{name}.send_chunk: 正常帧应返回 True"
        )
        assert asyncio.run(make().send_chunk(_chunk({"a": huge}), sequence=0)) is False, (
            f"{name}.send_chunk: 超预算帧必须返回 False，否则流式 handler 不会中止"
        )


def test_oversized_frame_is_replaced_not_passed_through() -> None:
    from jiuwenswarm.common.ws_limits import AGENT_WS_SEND_BUDGET_BYTES
    from jiuwenswarm.server.transports.sink import SSESink, UnaryHTTPSink

    huge = {"request_id": "big", "payload": {"a": "x" * (AGENT_WS_SEND_BUDGET_BYTES + 1000)}}

    unary = UnaryHTTPSink()
    assert asyncio.run(unary.send_wire(huge)) is False
    assert unary.wire != huge, "UnaryHTTPSink 应记下降级帧"
    assert "max_bytes" in json.dumps(unary.wire, ensure_ascii=False)

    sse = SSESink()
    assert asyncio.run(sse.send_wire(huge)) is False
    queued = sse.queue.get_nowait()
    assert queued != huge, "SSESink 应入队降级帧"
    assert "max_bytes" in json.dumps(queued, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 背压路径：慢/死的 SSE 消费者不得拖垮 handler 收尾与全局推送
#
# 这三条覆盖的是「队列填满」分支 —— 常规测试从不填满 maxsize，
# 于是这条路径在很长一段时间里一次都没被执行过。
# ---------------------------------------------------------------------------
def test_sse_finish_does_not_block_when_consumer_is_gone(monkeypatch) -> None:
    """队列满且无人消费时，``finish()`` 必须有界返回，不能永久阻塞。

    这是 handler 收尾的最后一步，而它执行的时机恰恰是「客户端已断开」：
    若在此阻塞，``iter_stream`` 的 ``await task`` 一并挂死、任务永久泄漏。
    """
    from jiuwenswarm.server.transports import sink as sink_mod

    monkeypatch.setattr(sink_mod, "FINISH_TIMEOUT", 0.05, raising=False)

    async def scenario() -> None:
        sse = sink_mod.SSESink(maxsize=2)
        await sse.queue.put({"a": 1})
        await sse.queue.put({"a": 2})          # 填满，且没有消费者
        await asyncio.wait_for(sse.finish(), timeout=2.0)

    asyncio.run(scenario())                     # 超时即测试失败


def test_sse_finish_still_delivers_sentinel_when_consumer_alive() -> None:
    """消费者正常时哨兵必须照常送达 —— 防止上一条的修复退化成「总是丢哨兵」。"""
    from jiuwenswarm.server.transports.sink import STREAM_DONE, SSESink

    async def scenario():
        sse = SSESink(maxsize=2)
        await sse.finish()
        return await sse.queue.get()

    assert asyncio.run(scenario()) is STREAM_DONE


def test_push_is_not_blocked_by_stalled_subscriber(monkeypatch) -> None:
    """一个停滞的 SSE 订阅者不得卡住整轮扇出。

    ``SSESink.send_wire`` 是 ``await queue.put``，队列满即阻塞；纯 try/except
    只兜异常、兜不住阻塞。没有超时的话，排在停滞者之后的订阅者一条都收不到，
    调用方（cron / send_file_to_user / proactive）的协程也一并卡死。
    """
    from jiuwenswarm.server.transports import push_registry as pr_mod
    from jiuwenswarm.server.transports.sink import SSESink

    monkeypatch.setattr(pr_mod, "SEND_TIMEOUT", 0.05, raising=False)

    async def scenario() -> tuple[int, int, int]:
        reg = pr_mod.PushRegistry()
        stalled = SSESink(maxsize=1)
        await stalled.queue.put({"filler": True})   # 消费者停滞、队列已满
        healthy = _RecordingSink()
        reg.register("stalled-sse", stalled)
        reg.register("healthy", healthy)
        delivered = await asyncio.wait_for(reg.push({"request_id": "p1"}), timeout=3.0)
        return delivered, len(healthy.wires), reg.subscriber_count()

    delivered, healthy_got, remaining = asyncio.run(scenario())
    assert healthy_got == 1, "停滞订阅者不得让健康订阅者收不到推送"
    assert delivered == 1
    assert remaining == 1, "停滞的订阅者应被就地注销，避免每轮都再等一次超时"


def test_events_stream_registers_only_after_generator_starts() -> None:
    """订阅必须在生成器**体内**注册，与 finally 的注销成对。

    注册若发生在生成器体外，客户端在响应开始推送前就断开时，异步生成器一次都
    没被迭代过 —— 按 PEP 525 语义此时生成器体不执行，``finally`` 里的注销自然
    也不执行，订阅者就永久留在注册表里，其队列无人消费直至填满。
    """
    from starlette.requests import Request

    from jiuwenswarm.server.agent_http_server import API_PREFIX
    from jiuwenswarm.server.transports.push_registry import get_push_registry

    from jiuwenswarm.server.agent_http_server import AgentHTTPServer

    server = AgentHTTPServer.__new__(AgentHTTPServer)
    server._ws_server = None  # type: ignore[attr-defined]
    app = server.build_app()
    target = f"{API_PREFIX}/events/stream"
    endpoint = next(
        r.endpoint for r in app.routes
        if getattr(r, "path", None) == target and "GET" in (getattr(r, "methods", None) or ())
    )

    registry = get_push_registry()
    before = registry.subscriber_count()

    scope = {
        "type": "http", "method": "GET", "path": target,
        "headers": [], "query_string": b"", "path_params": {},
    }
    response = asyncio.run(endpoint(Request(scope)))

    assert registry.subscriber_count() == before, (
        "构造响应阶段就注册了订阅者 —— 客户端若在首次推送前断开，"
        "生成器体不会执行，注销也就永远不会发生（订阅者永久泄漏）。"
    )
    assert response is not None


def test_ws_subscriber_is_never_dropped_on_slow_send(monkeypatch) -> None:
    """WS 订阅者**不得**因发送慢被注销 —— 这是 ``_GatewayWSPushSink`` 的既有契约。

    给推送投递加超时后，WS 侧必须整体豁免（``drop_on_stall=False``）——
    否则一次慢发送（大帧、背压、排在连接级 ``send_lock`` 后面）就会触发注销，
    导致 ``send_file`` / ``chat.file`` 推送静默丢失。
    """
    from jiuwenswarm.server.transports import push_registry as pr_mod

    monkeypatch.setattr(pr_mod, "SEND_TIMEOUT", 0.05, raising=False)

    class _SlowButAliveSink:
        """慢，但最终会成功；且和 _GatewayWSPushSink 一样从不抛异常。"""

        def __init__(self) -> None:
            self.sent = 0

        async def send_wire(self, wire: dict) -> bool:
            await asyncio.sleep(0.3)
            self.sent += 1
            return True

    async def scenario() -> tuple[int, int]:
        reg = pr_mod.PushRegistry()
        sink = _SlowButAliveSink()
        reg.register("gateway-ws:slow", sink, drop_on_stall=False)
        delivered = await reg.push({"request_id": "p1"})
        return delivered, reg.subscriber_count()

    delivered, remaining = asyncio.run(scenario())
    assert remaining == 1, "WS 订阅者不得因慢发送被注销"
    assert delivered == 1, "慢发送最终成功时应计入送达，不能因超时被丢弃"


def test_ws_registration_opts_out_of_stall_drop() -> None:
    """WS 的两个注册点都必须显式传 ``drop_on_stall=False``。

    光靠 registry 支持该参数不够 —— 调用方漏传就会退回默认的"超时即注销"。
    """
    import inspect

    from jiuwenswarm.extensions import clawee
    from jiuwenswarm.server import agent_ws_server

    for module in (agent_ws_server, clawee):
        src = inspect.getsource(module)
        assert "drop_on_stall=False" in src, (
            f"{module.__name__} 注册 gateway-ws 时未传 drop_on_stall=False —— "
            f"慢发送会把 Gateway 踢出推送名单。"
        )
        assert "make_ws_push_subscriber_id" in src, (
            f"{module.__name__} 必须使用每连接唯一订阅 id，"
            f"避免短连接断开清空长连接推送槽。"
        )


def test_make_ws_push_subscriber_id_is_unique_per_ws_object() -> None:
    from jiuwenswarm.server.transports.push_registry import make_ws_push_subscriber_id

    a, b = object(), object()
    assert make_ws_push_subscriber_id(a) != make_ws_push_subscriber_id(b)
    assert make_ws_push_subscriber_id(a).startswith("gateway-ws:")


def test_send_push_returns_zero_without_subscribers() -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.transports.push_registry import get_push_registry

    assert get_push_registry().subscriber_count() == 0
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    delivered = asyncio.run(
        server.send_push({"request_id": "r1", "channel_id": "web", "payload": {}})
    )
    assert delivered == 0
