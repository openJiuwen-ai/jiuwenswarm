# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.e2a.wire_codec import encode_agent_chunk_for_wire
from jiuwenswarm.common.schema.agent import AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.request_ext import INTERNAL_HEADER_NAME, decode_internal_header
from jiuwenswarm.gateway.routing.agent_rest_map import (
    RestAssemblyError,
    assemble_rest_request,
)
from jiuwenswarm.gateway.routing.http_agent_client import (
    HttpSseAgentServerClient,
    http_unary_to_agent_response,
    iter_sse_data_frames,
)


def test_assemble_chat_send_uses_completions_and_params_body_only():
    env = e2a_from_agent_fields(
        request_id="r1",
        channel_id="web",
        session_id="s1",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hi", "session_id": "s1"},
        is_stream=True,
        user_id="u1",
    )
    assembled = assemble_rest_request(env, base_url="http://127.0.0.1:8766")
    assert assembled.verb == "POST"
    assert assembled.url == "http://127.0.0.1:8766/api/v1/chat/completions"
    assert assembled.json_body == {"query": "hi", "session_id": "s1"}
    assert assembled.headers["X-Request-Id"] == "r1"
    assert assembled.headers["X-Session-Id"] == "s1"
    assert assembled.headers["X-User-Id"] == "u1"
    assert assembled.headers["Accept"] == "text/event-stream"
    assert assembled.used_rpc_fallback is False
    assert assembled.json_body is not None
    assert "method" not in assembled.json_body
    assert "request_id" not in assembled.json_body


def test_assemble_rest_request_carries_request_ext_in_internal_header():
    env = e2a_from_agent_fields(
        request_id="r-ext",
        channel_id="web",
        session_id="s-ext",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hi"},
        is_stream=True,
        user_id="u1",
    )
    env.channel_context["ext"] = {
        "tenant": "租户-a",
        "custom_trace": "trace-1",
    }

    assembled = assemble_rest_request(env, base_url="http://127.0.0.1:8766")

    assert decode_internal_header(assembled.headers[INTERNAL_HEADER_NAME]) == {
        "tenant": "租户-a",
        "custom_trace": "trace-1",
    }
    assert "ext" not in (assembled.json_body or {})


def test_assemble_session_rename_fills_path_and_drops_used_keys():
    env = e2a_from_agent_fields(
        request_id="r2",
        channel_id="web",
        session_id="sess-9",
        req_method=ReqMethod.SESSION_RENAME,
        params={"title": "new"},
        is_stream=False,
    )
    assembled = assemble_rest_request(env, base_url="http://127.0.0.1:8766")
    assert assembled.verb == "PATCH"
    assert assembled.url == "http://127.0.0.1:8766/api/v1/sessions/sess-9"
    assert assembled.json_body == {"title": "new"}


def test_assemble_session_create_strips_session_id_from_body():
    """Agent rejects params.session_id on create; envelope sid must not leak into body."""
    env = e2a_from_agent_fields(
        request_id="r-create",
        channel_id="web",
        session_id="sess_temp_client_id",
        req_method=ReqMethod.SESSION_CREATE,
        params={
            "create_token": "tok-1",
            "mode": "agent",
            "session_id": "sess_temp_client_id",
            "title": "new chat",
        },
        is_stream=False,
    )
    assembled = assemble_rest_request(env, base_url="http://127.0.0.1:8766")
    assert assembled.verb == "POST"
    assert assembled.url == "http://127.0.0.1:8766/api/v1/sessions"
    assert assembled.json_body is not None
    assert "session_id" not in assembled.json_body
    assert assembled.json_body["create_token"] == "tok-1"
    assert assembled.json_body["mode"] == "agent"
    # 身份仍可上头，不进 params
    assert assembled.headers.get("X-Session-Id") == "sess_temp_client_id"


def test_assemble_history_stream_uses_stream_path_and_query():
    env = e2a_from_agent_fields(
        request_id="r3",
        channel_id="web",
        session_id="s1",
        req_method=ReqMethod.HISTORY_GET,
        params={"limit": 20},
        is_stream=True,
    )
    assembled = assemble_rest_request(env, base_url="http://127.0.0.1:8766")
    assert assembled.verb == "GET"
    assert assembled.url.endswith("/sessions/s1/history/stream")
    assert assembled.query == {"limit": "20"}
    assert assembled.json_body is None


def test_assemble_unknown_method_falls_back_to_rpc():
    env = e2a_from_agent_fields(
        request_id="r4",
        channel_id="web",
        session_id="s1",
        req_method=ReqMethod.TEAM_SESSION_RESET,
        params={"hard": True},
        is_stream=False,
    )
    assembled = assemble_rest_request(env, base_url="http://127.0.0.1:8766")
    assert assembled.used_rpc_fallback is True
    assert assembled.url.endswith("/rpc/team.session.reset")
    assert assembled.json_body == {"hard": True, "session_id": "s1"}


def test_assemble_missing_path_placeholder_raises():
    env = e2a_from_agent_fields(
        request_id="r5",
        channel_id="web",
        req_method=ReqMethod.AGENTS_GET,
        params={},
        is_stream=False,
    )
    with pytest.raises(RestAssemblyError):
        assemble_rest_request(env, base_url="http://127.0.0.1:8766")


def test_http_unary_unwraps_data_result_and_error_details_code():
    ok = http_unary_to_agent_response(
        {
            "request_id": "r1",
            "ok": True,
            "data": {"result": {"title": "ok"}},
            "metadata": {"k": 1},
        },
        channel_id="web",
        request_id="fallback",
    )
    assert ok.ok is True
    assert ok.payload == {"title": "ok"}
    assert ok.metadata == {"k": 1}

    bad = http_unary_to_agent_response(
        {
            "request_id": "r2",
            "ok": False,
            "error": {
                "code": "E2A.AGENT_ERROR",
                "message": "Agent error",
                "details": {"code": "NOT_FOUND", "error": "missing"},
            },
        },
        channel_id="web",
        request_id="r2",
    )
    assert bad.ok is False
    assert bad.payload["code"] == "E2A.AGENT_ERROR"
    assert bad.payload["error"] == "Agent error"


@pytest.mark.asyncio
async def test_connect_rejects_websocket_url():
    client = HttpSseAgentServerClient()
    with pytest.raises(RuntimeError, match="http\\(s\\)"):
        await client.connect("ws://127.0.0.1:18092")


def _sse_blob(wire: dict) -> bytes:
    return f"event: e2a.chunk\ndata: {json.dumps(wire, ensure_ascii=False)}\n\n".encode("utf-8")


def _http_json(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return (
        f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode("ascii") + body


async def _start_interrupt_stub():
    interrupt_received = asyncio.Event()
    interrupt_paths: list[str] = []

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            request_line = header.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            _method, path, *_rest = request_line.split(" ")
            content_length = 0
            for raw in header.split(b"\r\n"):
                if raw.lower().startswith(b"content-length:"):
                    content_length = int(raw.split(b":", 1)[1].strip() or 0)
            if content_length:
                await reader.readexactly(content_length)

            if path.endswith("/health"):
                writer.write(
                    _http_json({"ok": True, "data": {"status": "ready"}, "request_id": "h1"})
                )
            elif path.endswith("/actions/interrupt"):
                interrupt_paths.append(path)
                interrupt_received.set()
                writer.write(
                    _http_json(
                        {
                            "request_id": "int-1",
                            "ok": True,
                            "data": {"accepted": True},
                            "metadata": {},
                        }
                    )
                )
            elif path.endswith("/chat/completions"):
                delta = encode_agent_chunk_for_wire(
                    AgentResponseChunk(
                        request_id="chat-1",
                        channel_id="web",
                        payload={"content": "hi", "event_type": "chat.delta"},
                        is_complete=False,
                    ),
                    response_id="chat-1",
                    sequence=0,
                )
                done = encode_agent_chunk_for_wire(
                    AgentResponseChunk(
                        request_id="chat-1",
                        channel_id="web",
                        payload={"is_complete": True},
                        is_complete=True,
                    ),
                    response_id="chat-1",
                    sequence=1,
                )
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n"
                )
                writer.write(_sse_blob(delta))
                await writer.drain()
                await asyncio.wait_for(interrupt_received.wait(), timeout=5)
                writer.write(_sse_blob(done))
            else:
                writer.write(_http_json({"ok": False, "error": {"code": "NOT_FOUND"}}))
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, interrupt_received, interrupt_paths


@pytest.mark.asyncio
async def test_interrupt_posts_while_sse_still_open():
    server, port, interrupt_received, interrupt_paths = await _start_interrupt_stub()
    base = f"http://127.0.0.1:{port}"
    client = HttpSseAgentServerClient()
    try:
        await client.connect(base)
        chat_env = e2a_from_agent_fields(
            request_id="chat-1",
            channel_id="web",
            session_id="sess-1",
            req_method=ReqMethod.CHAT_SEND,
            params={"query": "hi"},
            is_stream=True,
        )
        interrupt_env = e2a_from_agent_fields(
            request_id="int-1",
            channel_id="web",
            session_id="sess-1",
            req_method=ReqMethod.CHAT_CANCEL,
            params={"intent": "cancel"},
            is_stream=False,
        )
        chunks: list[AgentResponseChunk] = []
        async for chunk in client.send_request_stream(chat_env):
            chunks.append(chunk)
            if len(chunks) == 1:
                resp = await client.send_request(interrupt_env)
                assert resp.ok is True
        assert interrupt_received.is_set()
        assert interrupt_paths == ["/api/v1/chat/sess-1/actions/interrupt"]
        assert len(chunks) >= 2
    finally:
        await client.disconnect()
        server.close()
        await server.wait_closed()


def test_default_agent_url_http_when_type_http(monkeypatch):
    monkeypatch.setenv("AGENT_SERVER_HOST", "127.0.0.1")
    monkeypatch.delenv("AGENT_HTTP_PORT", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.gateway.app_gateway._gateway_agent_client_type",
        lambda: "http",
    )
    from jiuwenswarm.gateway.app_gateway import _default_agent_server_url

    assert _default_agent_server_url() == "http://127.0.0.1:8766"


def test_default_agent_url_http_honors_port_env(monkeypatch):
    monkeypatch.setenv("AGENT_SERVER_HOST", "10.0.0.2")
    monkeypatch.setenv("AGENT_HTTP_PORT", "9999")
    monkeypatch.setattr(
        "jiuwenswarm.gateway.app_gateway._gateway_agent_client_type",
        lambda: "http",
    )
    from jiuwenswarm.gateway.app_gateway import _default_agent_server_url

    assert _default_agent_server_url() == "http://10.0.0.2:9999"


def test_default_agent_url_websocket_when_type_default(monkeypatch):
    monkeypatch.setenv("AGENT_SERVER_HOST", "127.0.0.1")
    monkeypatch.delenv("AGENT_SERVER_PORT", raising=False)
    monkeypatch.delenv("AGENT_PORT", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.gateway.app_gateway._gateway_agent_client_type",
        lambda: "websocket",
    )
    from jiuwenswarm.gateway.app_gateway import _default_agent_server_url

    assert _default_agent_server_url() == "ws://127.0.0.1:18092"


@pytest.mark.parametrize(
    ("payload", "expect_ok", "expect_payload"),
    [
        (
            {"request_id": "r1", "ok": True, "data": {"result": {"title": "ok"}}},
            True,
            {"title": "ok"},
        ),
        (
            {"request_id": "r1", "ok": True, "data": {"sessions": []}},
            True,
            {"sessions": []},
        ),
        ({"request_id": "r1", "ok": True, "data": None}, True, {}),
        ({"request_id": "r1", "ok": True, "data": "hello"}, True, {"content": "hello"}),
        ({"ok": True}, True, {}),
        (
            {
                "request_id": "r2",
                "ok": False,
                "error": {"code": "NOT_FOUND", "message": "missing", "details": {"k": 1}},
            },
            False,
            {"error": "missing", "code": "NOT_FOUND", "k": 1},
        ),
        (
            {
                "ok": False,
                "error": {
                    "message": "Agent error",
                    "details": {"code": "E2A.AGENT_ERROR", "error": "inner"},
                },
            },
            False,
            {"error": "Agent error", "code": "E2A.AGENT_ERROR"},
        ),
        ({"ok": False}, False, {"error": None, "code": None}),
    ],
)
def test_http_unary_unwrap_shapes(payload, expect_ok, expect_payload):
    resp = http_unary_to_agent_response(payload, channel_id="web", request_id="fallback")
    assert resp.ok is expect_ok
    assert resp.channel_id == "web"
    for key, value in expect_payload.items():
        assert resp.payload.get(key) == value
    if payload.get("request_id"):
        assert resp.request_id == payload["request_id"]
    else:
        assert resp.request_id == "fallback"


class _LineResponse:
    def __init__(self, text: str) -> None:
        self._text = text

    async def aiter_lines(self):
        for line in self._text.split("\n"):
            yield line


@pytest.mark.asyncio
async def test_iter_sse_skips_keepalive_event_and_bad_json():
    blob = (
        ": keepalive\n"
        "\n"
        "event: e2a.chunk\n"
        "data: not-json\n"
        "\n"
        "data: [1,2]\n"
        "\n"
        "data: {\"a\": 1}\n"
        "\n"
        "data: {\"ok\": true, \"n\": 2}\n"
        "\n"
        "data: {\"trail\": true}\n"
    )
    frames = [frame async for frame in iter_sse_data_frames(_LineResponse(blob))]
    assert frames == [{"a": 1}, {"ok": True, "n": 2}, {"trail": True}]


@pytest.mark.asyncio
async def test_iter_sse_joins_multiline_data():
    blob = "data: {\n" 'data: "ok": true\n' "data: }\n" "\n"
    frames = [frame async for frame in iter_sse_data_frames(_LineResponse(blob))]
    assert frames == [{"ok": True}]


def _health_ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "data": {"status": "ready"}})


def _chunk_wire(request_id: str, *, complete: bool, content: str = "hi") -> dict:
    return encode_agent_chunk_for_wire(
        AgentResponseChunk(
            request_id=request_id,
            channel_id="web",
            payload={"content": content, "event_type": "chat.delta"}
            if not complete
            else {"is_complete": True},
            is_complete=complete,
        ),
        response_id=request_id,
        sequence=1 if complete else 0,
    )


async def _connected(handler) -> HttpSseAgentServerClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = HttpSseAgentServerClient(http_client=http)
    await client.connect("http://127.0.0.1:8766")
    return client


@pytest.mark.asyncio
async def test_connect_rejects_wss_and_empty_scheme():
    client = HttpSseAgentServerClient()
    with pytest.raises(RuntimeError, match="http\\(s\\)"):
        await client.connect("wss://127.0.0.1:18092")
    with pytest.raises(RuntimeError, match="http\\(s\\)"):
        await client.connect("127.0.0.1:8766")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "health",
    [
        lambda _r: httpx.Response(503, json={"ok": False, "error": {"code": "DOWN"}}),
        lambda _r: httpx.Response(200, json={"ok": False, "data": {"status": "starting"}}),
        lambda _r: httpx.Response(200, json={"status": "ready"}),
        lambda _r: httpx.Response(200, content=b"not-json"),
        lambda _r: httpx.Response(200, json=["nope"]),
    ],
)
async def test_connect_health_failure_does_not_mark_ready(health):
    client = HttpSseAgentServerClient(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(health))
    )
    with pytest.raises(RuntimeError, match="health"):
        await client.connect("http://127.0.0.1:8766")
    assert client.server_ready is False


@pytest.mark.asyncio
async def test_connect_https_health_ok():
    client = await _connected(_health_ok)
    try:
        assert client.server_ready is True
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_send_before_connect_raises():
    client = HttpSseAgentServerClient()
    env = e2a_from_agent_fields(
        request_id="r1",
        channel_id="web",
        req_method=ReqMethod.SESSION_LIST,
        params={},
    )
    with pytest.raises(RuntimeError, match="未连接"):
        await client.send_request(env)


@pytest.mark.asyncio
async def test_send_request_unary_success_and_non_json():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/health"):
            return _health_ok(request)
        if request.url.path.endswith("/sessions"):
            return httpx.Response(
                200,
                json={
                    "request_id": "t-list",
                    "ok": True,
                    "data": {"result": {"sessions": [{"id": "s1"}]}},
                    "metadata": {"k": 1},
                },
            )
        return httpx.Response(200, content=b"<html>nope</html>")

    client = await _connected(handler)
    try:
        ok = await client.send_request(
            e2a_from_agent_fields(
                request_id="t-list",
                channel_id="web",
                session_id="s1",
                req_method=ReqMethod.SESSION_LIST,
                params={"limit": 3},
                is_stream=True,
            )
        )
        assert ok.ok is True
        assert ok.payload == {"sessions": [{"id": "s1"}]}
        assert ok.metadata == {"k": 1}
        list_req = [r for r in seen if r.url.path.endswith("/sessions")][0]
        assert list_req.method == "GET"
        assert list_req.headers["x-request-id"] == "t-list"
        assert list_req.headers["accept"] == "application/json"

        bad = await client.send_request(
            e2a_from_agent_fields(
                request_id="t-init",
                channel_id="web",
                req_method=ReqMethod.INITIALIZE,
                params={},
            )
        )
        assert bad.ok is False
        assert bad.payload["code"] == "HTTP_ERROR"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_send_request_stream_yields_chunks_and_skips_garbage():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return _health_ok(request)
        good = _chunk_wire("chat-1", complete=False)
        done = _chunk_wire("chat-1", complete=True)
        body = (
            b": keepalive\n\n"
            b"event: e2a.chunk\n"
            b"data: {not json}\n\n"
            + f"data: {json.dumps(good, ensure_ascii=False)}\n\n".encode("utf-8")
            + f"data: {json.dumps(done, ensure_ascii=False)}\n\n".encode("utf-8")
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )

    client = await _connected(handler)
    try:
        chunks: list[AgentResponseChunk] = []
        async for chunk in client.send_request_stream(
            e2a_from_agent_fields(
                request_id="chat-1",
                channel_id="web",
                session_id="s1",
                req_method=ReqMethod.CHAT_SEND,
                params={"query": "hi"},
            )
        ):
            chunks.append(chunk)
        assert len(chunks) == 2
        assert chunks[0].is_complete is False
        assert chunks[-1].is_complete is True
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_send_request_stream_http_error_is_complete_chunk():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return _health_ok(request)
        return httpx.Response(
            404,
            json={
                "request_id": "h1",
                "ok": False,
                "error": {"code": "NOT_FOUND", "message": "no session"},
            },
        )

    client = await _connected(handler)
    try:
        chunks = [
            chunk
            async for chunk in client.send_request_stream(
                e2a_from_agent_fields(
                    request_id="h1",
                    channel_id="web",
                    session_id="s1",
                    req_method=ReqMethod.HISTORY_GET,
                    params={},
                    is_stream=True,
                )
            )
        ]
        assert len(chunks) == 1
        assert chunks[0].is_complete is True
        assert chunks[0].payload["code"] == "NOT_FOUND"
        assert chunks[0].payload["error"] == "no session"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_push_loop_only_forwards_server_push_frames():
    received: list[dict] = []
    got = asyncio.Event()
    stream_release = asyncio.Event()

    async def on_push(frame: dict) -> None:
        received.append(frame)
        got.set()

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            request_line = header.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            _method, path, *_rest = request_line.split(" ")
            if path.endswith("/health"):
                writer.write(_http_json({"ok": True, "data": {"status": "ready"}}))
                await writer.drain()
                return
            if path.endswith("/events/stream"):
                noise = {"request_id": "n", "metadata": {}}
                push = {
                    "request_id": "p1",
                    "metadata": {E2A_WIRE_SERVER_PUSH_KEY: True},
                    "body": {"event_type": "proactive_recommendation"},
                }
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Cache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n"
                )
                writer.write(_sse_blob(noise))
                writer.write(_sse_blob(push))
                await writer.drain()
                await stream_release.wait()
                return
            writer.write(_http_json({"ok": False, "error": {"code": "NOT_FOUND"}}))
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = HttpSseAgentServerClient()
    client.set_server_push_handler(on_push)
    try:
        await client.connect(f"http://127.0.0.1:{port}")
        await asyncio.wait_for(got.wait(), timeout=5)
        assert received[0]["request_id"] == "p1"
        assert E2A_WIRE_SERVER_PUSH_KEY in received[0]["metadata"]
    finally:
        stream_release.set()
        await client.disconnect()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_push_loop_backs_off_after_clean_stream_end(monkeypatch):
    """Clean SSE EOF must share the reconnect sleep, or GET /events/stream hot-loops."""
    import jiuwenswarm.gateway.routing.http_agent_client as hac

    monkeypatch.setattr(hac, "_PUSH_RETRY_SECONDS", 0.2)
    stream_hits = 0

    async def on_push(_frame: dict) -> None:
        return None

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal stream_hits
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            request_line = header.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            _method, path, *_rest = request_line.split(" ")
            if path.endswith("/health"):
                writer.write(_http_json({"ok": True, "data": {"status": "ready"}}))
                await writer.drain()
                return
            if path.endswith("/events/stream"):
                stream_hits += 1
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                return
            writer.write(_http_json({"ok": False, "error": {"code": "NOT_FOUND"}}))
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = HttpSseAgentServerClient()
    client.set_server_push_handler(on_push)
    try:
        await client.connect(f"http://127.0.0.1:{port}")
        await asyncio.sleep(0.55)
        assert 2 <= stream_hits <= 4, stream_hits
    finally:
        await client.disconnect()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_send_request_with_base_url_skips_connect():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={"request_id": "r1", "ok": True, "data": {"result": {"n": 1}}},
        )

    client = HttpSseAgentServerClient(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        env = e2a_from_agent_fields(
            request_id="r1",
            channel_id="web",
            req_method=ReqMethod.SESSION_LIST,
            params={},
        )
        result = await client.send_request(env, base_url="http://10.42.1.8:8080")
        assert result.ok is True
        assert any("10.42.1.8:8080/api/v1/sessions" in url for url in seen)
        assert client.server_ready is False
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_send_request_base_url_5xx_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"ok": False, "error": {"message": "bad gateway"}})

    client = HttpSseAgentServerClient(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    env = e2a_from_agent_fields(
        request_id="r1",
        channel_id="web",
        req_method=ReqMethod.SESSION_LIST,
        params={},
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.send_request(env, base_url="http://10.42.1.8:8080")
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_cancelled_rid_drops_envelope_and_chunk_ids():
    client = HttpSseAgentServerClient()
    await client._mark_stream_cancelled("old-rid")
    assert client._should_drop_stream_chunk(envelope_rid="old-rid", chunk_rid="x")
    assert client._should_drop_stream_chunk(envelope_rid="new", chunk_rid="old-rid")
    assert not client._should_drop_stream_chunk(envelope_rid="new", chunk_rid="new")


async def _start_trailing_sse_stub(*, first_rid: str = "chat-old"):
    first_sent = asyncio.Event()
    release_trailing = asyncio.Event()
    paths: list[str] = []

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            request_line = header.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            _method, path, *_rest = request_line.split(" ")
            paths.append(path)
            content_length = 0
            for raw in header.split(b"\r\n"):
                if raw.lower().startswith(b"content-length:"):
                    content_length = int(raw.split(b":", 1)[1].strip() or 0)
            body = b""
            if content_length:
                body = await reader.readexactly(content_length)

            if path.endswith("/health"):
                writer.write(
                    _http_json({"ok": True, "data": {"status": "ready"}, "request_id": "h1"})
                )
            elif path.endswith("/chat/completions"):
                req_rid = first_rid
                try:
                    parsed = json.loads(body.decode("utf-8") or "{}")
                    query = str((parsed or {}).get("query") or "")
                    if query == "new":
                        req_rid = "chat-new"
                except Exception:  # noqa: BLE001
                    pass
                first = encode_agent_chunk_for_wire(
                    AgentResponseChunk(
                        request_id=req_rid,
                        channel_id="web",
                        payload={"content": "first", "event_type": "chat.delta"},
                        is_complete=False,
                    ),
                    response_id=req_rid,
                    sequence=0,
                )
                trailing = encode_agent_chunk_for_wire(
                    AgentResponseChunk(
                        request_id=req_rid,
                        channel_id="web",
                        payload={"content": "STALE_TAIL", "event_type": "chat.delta"},
                        is_complete=False,
                    ),
                    response_id=req_rid,
                    sequence=1,
                )
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n"
                )
                writer.write(_sse_blob(first))
                await writer.drain()
                first_sent.set()
                await asyncio.wait_for(release_trailing.wait(), timeout=5)
                writer.write(_sse_blob(trailing))
            else:
                writer.write(_http_json({"ok": False, "error": {"code": "NOT_FOUND"}}))
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, first_sent, release_trailing


@pytest.mark.asyncio
async def test_send_request_stream_cancel_drops_trailing_chunks():
    server, port, first_sent, release_trailing = await _start_trailing_sse_stub()
    base = f"http://127.0.0.1:{port}"
    client = HttpSseAgentServerClient()
    yielded: list[str] = []
    try:
        await client.connect(base)
        env = e2a_from_agent_fields(
            request_id="chat-old",
            channel_id="web",
            session_id="s1",
            req_method=ReqMethod.CHAT_SEND,
            params={"query": "old"},
            is_stream=True,
        )

        async def _consume() -> None:
            async for chunk in client.send_request_stream(env):
                payload = chunk.payload or {}
                yielded.append(str(payload.get("content") or ""))

        task = asyncio.create_task(_consume())
        await asyncio.wait_for(first_sent.wait(), timeout=5)
        for _ in range(100):
            if yielded:
                break
            await asyncio.sleep(0.01)
        assert "first" in yielded
        await client._mark_stream_cancelled("chat-old")
        release_trailing.set()
        await asyncio.sleep(0.1)
        assert "STALE_TAIL" not in yielded
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        release_trailing.set()
        await client.disconnect()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_new_stream_supersedes_old_stream_trailing_chunks():
    server, port, first_sent, release_trailing = await _start_trailing_sse_stub()
    base = f"http://127.0.0.1:{port}"
    client = HttpSseAgentServerClient()
    old_yielded: list[str] = []
    new_yielded: list[str] = []
    try:
        await client.connect(base)
        old_env = e2a_from_agent_fields(
            request_id="chat-old",
            channel_id="web",
            session_id="s1",
            req_method=ReqMethod.CHAT_SEND,
            params={"query": "old"},
            is_stream=True,
        )
        new_env = e2a_from_agent_fields(
            request_id="chat-new",
            channel_id="web",
            session_id="s1",
            req_method=ReqMethod.CHAT_SEND,
            params={"query": "new"},
            is_stream=True,
        )

        async def _consume_old() -> None:
            async for chunk in client.send_request_stream(old_env):
                payload = chunk.payload or {}
                old_yielded.append(str(payload.get("content") or ""))

        old_task = asyncio.create_task(_consume_old())
        await asyncio.wait_for(first_sent.wait(), timeout=5)
        first_sent.clear()

        async def _consume_new() -> None:
            async for chunk in client.send_request_stream(new_env):
                payload = chunk.payload or {}
                new_yielded.append(str(payload.get("content") or ""))

        new_task = asyncio.create_task(_consume_new())
        await asyncio.wait_for(first_sent.wait(), timeout=5)
        release_trailing.set()
        await asyncio.sleep(0.15)
        old_task.cancel()
        new_task.cancel()
        await asyncio.gather(old_task, new_task, return_exceptions=True)
        assert "first" in old_yielded
        assert "STALE_TAIL" not in old_yielded
    finally:
        release_trailing.set()
        await client.disconnect()
        server.close()
        await server.wait_closed()
