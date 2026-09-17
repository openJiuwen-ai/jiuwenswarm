# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import httpx
import pytest

from jiuwenswarm.common.auth import login_credentials
from jiuwenswarm.common.auth.login_credentials import (
    build_login_model_entry,
    inject_login_credential,
    login_auth_from_params,
    placeholder_api_key,
)
from jiuwenswarm.common.e2a.constants import E2A_MODEL_AUTH_PARAM_KEY

API_BASE = "https://apig.example.com/v1"
REF = "abe633f3a47a2758174eabe9160daf36"


@pytest.fixture(autouse=True)
def _clean_registry():
    login_credentials.reset_for_test()
    yield
    login_credentials.reset_for_test()


def _params(token="id-token-1", ref=REF, api_base=API_BASE, model_name="glm-5"):
    return {
        "model_name": model_name,
        E2A_MODEL_AUTH_PARAM_KEY: {"api_base": api_base, "api_key": token, "credential_ref": ref},
    }


async def _send(url: str, authorization: str | None) -> httpx.Request:
    request = httpx.Request("POST", url, headers={"Authorization": authorization} if authorization else {})
    await inject_login_credential(request)
    return request


def test_forwarded_credentials_are_registered_and_replaced_by_a_placeholder():
    auth = login_auth_from_params(_params())
    assert auth is not None
    assert auth.api_base == API_BASE
    assert auth.api_key == placeholder_api_key(REF)
    assert "id-token-1" not in auth.api_key


@pytest.mark.parametrize(
    "params",
    [
        None,
        {},
        {E2A_MODEL_AUTH_PARAM_KEY: "not-a-dict"},
        _params(token=""),
        _params(api_base=""),
        _params(ref=""),
        _params(ref="sess-1"),  # 不是句柄形状：宁可当没带，也不拿会话 id 去建配置
    ],
)
def test_incomplete_credentials_count_as_absent(params):
    assert login_auth_from_params(params) is None


def test_login_model_entry_uses_the_bare_name_and_the_placeholder():
    entry = build_login_model_entry(_params(), "glm-5#3")
    assert entry is not None
    mcc = entry["model_client_config"]
    assert mcc["model_name"] == "glm-5"
    assert mcc["api_base"] == API_BASE
    assert mcc["api_key"] == placeholder_api_key(REF)


def test_no_login_model_entry_without_credentials():
    assert build_login_model_entry({"model_name": "glm-5"}, "glm-5") is None


@pytest.mark.asyncio
async def test_placeholder_is_swapped_for_the_registered_token():
    login_auth_from_params(_params(token="id-token-1"))
    request = await _send(f"{API_BASE}/chat/completions", f"Bearer {placeholder_api_key(REF)}")
    assert request.headers["Authorization"] == "Bearer id-token-1"


@pytest.mark.asyncio
async def test_a_later_request_refreshes_the_token_for_existing_models():
    """集群里常驻的成员不重建：配置里的占位值不变，发出去的 token 要跟着最新的登记走。"""
    login_auth_from_params(_params(token="id-token-1"))
    login_auth_from_params(_params(token="id-token-2"))
    request = await _send(f"{API_BASE}/chat/completions", f"Bearer {placeholder_api_key(REF)}")
    assert request.headers["Authorization"] == "Bearer id-token-2"


@pytest.mark.asyncio
async def test_users_do_not_share_tokens():
    other_ref = "2b8ea975811361aee25cfeb50cbe084a"
    login_auth_from_params(_params(token="id-alice", ref=REF))
    login_auth_from_params(_params(token="id-bob", ref=other_ref))
    alice = await _send(f"{API_BASE}/chat/completions", f"Bearer {placeholder_api_key(REF)}")
    bob = await _send(f"{API_BASE}/chat/completions", f"Bearer {placeholder_api_key(other_ref)}")
    assert alice.headers["Authorization"] == "Bearer id-alice"
    assert bob.headers["Authorization"] == "Bearer id-bob"


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Bearer sk-user-own-key", "Basic abc"])
async def test_requests_without_a_placeholder_are_untouched(authorization):
    login_auth_from_params(_params())
    request = await _send(f"{API_BASE}/chat/completions", authorization)
    assert request.headers.get("Authorization") == authorization


@pytest.mark.asyncio
async def test_unknown_handle_sends_no_authorization_at_all():
    request = await _send(f"{API_BASE}/chat/completions", f"Bearer {placeholder_api_key(REF)}")
    assert "Authorization" not in request.headers


@pytest.mark.asyncio
async def test_expired_registration_sends_no_authorization(monkeypatch):
    login_auth_from_params(_params())
    now = login_credentials.time.time()
    monkeypatch.setattr(
        login_credentials.time, "time", lambda: now + login_credentials.REGISTRATION_TTL_S + 1
    )
    request = await _send(f"{API_BASE}/chat/completions", f"Bearer {placeholder_api_key(REF)}")
    assert "Authorization" not in request.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example.com/v1/chat/completions",
        "http://apig.example.com/v1/chat/completions",  # 降级到明文
        "https://apig.example.com:8443/v1/chat/completions",
        "https://apig.example.com/v1evil/chat/completions",
    ],
)
async def test_token_never_leaves_the_registered_endpoint(url):
    login_auth_from_params(_params(token="id-token-1"))
    request = await _send(url, f"Bearer {placeholder_api_key(REF)}")
    assert "Authorization" not in request.headers


@pytest.mark.asyncio
async def test_stale_registrations_are_pruned_on_write(monkeypatch):
    login_auth_from_params(_params(ref=REF))
    now = login_credentials.time.time()
    monkeypatch.setattr(
        login_credentials.time, "time", lambda: now + login_credentials.REGISTRATION_TTL_S + 1
    )
    login_auth_from_params(_params(ref="2b8ea975811361aee25cfeb50cbe084a"))
    assert REF not in login_credentials._registry


@pytest.mark.asyncio
async def test_patch_hooks_real_sdk_clients_and_is_idempotent(monkeypatch):
    from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
    from openjiuwen.core.foundation.llm.model_clients.openai_model_client import OpenAIModelClient

    monkeypatch.setattr(
        OpenAIModelClient, "_build_async_openai_client", OpenAIModelClient._build_async_openai_client
    )

    for _ in range(3):
        assert login_credentials.apply_login_credential_patch() is True

    client = OpenAIModelClient(
        ModelRequestConfig(model="glm-5"),
        ModelClientConfig(client_provider="OpenAI", api_key=placeholder_api_key(REF), api_base=API_BASE),
    )
    hooks = client._build_async_openai_client()._client.event_hooks["request"]
    assert hooks.count(inject_login_credential) == 1, "重复打补丁不能挂两遍"
