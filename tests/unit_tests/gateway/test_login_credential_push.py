# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from agent_gateway.auth import login_credentials as lc
from agent_gateway.auth.login_credentials import credential_ref_for_user
from e2a.constants import E2A_MODEL_AUTH_PARAM_KEY
from e2a.agent_models import AgentRequest
from e2a.message import ReqMethod
from agent_gateway.message_handler.message_handler import MessageHandler

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
async def test_agent_server_applies_the_pushed_update():
    from jiuwenswarm.server.context import RequestContext
    from jiuwenswarm.server.handlers.ops import handle_auth_credentials_update

    lc.login_auth_from_params(
        {E2A_MODEL_AUTH_PARAM_KEY: {"api_base": "https://apig.example.com/v1", "api_key": "id-old", "credential_ref": REF}}
    )
    sent: list = []

    class _Sink:
        async def send_response(self, response):
            sent.append(response)

    async def _call(params):
        request = AgentRequest(
            request_id="auth-1",
            channel_id="",
            req_method=ReqMethod.AUTH_CREDENTIALS_UPDATE,
            timestamp=time.time(),
            params=params,
        )
        await handle_auth_credentials_update(
            RequestContext(
                request=request,
                sink=_Sink(),
                connection_id="1",
                services=SimpleNamespace(),
            )
        )
        return sent[-1]

    assert (await _call({"credential_ref": REF, "api_key": "id-new"})).ok is True
    assert lc._lookup(REF).token == "id-new"

    unknown = "ef765dc40dd691dc34f6567937da5449"
    assert (await _call({"credential_ref": unknown, "api_key": "x"})).ok is False

    assert (await _call({"credential_ref": REF, "revoked": True})).ok is True
    assert lc._lookup(REF) is None


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
