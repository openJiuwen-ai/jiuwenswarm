# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.common.auth.model_catalog import LoginModel
from jiuwenswarm.common.auth import login_credentials
from jiuwenswarm.common.auth.login_credentials import placeholder_api_key
from jiuwenswarm.common.e2a.constants import E2A_MODEL_AUTH_PARAM_KEY
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

REF = "d8699b326b88c40dc63e2834eb0c74da"
SCOPED = {
    E2A_MODEL_AUTH_PARAM_KEY: {
        "api_base": "https://apig.example.com/v1",
        "api_key": "id-user",
        "credential_ref": REF,
    }
}


@pytest.fixture(autouse=True)
def _clean_registry():
    login_credentials.reset_for_test()
    yield
    login_credentials.reset_for_test()


@pytest.fixture
def adapter(monkeypatch):
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._model_cache = {"my-gpt": SimpleNamespace(name="my-gpt")}
    adapter._model_name_to_keys = {"my-gpt": ["my-gpt"]}
    adapter._last_resolved_model = None
    monkeypatch.setattr(
        "jiuwenswarm.common.auth.model_catalog.get_models",
        lambda session_id=None, allow_refresh=True: [
            LoginModel(model_name="glm-5", display_name="GLM-5"),
            LoginModel(model_name="my-gpt", display_name="my-gpt"),
        ],
    )
    monkeypatch.setattr(adapter, "_has_valid_model_config", lambda _name="": True)
    return adapter


def _request(model_name: str, **params):
    return SimpleNamespace(params={"model_name": model_name, **params}, session_id="s")


def test_free_model_without_credentials_is_refused_not_silently_swapped(adapter):
    assert adapter._model_config_error(_request("glm-5")) == (
        "login_required",
        "该模型需要登录华为账号后使用（未登录或登录已过期），请登录后重试",
    )
    assert adapter._model_config_error(_request("glm-5#7"))[0] == "login_required"


def test_free_model_with_request_scoped_credentials_passes(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_has_valid_model_config", lambda _name="": False)
    assert adapter._model_config_error(_request("glm-5", **SCOPED)) is None


def test_user_configured_model_with_the_same_name_wins(adapter):
    assert adapter._model_config_error(_request("my-gpt")) is None


def test_scoped_credentials_build_the_model_with_this_users_token(adapter, monkeypatch):
    built = {}

    def fake_build(mcc, mco):
        built.update(mcc)
        return SimpleNamespace(mcc=mcc)

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.build_model_from_entry", fake_build
    )
    assert adapter._request_scoped_login_model(_request("glm-5#3", **SCOPED), "glm-5#3") is not None
    assert built["model_name"] == "glm-5"
    assert built["api_key"] == placeholder_api_key(REF)
    assert login_credentials._lookup(REF).token == "id-user"
    assert built["api_base"] == "https://apig.example.com/v1"


@pytest.mark.parametrize(
    "auth",
    [
        None,
        "x",
        {},
        {"api_base": "https://a"},
        {"api_key": "k"},
        {"api_base": " ", "api_key": " "},
        {"api_base": "https://a", "api_key": "k"},  # 缺句柄
    ],
)
def test_incomplete_scoped_credentials_are_ignored(auth):
    params = {} if auth is None else {E2A_MODEL_AUTH_PARAM_KEY: auth}
    assert JiuWenSwarmDeepAdapter._scoped_login_auth(SimpleNamespace(params=params)) is None


def test_model_check_records_whether_the_session_uses_a_free_model(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_has_valid_model_config", lambda _name="": True)
    adapter._model_config_error(_request("glm-5", **SCOPED))
    assert login_credentials.session_uses_login_model("s") is True

    adapter._model_config_error(SimpleNamespace(params={}, session_id="s"))
    assert login_credentials.session_uses_login_model("s") is True

    adapter._model_config_error(_request("my-gpt"))
    assert login_credentials.session_uses_login_model("s") is False
