# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers.session import _coerce_int, handle_session_list
from jiuwenswarm.server.transports.sink import UnaryHTTPSink


def _ctx(params: dict | None = None) -> tuple[RequestContext, UnaryHTTPSink]:
    sink = UnaryHTTPSink()
    request = AgentRequest(
        request_id="r1",
        channel_id="web",
        req_method=ReqMethod.SESSION_LIST,
        params=params if params is not None else {},
    )
    return RequestContext(request=request, sink=sink, connection_id="c1"), sink


def _run(ctx: RequestContext) -> None:
    asyncio.run(handle_session_list(ctx))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (7, 7),
        (7.0, 7),
        ("7", 7),
        ("  7 ", 7),
        (True, 20),      # bool 不算 int（与原实现一致）
        (7.5, 20),       # 非整数 float 走默认
        ("abc", 20),
        (None, 20),
        ([], 20),
    ],
)
def test_coerce_int_matches_legacy_rules(raw: object, expected: int) -> None:
    assert _coerce_int(raw, 20) == expected


def test_limit_defaults_to_20(monkeypatch) -> None:
    captured = {}

    def fake(limit: int, offset: int, sessions_root=None):
        captured.update(limit=limit, offset=offset)
        return [], 0

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_all_sessions_metadata", fake
    )
    ctx, _ = _ctx()
    _run(ctx)
    assert captured == {"limit": 20, "offset": 0}


@pytest.mark.parametrize(
    ("given", "clamped"),
    [(0, 1), (-5, 1), (500, 200), (200, 200), (1, 1)],
)
def test_limit_is_clamped(monkeypatch, given: int, clamped: int) -> None:
    captured = {}
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_all_sessions_metadata",
        lambda limit, offset, sessions_root=None: (captured.update(limit=limit) or ([], 0)),
    )
    ctx, _ = _ctx({"limit": given})
    _run(ctx)
    assert captured["limit"] == clamped


def test_negative_offset_clamped_to_zero(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_all_sessions_metadata",
        lambda limit, offset, sessions_root=None: (captured.update(offset=offset) or ([], 0)),
    )
    ctx, _ = _ctx({"offset": -3})
    _run(ctx)
    assert captured["offset"] == 0


# ---------------------------------------------------------------- 响应契约
def test_response_payload_shape(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_all_sessions_metadata",
        lambda limit, offset, sessions_root=None: ([{"session_id": "s1"}], 42),
    )
    ctx, sink = _ctx({"limit": 5, "offset": 10})
    _run(ctx)
    assert sink.response is not None
    assert sink.response.ok is True
    assert sink.response.payload == {
        "sessions": [{"session_id": "s1"}],
        "total": 42,
        "limit": 5,
        "offset": 10,
    }
    assert sink.response.request_id == "r1"
    assert sink.response.channel_id == "web"


def test_metadata_store_failure_degrades_to_empty(monkeypatch) -> None:

    def boom(limit: int, offset: int, sessions_root=None):
        raise RuntimeError("store down")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_all_sessions_metadata", boom
    )
    ctx, sink = _ctx()
    _run(ctx)
    assert sink.response is not None
    assert sink.response.ok is True
    assert sink.response.payload["sessions"] == []
    assert sink.response.payload["total"] == 0


def test_non_dict_params_tolerated(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_all_sessions_metadata",
        lambda limit, offset, sessions_root=None: ([], 0),
    )
    request = AgentRequest(
        request_id="r1", channel_id="web", req_method=ReqMethod.SESSION_LIST, params="not-a-dict"
    )
    sink = UnaryHTTPSink()
    _run(RequestContext(request=request, sink=sink, connection_id="c1"))
    assert sink.response is not None and sink.response.ok is True


# ---------------------------------------------------------------------------
# session.switch 的互斥：锁必须真的锁住，两个传输行为一致
# ---------------------------------------------------------------------------
class _SwitchProbe:
    """记录临界区进出深度，>1 即说明互斥失效。"""

    def __init__(self) -> None:
        self.depth = 0
        self.max_depth = 0

    async def prepare_session_switch_owner(self, **_kw):
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        await asyncio.sleep(0.02)          # 制造重叠窗口
        self.depth -= 1
        return (False, "agent", None, None, None)

    async def dispatch_session_switch_kvc(self, **_kw):
        return None


def _switch_ctx(connection_id: str, probe: _SwitchProbe) -> RequestContext:
    from jiuwenswarm.server.transports.sink import UnaryHTTPSink

    request = AgentRequest(
        request_id=f"r-{connection_id}",
        channel_id="web",
        session_id="sess-target",
        req_method=ReqMethod.SESSION_SWITCH,
        params={"session_id": "sess-target", "previous_session_id": "sess-old"},
    )
    return RequestContext(
        request=request,
        sink=UnaryHTTPSink(),
        connection_id=connection_id,
        services=probe,  # type: ignore[arg-type]
    )


def _max_concurrency(connection_ids: tuple[str, str]) -> int:
    from jiuwenswarm.server.handlers.session import (
        _session_switch_locks,
        handle_session_switch,
    )

    _session_switch_locks.clear()
    probe = _SwitchProbe()

    async def scenario() -> None:
        await asyncio.gather(
            *(handle_session_switch(_switch_ctx(cid, probe)) for cid in connection_ids)
        )

    asyncio.run(scenario())
    return probe.max_depth


def _http_connection_id() -> str:
    """取 HTTP 传输层【实际】给业务层的 connection_id。

    刻意不读常量，而是走真实构造路径：这样测的是"传输层产出什么"，
    而不是"我们希望它产出什么"。
    """
    from jiuwenswarm.server.agent_http_server import AgentHTTPServer
    from jiuwenswarm.server.transports.sink import UnaryHTTPSink

    server = AgentHTTPServer.__new__(AgentHTTPServer)
    server._ws_server = None  # type: ignore[attr-defined]
    request = AgentRequest(
        request_id="probe",
        channel_id="web",
        req_method=ReqMethod.SESSION_SWITCH,
        params={},
    )
    # 两个不同的 sink —— 模拟两个独立的 HTTP 请求
    return (
        server._make_ctx(UnaryHTTPSink(), request).connection_id,
        server._make_ctx(UnaryHTTPSink(), request).connection_id,
    )


def test_session_switch_serializes_within_one_connection() -> None:
    """WS 基准：同一连接上的快速导航必须串行（既有行为，不得回归）。"""
    assert _max_concurrency(("ws-1", "ws-1")) == 1


def test_http_connection_id_is_stable_across_requests() -> None:
    """HTTP 传输层必须给出【跨请求稳定】的 connection_id。

    取 ``id(sink)`` / ``request_id`` 这类每请求唯一的值会让所有按 connection_id
    分槽的逻辑静默失效 —— 首当其冲是 ``session.switch`` 的切换锁。
    """
    first, second = _http_connection_id()
    assert first == second, (
        f"HTTP 两个请求拿到了不同的 connection_id（{first!r} vs {second!r}）——"
        f" 按连接分槽的逻辑会退化成零互斥，见 handlers/session.py 的切换锁。"
    )


def test_session_switch_serializes_across_http_requests() -> None:
    """端到端：用传输层【真实产出】的 connection_id 验证互斥生效，与 WS 对齐。"""
    first, second = _http_connection_id()
    assert _max_concurrency((first, second)) == 1


def test_make_ctx_tolerates_missing_request() -> None:
    """``_make_ctx`` 必须能在 ``request=None`` 下构造成功。

    ``dispatch_raw_envelope`` 解析失败时正是这样调它（``agent_http_server`` 的
    ``_make_ctx(sink, None).sink.send_wire(...)``），用于把错误帧写回客户端。
    而 ``parse_inbound`` 的失败不止 JSON 坏了 —— **未知方法名同样算失败**，
    所以 ``POST /api/v1/e2a`` 带个写错的方法名就会走到这里。
    若 ``connection_id`` 改成从 ``request`` 推导（如 ``request.channel_id``），
    这里会抛 AttributeError，客户端拿到 500 而不是应有的错误帧。
    """
    from jiuwenswarm.server.agent_http_server import AgentHTTPServer
    from jiuwenswarm.server.transports.sink import UnaryHTTPSink

    server = AgentHTTPServer.__new__(AgentHTTPServer)
    server._ws_server = None  # type: ignore[attr-defined]
    ctx = server._make_ctx(UnaryHTTPSink(), None)
    assert isinstance(ctx.connection_id, str) and ctx.connection_id
