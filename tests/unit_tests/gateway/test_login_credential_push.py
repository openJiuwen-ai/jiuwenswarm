# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.auth import login_credentials as lc
from jiuwenswarm.common.auth.login_credentials import credential_ref_for_user
from jiuwenswarm.common.e2a.constants import E2A_MODEL_AUTH_PARAM_KEY
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler

REF = "abe633f3a47a2758174eabe9160daf36"


@pytest.fixture(autouse=True)
def _clean_registry():
    lc.reset_for_test()
    yield
    lc.reset_for_test()


def _handler(sent: list) -> MessageHandler:
    handler = object.__new__(MessageHandler)

    async def _send(env):
        sent.append(env)
        return SimpleNamespace(ok=True, payload={"applied": True})

    handler._send_non_stream_agent_request = _send
    return handler


def _refresh_chunk(ref: str = REF) -> SimpleNamespace:
    return SimpleNamespace(payload={"event_type": lc.CREDENTIAL_REFRESH_EVENT, "credential_ref": ref})


@pytest.mark.asyncio
async def test_refresh_request_is_answered_with_an_update(monkeypatch):
    seen_refs: list[str] = []

    def _refresh(ref):
        seen_refs.append(ref)
        return {"credential_ref": ref, "api_key": "id-new"}

    monkeypatch.setattr("jiuwenswarm.common.auth.passthrough.refreshed_credential_for_ref", _refresh)
    sent: list = []

    assert await _handler(sent)._handle_login_credential_refresh_push(_refresh_chunk()) is True

    assert seen_refs == [REF]
    assert len(sent) == 1
    assert sent[0].method == ReqMethod.AUTH_CREDENTIALS_UPDATE.value
    assert sent[0].params == {"credential_ref": REF, "api_key": "id-new"}


@pytest.mark.asyncio
async def test_refresh_runs_off_the_event_loop(monkeypatch):
    import threading

    loop_thread = threading.get_ident()
    ran_in: list[int] = []

    def _refresh(ref):
        ran_in.append(threading.get_ident())
        return None

    monkeypatch.setattr("jiuwenswarm.common.auth.passthrough.refreshed_credential_for_ref", _refresh)
    await _handler([])._handle_login_credential_refresh_push(_refresh_chunk())
    assert ran_in and ran_in[0] != loop_thread


@pytest.mark.asyncio
async def test_failed_refresh_sends_nothing_and_is_still_consumed(monkeypatch):
    def _boom(ref):
        raise RuntimeError("exchange service down")

    monkeypatch.setattr("jiuwenswarm.common.auth.passthrough.refreshed_credential_for_ref", _boom)
    sent: list = []
    assert await _handler(sent)._handle_login_credential_refresh_push(_refresh_chunk()) is True
    assert sent == []


@pytest.mark.asyncio
async def test_other_pushes_are_not_touched():
    sent: list = []
    handler = _handler(sent)
    assert await handler._handle_login_credential_refresh_push(SimpleNamespace(payload={"event_type": "cron.response"})) is False
    assert await handler._handle_login_credential_refresh_push(SimpleNamespace(payload=None)) is False
    assert sent == []


@pytest.mark.asyncio
async def test_agent_server_applies_the_pushed_update(monkeypatch):
    from jiuwenswarm.server import agent_ws_server as module

    lc.login_auth_from_params(
        {E2A_MODEL_AUTH_PARAM_KEY: {"api_base": "https://apig.example.com/v1", "api_key": "id-old", "credential_ref": REF}}
    )
    sent: list = []

    async def _capture(_ws, wire):
        sent.append(wire)

    monkeypatch.setattr(module, "send_wire_payload", _capture)
    monkeypatch.setattr(
        module,
        "encode_agent_response_for_wire",
        lambda response, *, response_id: {"response_id": response_id, "ok": response.ok, "payload": response.payload},
    )
    server = module.AgentWebSocketServer.__new__(module.AgentWebSocketServer)

    async def _call(params):
        request = AgentRequest(
            request_id="auth-1",
            channel_id="",
            req_method=ReqMethod.AUTH_CREDENTIALS_UPDATE,
            timestamp=time.time(),
            params=params,
        )
        await server._handle_auth_credentials_update(object(), request, asyncio.Lock())
        return sent[-1]

    assert (await _call({"credential_ref": REF, "api_key": "id-new"}))["ok"] is True
    assert lc._lookup(REF).token == "id-new"

    unknown = "ef765dc40dd691dc34f6567937da5449"
    assert (await _call({"credential_ref": unknown, "api_key": "x"}))["ok"] is False

    assert (await _call({"credential_ref": REF, "revoked": True}))["ok"] is True
    assert lc._lookup(REF) is None


@pytest.mark.asyncio
async def test_logout_revokes_the_agent_server_credential(monkeypatch):
    from jiuwenswarm.gateway.channel_manager.web import web_http_auth

    monkeypatch.setattr(lc, "_ref_key_cache", b"k" * 32)
    sent: list = []
    handler = _handler(sent)
    monkeypatch.setattr(MessageHandler, "try_get_instance", classmethod(lambda cls: handler))
    await web_http_auth._revoke_agent_server_credential("openid-1")
    assert len(sent) == 1
    assert sent[0].params == {"credential_ref": credential_ref_for_user("openid-1"), "revoked": True}


@pytest.mark.asyncio
async def test_logout_revoke_is_best_effort(monkeypatch):
    from jiuwenswarm.gateway.channel_manager.web import web_http_auth

    monkeypatch.setattr(lc, "_ref_key_cache", b"k" * 32)
    monkeypatch.setattr(MessageHandler, "try_get_instance", classmethod(lambda cls: None))
    await web_http_auth._revoke_agent_server_credential("openid-1")  # 没有 AgentServer 连接也不能抛

    handler = object.__new__(MessageHandler)

    async def _down(_env):
        raise ConnectionError("agent server gone")

    handler._send_non_stream_agent_request = _down
    monkeypatch.setattr(MessageHandler, "try_get_instance", classmethod(lambda cls: handler))
    await web_http_auth._revoke_agent_server_credential("openid-1")


_AUTH_FAILURE = "Error code: 401 - {'error_msg': 'Incorrect authentication information: frontend authorizer', 'error_code': 'APIG.0305'}"


def test_errors_are_classified_only_for_sessions_using_a_free_model():
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    lc.note_session_model("free-session", uses_login_model=True)
    free = {"event_type": "chat.error", "error": _AUTH_FAILURE}
    AgentWebSocketServer._annotate_model_error(free, "free-session")
    assert free["code"] == "login_required" and free["upstream"] is True

    own = {"event_type": "chat.error", "error": "Error code: 429 - Too Many Requests"}
    AgentWebSocketServer._annotate_model_error(own, "own-model-session")
    assert "code" not in own, "用户自配模型的限流不能说成免费额度用完"


def test_explicit_upstream_code_is_kept():
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    lc.note_session_model("free-session", uses_login_model=True)
    payload = {"event_type": "chat.error", "error": _AUTH_FAILURE, "code": "model_not_configured"}
    AgentWebSocketServer._annotate_model_error(payload, "free-session")
    assert payload["code"] == "model_not_configured"


def test_switching_to_an_own_model_clears_the_flag():
    lc.note_session_model("s", uses_login_model=True)
    lc.note_session_model("s", uses_login_model=False)
    assert lc.session_uses_login_model("s") is False


@pytest.mark.asyncio
async def test_team_errors_pushed_without_a_request_are_classified(monkeypatch):
    from jiuwenswarm.server import agent_ws_server as module

    lc.note_session_model("team-session", uses_login_model=True)
    captured: list = []
    monkeypatch.setattr(module, "build_server_push_wire", lambda msg: captured.append(msg) or {"ok": True})

    async def _send(_ws, _wire):
        return True

    monkeypatch.setattr(module, "send_wire_payload", _send)
    server = module.AgentWebSocketServer.__new__(module.AgentWebSocketServer)
    server._current_ws = object()
    server._current_send_lock = asyncio.Lock()
    original = {"event_type": "chat.error", "error": _AUTH_FAILURE}
    assert await server.send_push({"session_id": "team-session", "channel_id": "web", "payload": original}) is True
    assert captured[0]["payload"]["code"] == "login_required"
    assert "code" not in original, "不改调用方传进来的 dict"
