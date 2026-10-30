# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Gateway Web HTTP core-route tests (avoid heavy gateway package import side-effects)."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
OUTBOUND_PATH = ROOT / "gateway" / "channel_manager" / "web" / "outbound.py"
HTTP_PATH = ROOT / "gateway" / "channel_manager" / "web" / "web_http_routes.py"
TIMEOUTS_PATH = ROOT / "gateway" / "channel_manager" / "web" / "web_http_timeouts.py"
ROUTES_PATH = ROOT / "gateway" / "channel_manager" / "web" / "http_routes" / "mapped.py"


def _load_module(mod_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


outbound_mod = _load_module("jw_outbound_under_test", OUTBOUND_PATH)


class _FakePeer:
    def __init__(self, frames: list[dict[str, Any]]):
        self._frames = list(frames)
        self.closed = False

    async def wait_response(self, req_id: str, *, timeout: float = 60.0) -> dict[str, Any]:
        for f in self._frames:
            if f.get("type") == "res" and f.get("id") == req_id:
                return f
        return {"type": "res", "id": req_id, "ok": False, "error": "missing", "code": "NOT_FOUND", "payload": {}}

    async def iter_sse_frames(self, req_id: str, *, timeout: float = 600.0, **_kwargs):
        for f in self._frames:
            yield f


@pytest.fixture
def app_with_mock(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, AsyncMock]:
    # Stub dispatch module before loading web_http_routes.
    # Use monkeypatch.setitem so xdist workers restore the real module after
    # this fixture (otherwise later file-api tests ImportError on
    # ``_trust_client_tenant_headers`` from a leftover bare ModuleType stub).
    dispatch_mod = ModuleType("gateway.channel_manager.web.web_http_dispatch")
    dispatch_mock = AsyncMock()
    dispatch_mod.dispatch_http_request = dispatch_mock  # type: ignore[attr-defined]
    dispatch_mod._trust_client_tenant_headers = (  # type: ignore[attr-defined]
        lambda client_host=None: True
    )
    monkeypatch.setitem(
        sys.modules,
        "gateway.channel_manager.web.web_http_dispatch",
        dispatch_mod,
    )

    # Minimal package parents so relative imports in web_http_routes resolve if any
    for pkg in (
        "jiuwenswarm",
        "gateway",
        "gateway.channel_manager",
        "gateway.channel_manager.web",
    ):
        if pkg not in sys.modules:
            m = ModuleType(pkg)
            m.__path__ = []  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, pkg, m)

    # http_routes package so the real mapped table can load as a submodule.
    routes_pkg = ModuleType("gateway.channel_manager.web.http_routes")
    routes_pkg.__path__ = [str(ROUTES_PATH.parent)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gateway.channel_manager.web.http_routes", routes_pkg)

    # Real mapped table (no Gateway imports) so routes can be registered.
    _load_module(
        "gateway.channel_manager.web.http_routes.mapped",
        ROUTES_PATH,
    )

    # web_http_routes also mounts trajectory routes; stub to avoid heavy imports.
    trajectory = ModuleType("gateway.channel_manager.web.trajectory_http")
    trajectory.attach_trajectory_routes = lambda app, channel: None  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules,
        "gateway.channel_manager.web.trajectory_http",
        trajectory,
    )
    diagnosis = ModuleType("gateway.channel_manager.web.diagnose_http")
    diagnosis.attach_diagnose_routes = lambda app, channel, **kwargs: None
    monkeypatch.setitem(
        sys.modules,
        "gateway.channel_manager.web.diagnose_http",
        diagnosis,
    )

    # web_http_routes imports timeout helpers from web_http_timeouts (stdlib-only).
    _load_module(
        "gateway.channel_manager.web.web_http_timeouts",
        TIMEOUTS_PATH,
    )

    web_http_routes = _load_module("jw_web_http_api_routes_under_test", HTTP_PATH)
    channel = object()
    app = FastAPI()
    web_http_routes.register_web_http_api_routes(app, channel)
    return app, dispatch_mock


def test_bind_http_session_create_does_not_inject_session_id():
    sid, params = outbound_mod.bind_http_session("session.create", {})
    assert "session_id" not in params
    assert sid.startswith("webhttp_")
    assert params.get("create_token")


def test_bind_http_session_create_keeps_client_create_token():
    _, params = outbound_mod.bind_http_session(
        "session.create", {"create_token": "client-token-1"},
    )
    assert params["create_token"] == "client-token-1"


def test_bind_http_session_create_strips_client_session_id():
    sid, params = outbound_mod.bind_http_session(
        "session.create", {"session_id": "web_should_not_go_to_agent"},
    )
    assert "session_id" not in params
    assert sid.startswith("webhttp_")
    assert sid != "web_should_not_go_to_agent"


def test_bind_http_session_shared_user_reuses_one_session():
    first, first_params = outbound_mod.bind_http_session(
        "agent_templates.list", {"user_id": "user1", "filter": "mine"}, bind_param=False,
    )
    second, second_params = outbound_mod.bind_http_session(
        "agent_templates.list", {"user_id": "user1"}, bind_param=False,
    )
    assert first == second == "shared_user_user1"
    assert "session_id" not in first_params
    assert "session_id" not in second_params


def test_bind_http_session_bind_param_false_does_not_invent_session_id():
    sid, params = outbound_mod.bind_http_session(
        "permissions.tools.get", {}, bind_param=False,
    )
    assert "session_id" not in params
    assert sid == "shared_user_default"


def test_bind_http_session_sessionless_reads_share_one_route():
    user = {"user_id": "user1"}
    catalog, catalog_params = outbound_mod.bind_http_session(
        "agent_templates.list", user, bind_param=False,
    )
    projects, project_params = outbound_mod.bind_http_session(
        "project.list", {**user, "filter": "all"}, bind_param=False,
    )
    locale, locale_params = outbound_mod.bind_http_session(
        "locale.get_conf", user, bind_param=False,
    )
    assert catalog == projects == locale == "shared_user_user1"
    assert "session_id" not in catalog_params
    assert "session_id" not in project_params
    assert "session_id" not in locale_params
    created, created_params = outbound_mod.bind_http_session(
        "session.create", user,
    )
    assert created.startswith("webhttp_")
    assert created != catalog
    assert "session_id" not in created_params


def test_bind_http_session_bind_param_false_keeps_client_session_id():
    sid, params = outbound_mod.bind_http_session(
        "skills.list",
        {"session_id": "web_sess"},
        bind_param=False,
    )
    assert params["session_id"] == "web_sess"
    assert sid == "web_sess"


def test_bind_http_session_chat_keeps_session_id():
    sid, params = outbound_mod.bind_http_session("chat.send", {"query": "hi"})
    assert params["session_id"] == sid
    assert sid.startswith("webhttp_")


def test_is_sse_end_frame_enterprise_ignores_foreign_processing_false(monkeypatch):
    monkeypatch.setattr(outbound_mod, "is_enterprise", lambda: True)
    foreign = {
        "type": "event",
        "event": "chat.processing_status",
        "payload": {"is_processing": False, "request_id": "chat-old"},
    }
    own = {
        "type": "event",
        "event": "chat.processing_status",
        "payload": {"is_processing": False, "request_id": "chat-new"},
    }
    assert outbound_mod._is_sse_end_frame(foreign, "chat-new") is False
    assert outbound_mod._is_sse_end_frame(own, "chat-new") is True


def test_http_sse_outbound_enterprise_keeps_open_on_foreign_processing_false(monkeypatch):
    monkeypatch.setattr(outbound_mod, "is_enterprise", lambda: True)

    async def _run():
        peer = outbound_mod.HttpSseOutbound()
        await peer.send(json.dumps({
            "type": "res", "id": "chat-new", "ok": True, "payload": {"accepted": True},
        }))
        await peer.send(json.dumps({
            "type": "event",
            "event": "chat.processing_status",
            "payload": {"is_processing": False, "request_id": "chat-old"},
        }))
        await peer.send(json.dumps({
            "type": "event",
            "event": "chat.delta",
            "payload": {"content": "hi", "request_id": "chat-new"},
        }))
        await peer.send(json.dumps({
            "type": "event",
            "event": "chat.processing_status",
            "payload": {"is_processing": False, "request_id": "chat-new"},
        }))
        events = []
        async for f in peer.iter_sse_frames("chat-new", timeout=2):
            events.append(f)
        assert any(e.get("event") == "chat.delta" for e in events)
        assert events[-1].get("event") == "chat.processing_status"
        assert events[-1]["payload"]["request_id"] == "chat-new"

    asyncio.run(_run())


def test_http_json_outbound_wait_response():
    async def _run():
        peer = outbound_mod.HttpJsonOutbound()
        await peer.send(json.dumps({"type": "res", "id": "r1", "ok": True, "payload": {"a": 1}}))
        frame = await peer.wait_response("r1", timeout=2)
        assert frame["ok"] is True
        assert frame["payload"]["a"] == 1

    asyncio.run(_run())


def test_http_json_outbound_wait_response_accepts_chat_error_event():
    async def _run():
        peer = outbound_mod.HttpJsonOutbound()
        peer.accept_frame({
            "type": "event",
            "event": "chat.error",
            "payload": {
                "request_id": "r-quota",
                "error": "WORKSPACE_QUOTA_EXCEEDED",
                "code": "WORKSPACE_QUOTA_EXCEEDED",
            },
        })
        frame = await peer.wait_response("r-quota", timeout=2)
        assert frame["ok"] is False
        assert frame["code"] == "WORKSPACE_QUOTA_EXCEEDED"
        assert frame["error"] == "WORKSPACE_QUOTA_EXCEEDED"

    asyncio.run(_run())


def test_http_sse_outbound_iter_ends_on_final():
    async def _run():
        peer = outbound_mod.HttpSseOutbound()
        await peer.send(json.dumps({"type": "res", "id": "r1", "ok": True, "payload": {"accepted": True}}))
        await peer.send(json.dumps({"type": "event", "event": "chat.delta", "payload": {"content": "x"}}))
        await peer.send(json.dumps({"type": "event", "event": "chat.final", "payload": {"content": "x"}}))
        events = []
        async for f in peer.iter_sse_frames("r1", timeout=2):
            events.append(f)
        assert any(e.get("event") == "chat.delta" for e in events)
        assert events[-1].get("event") == "chat.final"

    asyncio.run(_run())


def test_http_sse_outbound_iter_ends_on_history_done():
    async def _run():
        peer = outbound_mod.HttpSseOutbound()
        await peer.send(json.dumps({"type": "res", "id": "r1", "ok": True, "payload": {"accepted": True}}))
        await peer.send(json.dumps({
            "type": "event", "event": "history.message",
            "payload": {"message": {"role": "user", "content": "hi"}},
        }))
        await peer.send(json.dumps({
            "type": "event", "event": "history.message",
            "payload": {"status": "done", "page_idx": 1, "total_pages": 1},
        }))
        events = []
        async for f in peer.iter_sse_frames("r1", timeout=2):
            events.append(f)
        assert events[-1].get("payload", {}).get("status") == "done"

    asyncio.run(_run())


def test_http_dispatch_registers_outbound_not_ws(monkeypatch: pytest.MonkeyPatch):
    """HTTP path must use request Outbound tables, not register_ws."""
    from gateway.channel_manager.base import RobotMessageRouter
    from gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
    from gateway.channel_manager.web import web_http_dispatch as disp

    channel = WebChannel(WebChannelConfig(host="127.0.0.1", port=0), RobotMessageRouter())
    register_calls: list[Any] = []

    async def _no_register(ws, rk, **kwargs):
        register_calls.append((ws, rk))

    monkeypatch.setattr(channel, "register_ws", _no_register)

    async def _handler(ws, req_id, params, session_id, **kwargs):
        await channel.send_response(ws, req_id, ok=True, payload={"pong": True})

    channel.register_method("connection.status", _handler)

    async def _run():
        out, rid, sid = await disp.dispatch_http_request(
            channel,
            method="connection.status",
            params={},
            headers={"X-Request-Id": "req-s3"},
            use_sse=False,
        )
        try:
            assert register_calls == []
            assert out.outbound_id in channel.delivery.request_outbounds
            assert getattr(out, "is_http_outbound", False) is True
            frame = await out.wait_response(rid, timeout=2)
            assert frame["ok"] is True
            assert frame["payload"]["pong"] is True
        finally:
            await channel.unregister_request_outbound(out)
        assert out.outbound_id not in channel.delivery.request_outbounds

    asyncio.run(_run())


def test_http_dispatch_session_create_emits_resolve_identity(
    monkeypatch: pytest.MonkeyPatch,
):
    """session.create + X-User-Id → http_resolve_identity UA。"""
    from gateway.channel_manager.base import RobotMessageRouter
    from gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
    from gateway.channel_manager.web import web_http_dispatch as disp

    channel = WebChannel(WebChannelConfig(host="127.0.0.1", port=0), RobotMessageRouter())
    audit_calls: list[dict[str, Any]] = []

    def _capture_ua(**kwargs: Any) -> None:
        audit_calls.append(kwargs)

    monkeypatch.setattr(
        "gateway.enterprise.capabilities.audit.audit_emit.emit_audit_ua",
        _capture_ua,
    )
    monkeypatch.setattr(disp, "is_enterprise", lambda: True)

    async def _handler(ws, req_id, params, session_id, **kwargs):
        await channel.send_response(ws, req_id, ok=True, payload={"id": "s1"})

    channel.register_method("session.create", _handler)
    channel.register_method("chat.send", _handler)

    async def _run():
        out, _rid, _sid = await disp.dispatch_http_request(
            channel,
            method="session.create",
            params={"title": "t"},
            headers={
                "X-Request-Id": "req-id-1",
                "X-User-Id": "user-alice",
            },
            client_host="10.0.0.8",
            use_sse=False,
        )
        await channel.unregister_request_outbound(out)

        assert len(audit_calls) == 1
        assert audit_calls[0]["PROC"] == "http_resolve_identity"
        assert audit_calls[0]["SUBMDL"] == "gateway"
        assert audit_calls[0]["UID"] == "user-alice"
        assert audit_calls[0]["request_id"] == "req-id-1"

        audit_calls.clear()
        out2, _rid2, _sid2 = await disp.dispatch_http_request(
            channel,
            method="chat.send",
            params={"content": "hi"},
            headers={
                "X-Request-Id": "req-id-2",
                "X-User-Id": "user-alice",
                "X-Session-Id": "sess-1",
            },
            client_host="10.0.0.8",
            use_sse=False,
        )
        await channel.unregister_request_outbound(out2)
        assert audit_calls == []

        out3, _rid3, _sid3 = await disp.dispatch_http_request(
            channel,
            method="session.create",
            params={},
            headers={"X-Request-Id": "req-id-3"},
            client_host="10.0.0.8",
            use_sse=False,
        )
        await channel.unregister_request_outbound(out3)
        assert audit_calls == []

    asyncio.run(_run())


def test_http_sse_unregister_on_cancel():
    """Client cancel / stop clears request outbound routing tables."""
    from gateway.channel_manager.base import RobotMessageRouter
    from gateway.channel_manager.web.outbound import HttpSseOutbound
    from gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig

    channel = WebChannel(WebChannelConfig(host="127.0.0.1", port=0), RobotMessageRouter())

    async def _run():
        out = HttpSseOutbound(session_id="sess-cancel")
        oid = channel.register_request_outbound(out)
        assert oid in channel.delivery.request_outbounds
        assert oid in channel.delivery.session_request_outbounds.get("sess-cancel", set())

        # Simulate client disconnect mid-stream: unregister like _stream finally.
        await channel.unregister_request_outbound(out)
        assert oid not in channel.delivery.request_outbounds
        assert not channel.delivery.session_request_outbounds
        assert out.closed is True

        # stop_http / clear_request_outbounds also empties leftovers.
        out2 = HttpSseOutbound(session_id="sess-stop")
        channel.register_request_outbound(out2)
        await channel.delivery.clear()
        assert channel.delivery.request_outbounds == {}
        assert channel.delivery.session_request_outbounds == {}
        assert out2.closed is True

    asyncio.run(_run())


def test_sse_idle_timeout_ends_stream():
    async def _run():
        peer = outbound_mod.HttpSseOutbound()
        events = []
        async for f in peer.iter_sse_frames(
            "r1", timeout=5.0, idle_timeout=0.15, keepalive=0.05,
        ):
            events.append(f)
            if f.get("event") == "chat.error":
                break
        assert any(
            e.get("event") == "chat.error"
            and "idle" in str((e.get("payload") or {}).get("error", ""))
            for e in events
        )

    asyncio.run(_run())


def test_sse_zero_timeout_waits_for_final():
    async def _run():
        peer = outbound_mod.HttpSseOutbound()

        async def _emit_final() -> None:
            await asyncio.sleep(0.2)
            await peer.send(json.dumps({"type": "event", "event": "chat.final", "payload": {}}))

        asyncio.create_task(_emit_final())
        events = []
        async for f in peer.iter_sse_frames("r1", timeout=0, keepalive=0.05):
            events.append(f)
        assert events[-1].get("event") == "chat.final"
        assert not any(
            e.get("event") == "chat.error"
            and str((e.get("payload") or {}).get("error", "")) == "stream timeout"
            for e in events
        )

    asyncio.run(_run())


def test_sse_positive_timeout_still_ends_stream():
    async def _run():
        peer = outbound_mod.HttpSseOutbound()
        events = []
        async for f in peer.iter_sse_frames("r1", timeout=0.15, keepalive=0.05):
            events.append(f)
            if f.get("event") == "chat.error":
                break
        assert any(
            e.get("event") == "chat.error"
            and str((e.get("payload") or {}).get("error", "")) == "stream timeout"
            for e in events
        )

    asyncio.run(_run())


def test_history_json_collects_messages(app_with_mock):
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([
            {"type": "res", "id": rid, "ok": True, "payload": {"accepted": True}},
            {
                "type": "event",
                "event": "history.message",
                "payload": {"message": {"role": "user", "content": "hi"}, "page_idx": 1, "total_pages": 1},
            },
            {
                "type": "event",
                "event": "history.message",
                "payload": {"status": "done", "page_idx": 1, "total_pages": 1},
            },
        ])
        return peer, rid, "s1"

    dispatch.side_effect = _disp
    client = TestClient(app)
    r = client.get("/api/v1/sessions/s1/history")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["messages"][0]["content"] == "hi"
    assert data["page_idx"] == 1
    assert dispatch.await_args.kwargs["params"]["page_idx"] == 1


def test_history_stream_omitted_cursor_forwards_null(app_with_mock):
    """Frontend drops cursor=null from query; gateway must not forward \"\"."""
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([
            {"type": "res", "id": rid, "ok": True, "payload": {"accepted": True}},
            {
                "type": "event",
                "event": "history.message",
                "payload": {
                    "status": "done",
                    "has_more": False,
                    "next_cursor": None,
                    "snapshot_id": "snap",
                },
            },
        ])
        return peer, rid, "web_sid"

    dispatch.side_effect = _disp
    client = TestClient(app)
    r = client.get("/api/v1/sessions/web_sid/history", params={"limit": 50})
    assert r.status_code == 200
    params = dispatch.await_args.kwargs["params"]
    assert "cursor" in params
    assert params["cursor"] is None
    assert params["limit"] == 50


def test_openapi_lists_core_routes(app_with_mock):
    app, _dispatch = app_with_mock
    client = TestClient(app, follow_redirects=False)
    spec = client.get("/openapi.json").json()
    assert "/api/v1/health" in spec["paths"]
    assert "/api/v1/chat/completions" in spec["paths"]
    assert "/api/v1/chat/send" not in spec["paths"]
    assert spec["paths"]["/api/v1/sessions"]["post"]["tags"] == ["sessions"]
    post_body = spec["paths"]["/api/v1/sessions"]["post"]["requestBody"]["content"]["application/json"]
    example = post_body.get("example")
    if example is None:
        example = (post_body.get("examples") or {}).get("empty", {}).get("value")
    if example is None:
        example = post_body.get("schema", {}).get("example")
    assert example == {}
    assert not post_body.get("schema", {}).get("required")


def test_health(app_with_mock):
    app, _dispatch = app_with_mock
    client = TestClient(app)
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["status"] == "ready"


def test_connection_status(app_with_mock):
    app, dispatch = app_with_mock
    peer = _FakePeer([
        {"type": "res", "id": "fixed", "ok": True, "payload": {"agent_ready": True, "protocol_version": "1.0"}},
    ])

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "fixed"
        peer._frames[0]["id"] = rid
        return peer, rid, "sess"

    dispatch.side_effect = _disp
    client = TestClient(app)
    r = client.get("/api/v1/connection/status", headers={"X-Request-Id": "fixed"})
    assert r.status_code == 200
    assert r.json()["data"]["agent_ready"] is True
    dispatch.assert_awaited()
    assert dispatch.await_args.kwargs["method"] == "connection.status"


def test_sessions_list_create_delete(app_with_mock):
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        method = kwargs["method"]
        rid = kwargs.get("request_id") or "r"
        if method == "session.list":
            payload = {"sessions": [{"session_id": "s1"}], "total": 1, "limit": 20, "offset": 0}
        elif method == "session.create":
            payload = {"session_id": (kwargs.get("params") or {}).get("session_id") or "s_new"}
        else:
            payload = {"deleted": True}
        peer = _FakePeer([{"type": "res", "id": rid, "ok": True, "payload": payload}])
        return peer, rid, "s1"

    dispatch.side_effect = _disp
    client = TestClient(app)

    r = client.get("/api/v1/sessions")
    assert r.status_code == 200
    assert r.json()["data"]["total"] == 1

    r = client.post("/api/v1/sessions", json={"session_id": "s_x"})
    assert r.status_code == 201
    assert r.json()["data"]["session_id"] == "s_x"

    r = client.delete("/api/v1/sessions/s_x")
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.parametrize("path", [
    "/api/v1/chat/completions", "/api/v1/chat/send", "/api/v1/chat/resume",
    "/api/v1/chat/sess_1/actions/user_answer", "/api/v1/chat/sess_1/actions/answer",
])
@pytest.mark.parametrize("stream,accept", [(True, "application/json"), (False, "application/json"), (False, "text/event-stream")])
@pytest.mark.parametrize("field", ["user_id", "group_id", "bot_id", "gateway_id"])
def test_chat_rejects_conflicting_identity_before_dispatch(app_with_mock, path, stream, accept, field):
    app, dispatch = app_with_mock
    response = TestClient(app).post(
        path,
        json={"session_id": "sess_1", "query": "probe", "user_id": "A", field: "B", "enable_streaming": stream},
        headers={"X-" + field.replace("_", "-"): "A", "X-Request-Id": "conflict", "Accept": accept},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDENTITY_CONFLICT"
    assert response.headers["x-request-id"] == "conflict"
    dispatch.assert_not_called()


@pytest.mark.parametrize("path", [
    "/api/v1/chat/completions", "/api/v1/chat/resume",
    "/api/v1/chat/sess_1/actions/user_answer",
])
@pytest.mark.parametrize("body_identity", [
    {}, {"bot_id": "A"}, {"bot_id": " A "}, {"bot_id": None},
    {"bot_id": ""}, {"bot_id": "  "},
    {"user_id": "A", "user": "B"},  # user is not a routing identity field.
])
def test_chat_accepts_matching_or_header_only_identity(app_with_mock, body_identity, path):
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs["request_id"]
        return _FakePeer([
            {"type": "res", "id": rid, "ok": True, "payload": {"accepted": True}},
        ]), rid, "sess_1"

    dispatch.side_effect = _disp
    response = TestClient(app).post(
        path,
        json={"session_id": "sess_1", "query": "probe", "enable_streaming": False, **body_identity},
        headers={"X-Bot-Id": "A", "X-User-Id": "A"},
    )
    assert response.status_code == 200
    dispatch.assert_awaited_once()


def test_chat_completions_sse(app_with_mock):
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([
            {"type": "res", "id": rid, "ok": True, "payload": {"accepted": True}},
            {"type": "event", "event": "chat.delta", "payload": {"content": "hi"}},
            {"type": "event", "event": "chat.final", "payload": {"content": "hi"}},
        ])
        return peer, rid, "sess_1"

    dispatch.side_effect = _disp
    client = TestClient(app)
    with client.stream(
        "POST",
        "/api/v1/chat/completions",
        json={"session_id": "sess_1", "query": "hello"},
        headers={"Accept": "text/event-stream", "X-Request-Id": "r"},
    ) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())
    assert "event: chat.delta" in text
    assert "event: chat.final" in text
    dispatch.assert_called()
    assert dispatch.await_args.kwargs["method"] == "chat.send"


def test_chat_completions_sse_stays_open_after_dispatch_returns(app_with_mock):
    """Enterprise HTTP chat.send: dispatch returns when the Agent stream is only
    started. The SSE outbound must stay registered so later chat.delta/final
    still reach the browser (old done-callback closed it → accepted then EOF).
    """
    web_http_routes = sys.modules["jw_web_http_api_routes_under_test"]

    class _Channel:
        async def unregister_request_outbound(self, outbound):
            close = getattr(outbound, "close", None)
            if not callable(close):
                return
            result = close()
            if inspect.isawaitable(result):
                await result

    _, dispatch = app_with_mock
    app = FastAPI()
    web_http_routes.register_web_http_api_routes(app, _Channel())

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = outbound_mod.HttpSseOutbound(session_id="sess_1")
        peer.accept_frame({
            "type": "res",
            "id": rid,
            "ok": True,
            "payload": {"accepted": True},
        })

        async def _emit_after_dispatch_returns() -> None:
            await asyncio.sleep(0.05)
            if peer.closed:
                return
            peer.accept_frame({
                "type": "event",
                "event": "chat.delta",
                "payload": {"content": "hi"},
            })
            peer.accept_frame({
                "type": "event",
                "event": "chat.final",
                "payload": {"content": "hi"},
            })

        asyncio.create_task(_emit_after_dispatch_returns())
        return peer, rid, "sess_1"

    dispatch.side_effect = _disp
    client = TestClient(app)
    with client.stream(
        "POST",
        "/api/v1/chat/completions",
        json={"session_id": "sess_1", "query": "hello"},
        headers={"Accept": "text/event-stream", "X-Request-Id": "r"},
    ) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())
    assert "event: web.response" in text
    assert "event: chat.delta" in text
    assert "event: chat.final" in text


def test_chat_send_compat_path(app_with_mock):
    """Compat path /chat/send still works; OpenAPI only exposes /chat/completions."""
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([
            {"type": "res", "id": rid, "ok": True, "payload": {"accepted": True}},
            {"type": "event", "event": "chat.final", "payload": {}},
        ])
        return peer, rid, "sess_1"

    dispatch.side_effect = _disp
    client = TestClient(app)
    with client.stream(
        "POST",
        "/api/v1/chat/send",
        json={"session_id": "sess_1", "query": "hello"},
        headers={"Accept": "text/event-stream", "X-Request-Id": "r"},
    ) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())
    assert "event: chat.final" in text
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/chat/send" not in paths
    assert "/api/v1/chat/completions" in paths
    assert dispatch.await_args.kwargs["method"] == "chat.send"


def test_resolve_web_http_timeouts_defaults_and_env(monkeypatch: pytest.MonkeyPatch):
    for key in (
        "GATEWAY_WEB_HTTP_SSE_TIMEOUT",
        "GATEWAY_WEB_HTTP_SSE_IDLE_TIMEOUT",
        "GATEWAY_WEB_HTTP_SSE_KEEPALIVE",
        "GATEWAY_WEB_HTTP_UNARY_TIMEOUT",
        "GATEWAY_WEB_HTTP_HISTORY_TIMEOUT",
    ):
        monkeypatch.delenv(key, raising=False)
    mod = _load_module("jw_web_http_timeouts_default", TIMEOUTS_PATH)
    assert mod.resolve_web_http_sse_timeout() == 0.0
    assert mod.resolve_web_http_sse_idle_timeout() == 0.0
    assert mod.resolve_web_http_sse_keepalive() == 30.0
    assert mod.resolve_web_http_unary_timeout() == 120.0
    assert mod.resolve_web_http_history_timeout() == 60.0

    monkeypatch.setenv("GATEWAY_WEB_HTTP_SSE_TIMEOUT", "0")
    mod_zero = _load_module("jw_web_http_timeouts_zero", TIMEOUTS_PATH)
    assert mod_zero.resolve_web_http_sse_timeout() == 0.0

    monkeypatch.setenv("GATEWAY_WEB_HTTP_SSE_TIMEOUT", "90")
    monkeypatch.setenv("GATEWAY_WEB_HTTP_SSE_IDLE_TIMEOUT", "45")
    monkeypatch.setenv("GATEWAY_WEB_HTTP_UNARY_TIMEOUT", "15")
    mod2 = _load_module("jw_web_http_timeouts_env", TIMEOUTS_PATH)
    assert mod2.resolve_web_http_sse_timeout() == 90.0
    assert mod2.resolve_web_http_sse_idle_timeout() == 45.0
    assert mod2.resolve_web_http_unary_timeout() == 15.0


def test_chat_interrupt(app_with_mock):
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer(
            [
                {
                    "type": "res",
                    "id": rid,
                    "ok": True,
                    "payload": {
                        "accepted": True,
                        "session_id": "sess_1",
                        "intent": "cancel",
                        "event_type": "chat.interrupt_result",
                        "success": True,
                        "message": "任务已取消",
                    },
                }
            ]
        )
        return peer, rid, "sess_1"

    dispatch.side_effect = _disp
    client = TestClient(app)
    r = client.post("/api/v1/chat/sess_1/actions/interrupt", json={})
    assert r.status_code == 200
    assert dispatch.await_args.kwargs["method"] == "chat.interrupt"
    assert r.json()["data"]["event_type"] == "chat.interrupt_result"
    assert r.json()["data"]["success"] is True


web_http_routes = _load_module(
    "jw_web_http_routes_under_test",
    ROUTES_PATH,
)


def test_mapped_route_table_unique_http_surfaces():
    web_http_routes.validate_mapped_routes()
    keys = {
        (r.http_method, r.path) for r in web_http_routes.MAPPED_ROUTES
    }
    assert len(keys) == len(web_http_routes.MAPPED_ROUTES)
    rpc_methods = {r.rpc_method for r in web_http_routes.MAPPED_ROUTES}
    assert "config.get" in rpc_methods
    assert "models.list" in rpc_methods
    assert "locale.get_conf" in rpc_methods
    assert "locale.set_conf" in rpc_methods
    assert "cron.job.list" in rpc_methods
    assert "cron.job.run_now" in rpc_methods
    assert "permissions.tools.get" in rpc_methods
    assert "skills.enterprise.list" in rpc_methods
    assert "harness.export" in rpc_methods


def test_web_http_catalog_lists_settings_and_workspace(app_with_mock):
    app, _dispatch = app_with_mock
    client = TestClient(app)
    r = client.get("/api/v1/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    methods = {row["rpc_method"] for row in body["data"]["routes"]}
    assert "config.get" in methods
    assert "models.list" in methods
    assert "locale.set_conf" in methods
    assert "cron.job.preview" in methods
    assert "permissions.owner_scopes.get" in methods
    assert "skills.list" in methods
    assert "harness.packages" in methods
    assert "chat.send" in methods
    paths = {row["path"] for row in body["data"]["routes"]}
    assert "/api/v1/chat/{session_id}/actions/steer" not in paths
    assert "/api/v1/chat/{session_id}/actions/steer/status" not in paths
    assert "/api/v1/workspace/download" not in paths


def test_permissions_tools_get(app_with_mock):
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([
            {"type": "res", "id": rid, "ok": True, "payload": {"tools": {"bash": "ask"}}},
        ])
        return peer, rid, "s"

    dispatch.side_effect = _disp
    client = TestClient(app)
    r = client.get("/api/v1/permissions/tools")
    assert r.status_code == 200
    assert r.json()["data"]["tools"]["bash"] == "ask"
    assert r.headers["x-web-rpc-method"] == "permissions.tools.get"
    assert dispatch.await_args.kwargs["method"] == "permissions.tools.get"
    assert dispatch.await_args.kwargs["bind_session_param"] is False
    assert "session_id" not in (dispatch.await_args.kwargs.get("params") or {})


def test_permissions_tools_patch_path_param(app_with_mock):
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([
            {"type": "res", "id": rid, "ok": True, "payload": {"tools": {"bash": "deny"}}},
        ])
        return peer, rid, "s"

    dispatch.side_effect = _disp
    client = TestClient(app)
    r = client.patch("/api/v1/permissions/tools/bash", json={"level": "deny"})
    assert r.status_code == 200
    kwargs = dispatch.await_args.kwargs
    assert kwargs["method"] == "permissions.tools.update"
    assert kwargs["params"]["tool"] == "bash"
    assert kwargs["params"]["level"] == "deny"


def test_skills_enterprise_list_tenant_headers(app_with_mock):
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([
            {"type": "res", "id": rid, "ok": True, "payload": {"skills": []}},
        ])
        return peer, rid, "s"

    dispatch.side_effect = _disp
    client = TestClient(app)
    r = client.get(
        "/api/v1/skills/enterprise",
        headers={"X-Group-Id": "g1", "X-Bot-Id": "b1", "X-User-Id": "u1"},
    )
    assert r.status_code == 200
    params = dispatch.await_args.kwargs["params"]
    assert dispatch.await_args.kwargs["method"] == "skills.enterprise.list"
    assert params["group_id"] == "g1"
    assert params["bot_id"] == "b1"
    assert params["user_id"] == "u1"


def test_harness_packages_get(app_with_mock):
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([
            {
                "type": "res",
                "id": rid,
                "ok": True,
                "payload": {"packages": [], "active_package_ids": []},
            },
        ])
        return peer, rid, "s"

    dispatch.side_effect = _disp
    client = TestClient(app)
    r = client.get("/api/v1/harness/packages", headers={"X-Session-Id": "web_1"})
    assert r.status_code == 200
    assert dispatch.await_args.kwargs["method"] == "harness.packages"
    assert r.json()["metadata"]["rpc_method"] == "harness.packages"


def test_skills_evolution_get_forwards_name_query(app_with_mock):
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([
            {"type": "res", "id": rid, "ok": True, "payload": {"exists": False, "entries": []}},
        ])
        return peer, rid, "s"

    dispatch.side_effect = _disp
    client = TestClient(app)
    r = client.get(
        "/api/v1/skills/evolution?name=demo-skill&session_id=web_1",
        headers={"X-Session-Id": "web_1"},
    )
    assert r.status_code == 200
    params = dispatch.await_args.kwargs["params"]
    assert dispatch.await_args.kwargs["method"] == "skills.evolution.get"
    assert params["name"] == "demo-skill"
    assert params["session_id"] == "web_1"


def test_skills_skillnet_install_status_forwards_install_id(app_with_mock):
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([
            {"type": "res", "id": rid, "ok": True, "payload": {"success": True, "status": "pending"}},
        ])
        return peer, rid, "s"

    dispatch.side_effect = _disp
    client = TestClient(app)
    r = client.get(
        "/api/v1/skills/skillnet/install-status?install_id=job-abc&session_id=web_1",
        headers={"X-Session-Id": "web_1"},
    )
    assert r.status_code == 200
    params = dispatch.await_args.kwargs["params"]
    assert dispatch.await_args.kwargs["method"] == "skills.skillnet.install_status"
    assert params["install_id"] == "job-abc"
    assert params["session_id"] == "web_1"


def test_config_get(app_with_mock):
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([
            {"type": "res", "id": rid, "ok": True, "payload": {"app_version": "1.0"}},
        ])
        return peer, rid, "s"

    dispatch.side_effect = _disp
    client = TestClient(app)
    r = client.get("/api/v1/config")
    assert r.status_code == 200
    assert r.json()["data"]["app_version"] == "1.0"
    assert r.headers["x-web-rpc-method"] == "config.get"
    assert dispatch.await_args.kwargs["method"] == "config.get"
    assert dispatch.await_args.kwargs["bind_session_param"] is False


def test_models_list(app_with_mock):
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([
            {
                "type": "res",
                "id": rid,
                "ok": True,
                "payload": {"models": [{"model_name": "m1"}], "active_model": "m1"},
            },
        ])
        return peer, rid, "s"

    dispatch.side_effect = _disp
    client = TestClient(app)
    r = client.get("/api/v1/models")
    assert r.status_code == 200
    assert r.json()["data"]["active_model"] == "m1"
    assert dispatch.await_args.kwargs["method"] == "models.list"


def test_locale_put(app_with_mock):
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([
            {"type": "res", "id": rid, "ok": True, "payload": {"preferred_language": "en"}},
        ])
        return peer, rid, "s"

    dispatch.side_effect = _disp
    client = TestClient(app)
    r = client.put("/api/v1/locale", json={"preferred_language": "en"})
    assert r.status_code == 200
    assert dispatch.await_args.kwargs["method"] == "locale.set_conf"
    assert dispatch.await_args.kwargs["params"]["preferred_language"] == "en"


def test_cron_job_list_and_toggle(app_with_mock):
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([
            {"type": "res", "id": rid, "ok": True, "payload": {"jobs": []}},
        ])
        return peer, rid, "s"

    dispatch.side_effect = _disp
    client = TestClient(app)
    r = client.get(
        "/api/v1/cron/jobs",
        params={"project_id": "default"},
        headers={"X-Group-Id": "g1", "X-User-Id": "u1"},
    )
    assert r.status_code == 200
    assert dispatch.await_args.kwargs["method"] == "cron.job.list"
    params = dispatch.await_args.kwargs["params"]
    assert params["project_id"] == "default"
    assert params["group_id"] == "g1"
    assert params["user_id"] == "u1"

    async def _disp_toggle(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([
            {"type": "res", "id": rid, "ok": True, "payload": {"job": {"id": "job-1"}}},
        ])
        return peer, rid, "s"

    dispatch.side_effect = _disp_toggle
    r2 = client.post(
        "/api/v1/cron/jobs/job-1/actions/toggle",
        json={"enabled": True},
        headers={"X-Group-Id": "g1"},
    )
    assert r2.status_code == 200
    tparams = dispatch.await_args.kwargs["params"]
    assert dispatch.await_args.kwargs["method"] == "cron.job.toggle"
    assert tparams["id"] == "job-1"
    assert tparams["enabled"] is True
    assert tparams["group_id"] == "g1"


def test_cron_job_update_patch_body(app_with_mock):
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([
            {"type": "res", "id": rid, "ok": True, "payload": {"job": {"id": "job-1"}}},
        ])
        return peer, rid, "s"

    dispatch.side_effect = _disp
    client = TestClient(app)
    r = client.patch(
        "/api/v1/cron/jobs/job-1",
        json={"patch": {"name": "n2"}},
    )
    assert r.status_code == 200
    assert dispatch.await_args.kwargs["method"] == "cron.job.update"
    assert dispatch.await_args.kwargs["params"]["id"] == "job-1"
    assert dispatch.await_args.kwargs["params"]["patch"]["name"] == "n2"


def test_openapi_includes_settings_and_workspace(app_with_mock):
    app, _dispatch = app_with_mock
    spec = TestClient(app).get("/openapi.json").json()
    paths = spec["paths"]
    assert "/api/v1/config" in paths
    assert "/api/v1/models" in paths
    assert "/api/v1/locale" in paths
    assert "/api/v1/cron/jobs" in paths
    assert "/api/v1/cron/jobs/{id}/actions/run-now" in paths
    assert "/api/v1/permissions/tools" in paths
    assert "/api/v1/skills/enterprise" in paths
    assert "/api/v1/harness/packages" in paths
    assert "/api/v1/catalog" in paths


def _fill_mapped_path(path: str) -> str:
    return (
        path.replace("{tool}", "bash")
        .replace("{id}", "rule-1")
        .replace("{name}", "demo-skill")
        .replace("{package_id}", "native")
    )


def test_mapped_routes_dispatch_rpc_method(app_with_mock):
    """Every table-driven HTTP surface reaches dispatch with the declared rpc_method."""
    app, dispatch = app_with_mock

    async def _disp(*args, **kwargs):
        rid = kwargs.get("request_id") or "r"
        peer = _FakePeer([{"type": "res", "id": rid, "ok": True, "payload": {"ok": True}}])
        return peer, rid, "s"

    dispatch.side_effect = _disp
    client = TestClient(app)
    failures: list[str] = []
    for route in web_http_routes.MAPPED_ROUTES:
        url = "/api/v1" + _fill_mapped_path(route.path)
        req_kwargs: dict[str, Any] = {}
        if route.accept_body:
            req_kwargs["json"] = {}
        resp = client.request(route.http_method, url, **req_kwargs)
        expect = 201 if route.created else 200
        got_method = (
            dispatch.await_args.kwargs.get("method") if dispatch.await_args else None
        )
        if resp.status_code != expect or got_method != route.rpc_method:
            failures.append(
                f"{route.http_method} {url} -> {resp.status_code} "
                f"method={got_method!r} want {expect}/{route.rpc_method} body={resp.text[:200]}"
            )
    assert not failures, "\n".join(failures)
