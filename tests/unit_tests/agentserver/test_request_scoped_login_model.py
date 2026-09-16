# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import pytest

from jiuwenswarm.common.auth import login_credentials
from jiuwenswarm.common.auth.login_credentials import placeholder_api_key
from jiuwenswarm.common.e2a.constants import E2A_MODEL_AUTH_PARAM_KEY
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    build_model_from_entry,
)

API_BASE = "https://apig.example.com/v1"
ID_TOKEN = "id-token-of-this-user"
REF = "abe633f3a47a2758174eabe9160daf36"
AUTH = {"api_base": API_BASE, "api_key": ID_TOKEN, "credential_ref": REF}


@pytest.fixture(autouse=True)
def _clean_registry():
    login_credentials.reset_for_test()
    yield
    login_credentials.reset_for_test()


def _adapter() -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._last_resolved_model = None
    return adapter


def _request(auth=None, model_name: str = "GLM-5.2") -> AgentRequest:
    params = {"model_name": model_name}
    if auth is not None:
        params[E2A_MODEL_AUTH_PARAM_KEY] = auth
    return AgentRequest(request_id="req-1", params=params)


def test_builds_the_model_from_the_forwarded_id_token():
    adapter = _adapter()
    model = adapter._request_scoped_login_model(_request(AUTH), "GLM-5.2")
    mcc = model.model_client_config
    assert mcc.api_base == API_BASE
    assert mcc.api_key == placeholder_api_key(REF)
    assert ID_TOKEN not in mcc.api_key
    assert login_credentials._lookup(REF).token == ID_TOKEN
    assert adapter._last_resolved_model is model


def test_global_index_suffix_is_stripped_from_the_model_name():
    model = _adapter()._request_scoped_login_model(_request(AUTH), "GLM-5.2#3")
    assert model.model_config.model_name == "GLM-5.2"


@pytest.mark.parametrize(
    "auth",
    [
        None,
        "not-a-dict",
        {"api_base": API_BASE, "credential_ref": REF},
        {"api_key": ID_TOKEN, "credential_ref": REF},
        {"api_base": "  ", "api_key": ID_TOKEN, "credential_ref": REF},
        {"api_base": API_BASE, "api_key": ID_TOKEN},
    ],
)
def test_without_usable_credentials_falls_back_to_normal_resolution(auth):
    assert _adapter()._request_scoped_login_model(_request(auth), "GLM-5.2") is None


PLACEHOLDER_ENTRY = {  # .env 模板里的占位值：新装、没配过模型
    "model_name": "your-model-name",
    "api_base": "https://example.com/compatible-mode/v1",
    "api_key": "sk-xxxxxxxxx",
    "client_provider": "OpenAI",
}


def _adapter_whose_default_is(entry) -> JiuWenSwarmDeepAdapter:
    adapter = _adapter()
    adapter._requested_model_name = lambda _request: ""
    adapter._is_uncredentialed_login_model = lambda _request, _requested: False
    adapter._resolve_model_by_name = lambda _name: build_model_from_entry(dict(entry), {})
    return adapter


def test_placeholder_default_model_asks_the_user_to_configure_or_sign_in():
    code, message = _adapter_whose_default_is(PLACEHOLDER_ENTRY)._model_config_error(_request())
    assert code == "model_not_configured"
    assert "登录" in message


def test_forwarded_login_credentials_skip_the_check():
    assert _adapter_whose_default_is(PLACEHOLDER_ENTRY)._model_config_error(_request(AUTH)) is None


def test_model_check_registers_the_token_even_when_the_model_is_not_rebuilt():
    adapter = _adapter_whose_default_is(PLACEHOLDER_ENTRY)
    adapter._model_config_error(_request({**AUTH, "api_key": "id-token-1"}))
    adapter._model_config_error(_request({**AUTH, "api_key": "id-token-2"}))
    assert login_credentials._lookup(REF).token == "id-token-2"


def test_usable_model_passes():
    entry = {**PLACEHOLDER_ENTRY, "model_name": "my-gpt", "api_base": "https://api.openai.com/v1", "api_key": "sk-real"}
    assert _adapter_whose_default_is(entry)._model_config_error(_request()) is None
