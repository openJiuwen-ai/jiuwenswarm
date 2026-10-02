# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.auth import session_owners
from jiuwenswarm.common.e2a.constants import E2A_LOGIN_REQUIRED_HINT_PARAM_KEY, E2A_MODEL_AUTH_PARAM_KEY
from jiuwenswarm.gateway.message_handler.message_handler import (
    _LOGIN_HINT_NO_OWNER,
    _LOGIN_HINT_OWNER_LOGGED_OUT,
    MessageHandler,
)

REF = "a" * 32


@pytest.fixture(autouse=True)
def _owners_in_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr("jiuwenswarm.common.auth.session_store.auth_dir", lambda: tmp_path)
    session_owners.reset_for_test()
    yield
    session_owners.reset_for_test()


def _run(env, *, session_model: str = "GLM-5.2") -> dict:
    handler = SimpleNamespace(agent_client=None)
    handler._effective_session_model = lambda sid, params: params.get("model_name") or session_model
    asyncio.run(MessageHandler._apply_session_login_owner(handler, env))
    return env.params


def _env(channel: str, params: dict, session_id: str = "web_s1") -> SimpleNamespace:
    return SimpleNamespace(channel=channel, session_id=session_id, method="chat.send", params=params)


def test_feishu_reply_uses_the_session_owners_credentials(monkeypatch):
    calls: list = []

    def _owner_auth(owner):
        calls.append((owner.model_name, owner.credential_ref))
        return {"api_base": "https://apig", "api_key": "id-owner", "credential_ref": owner.credential_ref}

    monkeypatch.setattr(session_owners, "login_auth_for_owner", _owner_auth)

    # 网页端带着登录会话用免费模型：记下主人
    _run(_env("web", {"model_name": "GLM-5.2#3", E2A_MODEL_AUTH_PARAM_KEY: {"credential_ref": REF}}))
    assert session_owners.lookup("web_s1") == session_owners.SessionOwner(REF, "GLM-5.2")

    # 飞书回复进同一会话、没带模型：按主人挂凭据
    params = _run(_env("feishu", {"query": "确认回复"}))
    assert params[E2A_MODEL_AUTH_PARAM_KEY]["api_key"] == "id-owner"
    # 显式带上模型：集群组装只认请求里的 model_name
    assert params["model_name"] == "GLM-5.2"
    assert calls == [("GLM-5.2", REF)]

    # 会话已经换成别的模型：不挂
    assert E2A_MODEL_AUTH_PARAM_KEY not in _run(_env("feishu", {"query": "x"}), session_model="my-model")
    # 没登录的浏览器：不代用主人的额度，也不带（伪造的）提示
    web = _run(_env("web", {"model_name": "GLM-5.2", E2A_LOGIN_REQUIRED_HINT_PARAM_KEY: "伪造"}))
    assert E2A_MODEL_AUTH_PARAM_KEY not in web and E2A_LOGIN_REQUIRED_HINT_PARAM_KEY not in web
    # 别的会话：没有主人，提示先去网页端发一条
    other = _run(_env("feishu", {"query": "x"}, session_id="web_s2"))
    assert E2A_MODEL_AUTH_PARAM_KEY not in other
    assert other[E2A_LOGIN_REQUIRED_HINT_PARAM_KEY] == _LOGIN_HINT_NO_OWNER
    assert len(calls) == 1


def test_owner_auth_uses_the_existing_refresh_path(monkeypatch):
    from jiuwenswarm.common.auth.model_catalog import LoginModel

    monkeypatch.setattr(
        "jiuwenswarm.common.auth.model_catalog.get_models",
        lambda session_id=None, allow_refresh=True: [LoginModel("GLM-5.2", "GLM-5.2")],
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.auth.apig.resolve_apig_config",
        lambda allow_refresh=True: SimpleNamespace(invoke_base_url="https://apig/v1"),
    )
    fresh = {"credential_ref": REF, "api_key": "id-new"}
    monkeypatch.setattr("jiuwenswarm.common.auth.passthrough.refreshed_credential_for_ref", lambda ref: fresh)

    owner = session_owners.SessionOwner(REF, "GLM-5.2")
    assert session_owners.login_auth_for_owner(owner) == {
        "api_base": "https://apig/v1", "api_key": "id-new", "credential_ref": REF,
    }
    # 主人已登出
    fresh = {"credential_ref": REF, "revoked": True}
    assert session_owners.login_auth_for_owner(owner) is None
    # 模型已不在免费模型目录（活动结束 / 下架）：不再走免费通道
    assert session_owners.login_auth_for_owner(session_owners.SessionOwner(REF, "GLM-5.1")) is None


def test_feishu_reply_when_owner_logged_out_says_so(monkeypatch):
    monkeypatch.setattr(session_owners, "login_auth_for_owner", lambda owner: None)
    session_owners.remember("web_s1", REF, "GLM-5.2")

    params = _run(_env("feishu", {"query": "x"}))
    assert E2A_MODEL_AUTH_PARAM_KEY not in params
    assert params[E2A_LOGIN_REQUIRED_HINT_PARAM_KEY] == _LOGIN_HINT_OWNER_LOGGED_OUT


def test_agent_server_uses_the_gateway_hint_for_login_required():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    def _message(params):
        return JiuWenSwarmDeepAdapter._login_required_message(SimpleNamespace(params=params))

    assert _message({E2A_LOGIN_REQUIRED_HINT_PARAM_KEY: _LOGIN_HINT_NO_OWNER}) == _LOGIN_HINT_NO_OWNER
    assert _message({}) == "该模型需要登录华为账号后使用（未登录或登录已过期），请登录后重试"
