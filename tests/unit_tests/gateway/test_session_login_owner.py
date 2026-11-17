# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent_gateway.auth import session_owners
from e2a.constants import E2A_LOGIN_REQUIRED_HINT_PARAM_KEY, E2A_MODEL_AUTH_PARAM_KEY
from agent_gateway.message_handler.message_handler import (
    _LOGIN_HINT_NO_OWNER,
    _LOGIN_HINT_OWNER_LOGGED_OUT,
    MessageHandler,
)

REF = "a" * 32


@pytest.fixture(autouse=True)
def _owners_in_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr("agent_gateway.auth.session_store.auth_dir", lambda: tmp_path)
    session_owners.reset_for_test()
    yield
    session_owners.reset_for_test()


def _run(env, *, session_model: str = "GLM-5.2") -> dict:
    handler = SimpleNamespace(agent_client=None)

    async def _effective_session_model(sid, params, user_id=None):
        return params.get("model_name") or session_model

    handler._effective_session_model = _effective_session_model
    asyncio.run(MessageHandler._apply_session_login_owner(handler, env))
    return env.params


def _env(channel: str, params: dict, session_id: str = "web_s1") -> SimpleNamespace:
    return SimpleNamespace(channel=channel, session_id=session_id, method="chat.send", params=params)


def test_agent_server_uses_the_gateway_hint_for_login_required():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    def _message(params):
        return JiuWenSwarmDeepAdapter._login_required_message(SimpleNamespace(params=params))

    assert _message({E2A_LOGIN_REQUIRED_HINT_PARAM_KEY: _LOGIN_HINT_NO_OWNER}) == _LOGIN_HINT_NO_OWNER
    assert _message({}) == "该模型需要登录华为账号后使用（未登录或登录已过期），请登录后重试"


