# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_http_routes as agent_http_routes_module
from jiuwenswarm.server.agent_http_routes import ROUTES, RouteSpec
from tests.transport.conftest import fill_path


def _route_id(spec: RouteSpec) -> str:
    return f"{spec.verb}:{spec.path}"


def test_route_table_not_empty() -> None:
    assert len(ROUTES) > 100, f"路由表异常偏少: {len(ROUTES)}"


@pytest.mark.parametrize("spec", ROUTES, ids=_route_id)
def test_route_method_is_valid_req_method(spec: RouteSpec) -> None:
    ReqMethod(spec.method)  # 非法值会抛 ValueError


def test_no_duplicate_route_definitions() -> None:
    seen: dict[tuple[str, str], RouteSpec] = {}
    dupes = []
    for spec in ROUTES:
        key = (spec.verb, spec.path)
        if key in seen:
            dupes.append(key)
        seen[key] = spec
    assert not dupes, f"重复路由定义: {dupes}"


@pytest.mark.parametrize("spec", ROUTES, ids=_route_id)
def test_route_reachable_and_envelope_wellformed(spec: RouteSpec, client, stub_server) -> None:
    url = f"/api/v1{fill_path(spec.path)}"
    resp = client.request(spec.verb, url, json={} if spec.verb != "GET" else None)

    # 1) 不应出现路由层错误（404 路由未注册 / 405 动词不匹配 / 422 参数解析失败）
    assert resp.status_code not in (404, 405, 422), (
        f"{spec.verb} {url} 路由层错误 {resp.status_code}: {resp.text[:300]}"
    )
    # 2) 桩必须收到请求，且 method 与声明一致
    env = stub_server.last
    assert env.get("method") == spec.method, f"method 映射错误: {env.get('method')} != {spec.method}"
    # 3) 响应信封合规
    body = resp.json()
    assert body.get("request_id"), "缺 request_id"
    assert body.get("ok") is True, f"桩返回成功却渲染为失败: {body}"
    assert "data" in body and "metadata" in body, f"信封字段缺失: {body.keys()}"
    # 4) 状态码符合声明
    assert resp.status_code == spec.status, (
        f"{spec.verb} {url} 状态码 {resp.status_code} != 声明 {spec.status}"
    )
    # 5) 响应头回显 request id
    assert resp.headers.get("X-Request-Id") == body["request_id"]


@pytest.mark.parametrize(
    "spec", [s for s in ROUTES if "{session_id}" in s.path], ids=_route_id
)
def test_session_id_lifted_to_envelope(spec: RouteSpec, client, stub_server) -> None:
    url = f"/api/v1{fill_path(spec.path)}"
    client.request(spec.verb, url, json={} if spec.verb != "GET" else None)
    assert stub_server.last.get("session_id") == "sess_probe"


@pytest.mark.parametrize("spec", [s for s in ROUTES if s.param_defaults], ids=_route_id)
def test_param_defaults_applied(spec: RouteSpec, client, stub_server) -> None:
    url = f"/api/v1{fill_path(spec.path)}"
    client.request(spec.verb, url, json={}, headers={"X-Request-Id": "rid-probe"})
    params = stub_server.last.get("params") or {}
    for key in spec.param_defaults:
        assert params.get(key), f"{key} 未被补齐: {params}"


# ---------------------------------------------------------------- 参数管道
def test_query_params_reach_params(client, stub_server) -> None:
    client.get("/api/v1/sessions?limit=7&offset=3")
    params = stub_server.last["params"]
    assert params.get("limit") == "7" and params.get("offset") == "3"


def test_body_overrides_query(client, stub_server) -> None:
    client.request("PATCH", "/api/v1/sessions/sess_probe?title=from_query", json={"title": "from_body"})
    assert stub_server.last["params"]["title"] == "from_body"


def test_headers_map_to_envelope(client, stub_server) -> None:
    client.get(
        "/api/v1/sessions",
        headers={"X-Channel-Id": "acp", "X-User-Id": "u1", "X-Request-Id": "rid1"},
    )
    env = stub_server.last
    assert env["channel"] == "acp"
    assert env["request_id"] == "rid1"
    assert env["params"].get("user_id") == "u1"
    assert env["metadata"].get("user_id") == "u1"


def test_internal_request_ext_header_reaches_agent_metadata(client, stub_server) -> None:
    from jiuwenswarm.common.request_ext import (
        INTERNAL_HEADER_NAME,
        encode_internal_header,
    )

    ext = {"tenant": "租户-a", "custom_trace": "trace-1"}
    response = client.get(
        "/api/v1/sessions",
        headers={INTERNAL_HEADER_NAME: encode_internal_header(ext)},
    )

    assert response.status_code == 200
    assert stub_server.last["metadata"]["ext"] == ext


def test_invalid_internal_request_ext_header_returns_400(client) -> None:
    from jiuwenswarm.common.request_ext import INTERNAL_HEADER_NAME

    response = client.get(
        "/api/v1/sessions",
        headers={INTERNAL_HEADER_NAME: "not+base64"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid internal request extension header"


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        # 明确的错误码优先
        ("NOT_FOUND", "whatever", 404),
        ("CONFLICT", "whatever", 409),
        ("VALIDATION_ERROR", "bad", 422),
        # 通用码按 message 细化 —— 防止客户端错误被误报为 500
        ("E2A.AGENT_ERROR", "session not found", 404),
        ("E2A.AGENT_ERROR", "会话不存在", 404),
        ("E2A.AGENT_ERROR", "skill already exists", 409),
        ("E2A.AGENT_ERROR", "create_token is required", 400),
        ("E2A.AGENT_ERROR", "缺少参数: name", 400),
        ("E2A.AGENT_ERROR", "permission denied", 403),
        # 无从判断时回落 500
        ("E2A.AGENT_ERROR", "boom", 500),
        ("", "", 500),
    ],
)
def test_error_status_resolution(code: str, message: str, expected: int) -> None:
    from jiuwenswarm.server.agent_http_server import resolve_error_status

    assert resolve_error_status(code, message) == expected


def test_health_needs_no_backend(client) -> None:
    body = client.get("/api/v1/health").json()
    assert body["ok"] is True and body["data"]["status"] == "ready"


def test_generic_rpc_accepts_every_req_method(client, stub_server) -> None:
    for method in ["session.list", "agents.list", "skills.installed", "heartbeat.get_conf"]:
        resp = client.post(f"/api/v1/rpc/{method}", json={})
        assert resp.status_code == 200, f"{method}: {resp.text[:200]}"
        assert stub_server.last["method"] == method


def test_generic_rpc_rejects_unknown_method(client) -> None:
    resp = client.post("/api/v1/rpc/definitely.not.a.method", json={})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "UNKNOWN_METHOD"


def test_e2a_passthrough(client, stub_server) -> None:
    resp = client.post(
        "/api/v1/e2a", json={"method": "session.list", "params": {"limit": 1}, "request_id": "e1"}
    )
    assert resp.status_code == 200
    assert stub_server.last["method"] == "session.list"


def test_e2a_rejects_bad_json(client) -> None:
    resp = client.post(
        "/api/v1/e2a", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BAD_REQUEST"


# ---------------------------------------------------------------- 流式
def test_chat_sse_when_accept_event_stream(client) -> None:
    resp = client.post(
        "/api/v1/chat/completions",
        json={"session_id": "s1", "query": "hi"},
        headers={"Accept": "text/event-stream"},
    )
    assert "event:" in resp.text, f"未返回 SSE: {resp.text[:200]}"


def test_chat_json_when_no_sse_accept(client, stub_server) -> None:
    resp = client.post("/api/v1/chat/completions", json={"session_id": "s1", "query": "hi"})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert stub_server.last["is_stream"] is False


def test_chat_stream_flag_in_body(client, stub_server) -> None:
    client.post("/api/v1/chat/completions", json={"session_id": "s1", "query": "hi",
                                                  "enable_streaming": True})
    assert stub_server.last["is_stream"] is True


@pytest.mark.parametrize(
    "kw",
    [
        dict(method="session.list", params={}, request_id="r1", session_id=None,
             channel_id="web", user_id=None, is_stream=False),
        dict(method="session.list", params={"limit": 5}, request_id="r2", session_id="s1",
             channel_id="acp", user_id="u1", is_stream=False),
        dict(method="chat.send", params={"query": "你好", "mode": "agent"}, request_id="r3",
             session_id="s2", channel_id="tui", user_id="u2", is_stream=True),
        dict(method="command.status", params={"nested": {"a": [1, 2, {"b": None}]}},
             request_id="r4", session_id="s3", channel_id="web", user_id=None, is_stream=False),
        dict(method="team.delete", params={"team_name": "T", "unicode": "中文🎉"},
             request_id="r5", session_id=None, channel_id="web", user_id="u3", is_stream=False),
        dict(method="chat.send", params={"query": "ext"}, request_id="r6",
             session_id="s4", channel_id="web", user_id="u4", is_stream=True,
             request_ext={"tenant": "租户-a", "feature": {"flag": True}}),
    ],
    ids=lambda kw: f"{kw['method']}-stream{int(kw['is_stream'])}",
)
def test_direct_agent_request_equals_json_roundtrip(kw) -> None:
    import dataclasses
    import json as _json

    from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
    from jiuwenswarm.common.e2a.models import E2AEnvelope
    from jiuwenswarm.server.agent_http_server import build_agent_request, build_envelope_json

    via_json = e2a_to_agent_request(E2AEnvelope.from_dict(_json.loads(build_envelope_json(**kw))))
    direct = build_agent_request(**kw)

    def norm(r):
        return dataclasses.asdict(r) if dataclasses.is_dataclass(r) else dict(vars(r))

    assert norm(direct) == norm(via_json)


@pytest.mark.parametrize(
    ("payload", "is_complete", "expected_event"),
    [
        ({"event_type": "chat.delta", "delta": "hi"}, False, "e2a.chunk"),
        ({"event_type": "chat.reasoning", "delta": "…"}, False, "e2a.chunk"),
        ({"event_type": "chat.final"}, True, "e2a.complete"),
    ],
)
def test_sse_event_name_comes_from_response_kind(payload, is_complete, expected_event) -> None:
    from jiuwenswarm.common.e2a.wire_codec import encode_agent_chunk_for_wire
    from jiuwenswarm.common.schema.agent import AgentResponseChunk
    from jiuwenswarm.server.agent_http_server import _frame_event_name

    chunk = AgentResponseChunk(
        request_id="r1", channel_id="web", payload=payload, is_complete=is_complete
    )
    wire = encode_agent_chunk_for_wire(chunk, response_id="r1", sequence=0)

    assert _frame_event_name(wire) == expected_event
    assert not _frame_event_name(wire).startswith("chat."), (
        "SSE event 不应是 chat.* —— 那是 body.event_type 的取值，不是帧类型"
    )
    # 两种帧的 body 结构**不同**，对接方容易踩：
    #   e2a.chunk    -> body 平铺，业务类型在 body.event_type
    #   e2a.complete -> body 嵌套在 result 下，业务类型在 body.result.event_type
    body = wire["body"]
    if is_complete:
        assert body["result"]["event_type"] == payload["event_type"], (
            "终态帧的载荷嵌在 body.result 下，不是平铺的 body"
        )
        assert "event_type" not in body, "终态帧 body 顶层不应有 event_type（易误读）"
    else:
        assert body["event_type"] == payload["event_type"], (
            "分片帧的业务语义类型应可从 body.event_type 直接取到"
        )


def test_payload_code_refines_generic_error_status() -> None:
    from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
    from jiuwenswarm.common.schema.agent import AgentResponse
    from jiuwenswarm.server.agent_http_server import frame_to_http_envelope

    def status_for(payload: dict) -> int:
        resp = AgentResponse(request_id="r1", channel_id="web", ok=False, payload=payload)
        wire = encode_agent_response_for_wire(resp, response_id="r1")
        return frame_to_http_envelope(wire, "r1")[1]

    assert status_for({"members": [], "code": "NOT_FOUND"}) == 404, (
        "payload 带已知错误码时应细化为对应状态码"
    )
    assert status_for({"members": []}) == 500, "不带码时维持既有的 500 回落"
    assert status_for({"members": [], "code": "SOME_CHANNEL_CODE"}) == 500, (
        "payload 里不在 ERROR_CODE_STATUS 的 code 不得改变状态码"
    )


def test_cors_defaults_to_local_frontend_not_wildcard(monkeypatch) -> None:
    from jiuwenswarm.server.agent_http_server import resolve_cors_origins

    monkeypatch.delenv("AGENT_HTTP_CORS_ORIGINS", raising=False)
    monkeypatch.setenv("FRONTEND_PORT", "6173")
    monkeypatch.setenv("WEB_PORT", "20000")
    origins, credentials = resolve_cors_origins()
    assert "*" not in origins, "默认不得放行任意来源"
    assert "http://localhost:6173" in origins, "应跟随 FRONTEND_PORT，而不是写死 5173"
    assert "http://127.0.0.1:20000" in origins, "WEB_PORT 也应放行"
    assert credentials is True

    monkeypatch.setenv("AGENT_HTTP_CORS_ORIGINS", "https://app.example.com")
    assert resolve_cors_origins() == (["https://app.example.com"], True)

    monkeypatch.setenv("AGENT_HTTP_CORS_ORIGINS", "*")
    assert resolve_cors_origins() == (["*"], False), (
        "显式放开时必须关掉 credentials，否则浏览器会拒绝整个跨域请求"
    )


# ---------------------------------------------------------------------------
# /e2a 原始信封：流式与非流式都必须原样透传
# ---------------------------------------------------------------------------
def test_e2a_stream_envelope_routes_to_sse() -> None:
    """信封声明 ``is_stream: true`` 时 ``/e2a`` 必须走 SSE。

    此前该分支用 ``UnaryHTTPSink`` 接：业务层全程 ``send_chunk``，而该 sink 只把
    ``send_unary`` / ``send_wire`` 的帧记进 ``last_frame``，chunk 全落进无人读取的
    ``frames``。结果是 HTTP 200 + ``{"ok": true, "data": null}`` —— 内容整个丢失，
    调用方却拿到"成功"，排查时会误往业务层找。
    """
    import asyncio
    import json as _json

    from starlette.requests import Request

    from jiuwenswarm.server.agent_http_server import API_PREFIX, AgentHTTPServer

    server = AgentHTTPServer.__new__(AgentHTTPServer)
    server._ws_server = None  # type: ignore[attr-defined]
    app = server.build_app()
    endpoint = next(
        r.endpoint for r in app.routes
        if getattr(r, "path", None) == f"{API_PREFIX}/e2a"
    )

    body = _json.dumps(
        {"protocol_version": "1.0", "method": "chat.send", "channel": "web",
         "params": {"query": "hi"}, "is_stream": True}
    ).encode()

    async def _receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http", "method": "POST", "path": f"{API_PREFIX}/e2a",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"", "path_params": {},
    }
    response = asyncio.run(endpoint(Request(scope, _receive)))

    media_type = getattr(response, "media_type", "") or ""
    assert "event-stream" in media_type, (
        f"is_stream=true 的信封应走 SSE，实际 media_type={media_type!r}。"
        f"用非流式 sink 接会静默返回空 200。"
    )


def test_e2a_unary_envelope_still_returns_json() -> None:
    """未声明流式的信封仍走普通 JSON —— 防止上一条把非流式也一并改掉。"""
    import asyncio
    import json as _json

    from starlette.requests import Request

    from jiuwenswarm.server.agent_http_server import API_PREFIX, AgentHTTPServer

    server = AgentHTTPServer.__new__(AgentHTTPServer)
    server._ws_server = None  # type: ignore[attr-defined]
    app = server.build_app()
    endpoint = next(
        r.endpoint for r in app.routes
        if getattr(r, "path", None) == f"{API_PREFIX}/e2a"
    )

    body = _json.dumps({"protocol_version": "1.0", "method": "session.list",
                        "channel": "web", "params": {}}).encode()

    async def _receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http", "method": "POST", "path": f"{API_PREFIX}/e2a",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"", "path_params": {},
    }
    response = asyncio.run(endpoint(Request(scope, _receive)))
    media_type = getattr(response, "media_type", "") or ""
    assert "event-stream" not in media_type, f"非流式信封不该走 SSE：{media_type!r}"


def test_unary_sink_warns_when_only_chunks_were_produced() -> None:
    """非流式 sink 只收到 chunk 时必须留痕，不能静默返回空成功。

    这是 ``/e2a`` 带 ``is_stream: true`` 那个 bug 的 signature：handler 确实产出了
    内容，但都是 ``send_chunk`` 的帧，``last_frame`` 为空，路由层照常返回
    ``HTTP 200 + data:null``。该路径已改走 SSE，这条探针是留给下一个把流式 handler
    接到非流式入口的人 —— 让同类问题第一次发生就可见。
    """
    import asyncio

    from jiuwenswarm.common.schema.agent import AgentResponseChunk
    from jiuwenswarm.server.transports.sink import UnaryHTTPSink

    sink = UnaryHTTPSink()

    async def scenario() -> None:
        await sink.send_chunk(
            AgentResponseChunk(
                request_id="r1", channel_id="web", payload={"text": "x"}, is_complete=True
            ),
            sequence=0,
            response_id="r1",
        )

    asyncio.run(scenario())

    seen = _capture_sink_warnings(lambda: sink.last_frame)
    assert sink.last_frame is None
    assert any("没有可渲染的响应帧" in m for m in seen), (
        f"未产生探针 warning —— 这类失败会退化成静默的空 200。已记录: {seen}"
    )


def test_unary_sink_stays_quiet_on_normal_response() -> None:
    """正常非流式响应不得触发探针 —— 防止它变成噪音。"""
    import asyncio
    import logging

    from jiuwenswarm.common.schema.agent import AgentResponse
    from jiuwenswarm.server.transports.sink import UnaryHTTPSink

    sink = UnaryHTTPSink()

    async def scenario() -> None:
        await sink.send_unary(
            AgentResponse(request_id="r1", channel_id="web", ok=True, payload={"a": 1}),
            response_id="r1",
        )

    asyncio.run(scenario())
    logger = logging.getLogger("jiuwenswarm.server.transports.sink")
    seen: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: seen.append(record.getMessage())  # type: ignore[assignment]
    logger.addHandler(handler)
    try:
        assert sink.last_frame is not None
    finally:
        logger.removeHandler(handler)
    assert not seen, f"正常响应不该触发探针：{seen}"


def _capture_sink_warnings(fn) -> list[str]:
    """执行 fn 并收集 ``transports.sink`` 打出的日志文本。"""
    import logging

    logger = logging.getLogger("jiuwenswarm.server.transports.sink")
    seen: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: seen.append(record.getMessage())  # type: ignore[assignment]
    logger.addHandler(handler)
    try:
        fn()
    finally:
        logger.removeHandler(handler)
    return seen


def test_query_string_numeric_params_are_coerced_like_ws() -> None:
    from jiuwenswarm.server.handlers.session import _coerce_int

    for ws_value, http_value in [(1, "1"), (20, "20"), (500, "500"), (0, "0")]:
        assert _coerce_int(ws_value, -999) == _coerce_int(http_value, -999), (
            f"WS 传 {ws_value!r} 与 HTTP 传 {http_value!r} 归一化结果不一致 —— "
            f"同一个方法两个传输会有不同行为。"
        )


def test_history_handlers_coerce_page_idx() -> None:
    import ast
    import inspect
    from jiuwenswarm.server.handlers import session as session_mod

    tree = ast.parse(inspect.getsource(session_mod))
    targets = {"handle_history_get", "handle_history_get_stream"}
    found: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.AsyncFunctionDef) and node.name in targets):
            continue
        found.add(node.name)
        src = ast.unparse(node)
        assert "_coerce_int(params.get('page_idx')" in src.replace('"', "'"), (
            f"{node.name} 未对 page_idx 做归一化 —— HTTP query 传下来是字符串，"
            f"get_conversation_history 的 isinstance(page_idx, int) 会判它非法。"
        )
    assert found == targets, f"未找到全部 handler：缺 {targets - found}"


def test_schedule_logs_coerces_numeric_params() -> None:
    import inspect
    from jiuwenswarm.server.handlers import schedule as schedule_mod

    src = inspect.getsource(schedule_mod)
    for name, default in (("history_index", "-1"), ("offset", "0"), ("limit", "500")):
        expected = f'_coerce_int(params.get("{name}"), {default})'
        assert expected in src, f"schedule logs 未归一化 {name} —— 期望 `{expected}`"


def test_gateway_rest_map_paths_all_exist_on_server() -> None:
    from jiuwenswarm.gateway.routing.agent_rest_map import REST_ROUTES
    registered = {(spec.verb, spec.path) for spec in ROUTES}
    registered_paths = {spec.path for spec in ROUTES}
    missing = []
    for method, (verb, path) in sorted(REST_ROUTES.items()):
        if (verb, path) in registered:
            continue
        why = "路径未注册 → 404" if path not in registered_paths else "路径在、动词不符 → 405"
        missing.append(f"{verb:6s} {path}  ({method})  [{why}]")

    assert not missing, (
        "Gateway 南向会打这些路径，但 AgentServer 没注册。切 HTTP 后必挂，且不会降级：\n  "
        + "\n  ".join(missing)
    )


def test_server_routes_all_reachable_from_gateway_map() -> None:
    from jiuwenswarm.gateway.routing.agent_rest_map import REST_ROUTES

    server_map = {spec.method: (spec.verb, spec.path) for spec in ROUTES}
    drifted = [
        f"{method}: server={server_map[method]}  gateway={REST_ROUTES[method]}"
        for method in sorted(server_map.keys() & REST_ROUTES.keys())
        if server_map[method] != REST_ROUTES[method]
    ]
    assert not drifted, "同一 method 两张表路径不一致：\n  " + "\n  ".join(drifted)

_KNOWN_UNIMPLEMENTED_ON_SERVER = frozenset({
    # --- Gateway 本地实现（app_web_handlers.register_method），Gateway 不转发 ---
    "channel.dingtalk.get_conf", "channel.dingtalk.set_conf",
    "channel.feishu.get_conf", "channel.feishu.set_conf",
    "channel.slack.get_conf", "channel.slack.set_conf",
    "channel.telegram.get_conf", "channel.telegram.set_conf",
    "channel.wechat.get_conf", "channel.wechat.set_conf",
    "channel.wechat.get_login_ui", "channel.wechat.unbind",
    "channel.whatsapp.get_conf", "channel.whatsapp.set_conf",
    "channel.xiaoyi.get_conf", "channel.xiaoyi.set_conf",
    "heartbeat.get_conf", "heartbeat.set_conf",
    "updater.check", "updater.download",
    "updater.get_conf", "updater.set_conf", "updater.get_status",
    # --- TUI 本地实现（tui_connect.register_local_handler）---
    "history.list_turns",
    "session.restore_files",
})


def _methods_with_server_landing_spot() -> set[str]:
    import re
    from pathlib import Path

    from jiuwenswarm.server import dispatch as dispatch_mod

    found = {m.value for m in dispatch_mod.HANDLERS}

    server_dir = Path(agent_http_routes_module.__file__).parent
    attr = re.compile(r"ReqMethod\.([A-Z0-9_]+)")
    for path in server_dir.rglob("*.py"):
        if "__pycache__" in path.parts or path.name == "agent_http_routes.py":
            continue
        for name in set(attr.findall(path.read_text(encoding="utf-8"))):
            member = getattr(ReqMethod, name, None)
            if member is not None:
                found.add(member.value)
    return found


def test_every_route_has_a_server_landing_spot_or_is_registered_as_missing() -> None:
    landing = _methods_with_server_landing_spot()
    unhandled = {spec.method for spec in ROUTES} - landing

    new = sorted(unhandled - _KNOWN_UNIMPLEMENTED_ON_SERVER)
    assert not new, (
        "这些路由声明了，但 AgentServer 上没有落点，调用方会拿到模型回的『收到』而不是错误：\n  "
        + "\n  ".join(new)
        + "\n补 handler，或写进 _KNOWN_UNIMPLEMENTED_ON_SERVER 并注明归属。"
    )

    stale = sorted(_KNOWN_UNIMPLEMENTED_ON_SERVER - unhandled)
    assert not stale, (
        "这些方法已经有落点了（或路由已删），清单该同步删掉：\n  " + "\n  ".join(stale)
    )
