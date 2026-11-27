"""Authorization must survive openjiuwen sanitize for Huawei MaaS Basic auth."""

from __future__ import annotations

import asyncio

from openjiuwen.core.foundation.llm import headers_helper
from openjiuwen.core.foundation.llm.model_clients import openai_model_client as oai_client_mod
from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig

from jiuwenswarm.common.local_env_config import bind_task_env_overlay, reset_task_env_overlay
from jiuwenswarm.llm_sse_patch import apply_openai_auth_header_patch


def test_build_base_headers_keeps_authorization() -> None:
    apply_openai_auth_header_patch()
    headers = headers_helper.build_base_headers(
        custom_headers={
            "Authorization": "Basic dGVzdA==",
            "X-Extra": "1",
        }
    )
    assert headers.get("Authorization") == "Basic dGVzdA=="
    assert headers.get("X-Extra") == "1"


def test_openai_client_module_binding_keeps_authorization() -> None:
    """from-import binding on the client module must also be patched."""
    apply_openai_auth_header_patch()
    headers = oai_client_mod.build_base_headers(
        custom_headers={"Authorization": "Basic Y2xpZW50"}
    )
    assert headers.get("Authorization") == "Basic Y2xpZW50"


def test_merge_request_headers_keeps_request_authorization() -> None:
    apply_openai_auth_header_patch()
    headers = headers_helper.merge_request_headers(
        {"X-Base": "b"},
        {"Authorization": "Basic cmVx", "X-Req": "r"},
    )
    assert headers.get("Authorization") == "Basic cmVx"
    assert headers.get("X-Base") == "b"
    assert headers.get("X-Req") == "r"


def test_maas_placeholder_uses_task_overlay_authorization_for_async_client() -> None:
    """DeepResearch SDK omits custom_headers but binds the trusted task overlay."""
    apply_openai_auth_header_patch()
    token = bind_task_env_overlay(
        {"default_headers": '{"Authorization":"Basic ZGVlcC1yZXNlYXJjaA=="}'}
    )
    client = None
    try:
        model_client = oai_client_mod.OpenAIModelClient(
            ModelRequestConfig(),
            ModelClientConfig(
                api_key="huawei-maas-session",
                api_base="https://example.invalid/v1",
                client_provider="OpenAI",
                use_shared_llm_http_client=False,
            ),
        )
        client = model_client._create_async_openai_client()
        assert client._custom_headers.get("Authorization") == "Basic ZGVlcC1yZXNlYXJjaA=="
    finally:
        reset_task_env_overlay(token)
        if client is not None:
            asyncio.run(client.close())


def test_regular_api_key_does_not_use_task_overlay_authorization() -> None:
    """The compatibility fallback must not alter ordinary OpenAI-compatible clients."""
    apply_openai_auth_header_patch()
    token = bind_task_env_overlay(
        {"default_headers": '{"Authorization":"Basic b3ZlcmxheQ=="}'}
    )
    client = None
    try:
        model_client = oai_client_mod.OpenAIModelClient(
            ModelRequestConfig(),
            ModelClientConfig(
                api_key="ordinary-api-key",
                api_base="https://example.invalid/v1",
                client_provider="OpenAI",
                use_shared_llm_http_client=False,
            ),
        )
        client = model_client._create_async_openai_client()
        assert client._custom_headers.get("Authorization") is None
    finally:
        reset_task_env_overlay(token)
        if client is not None:
            asyncio.run(client.close())


def _effective_authorization(client: object) -> str | None:
    custom = dict(getattr(client, "_custom_headers", None) or {})
    for key, value in custom.items():
        if str(key).lower() == "authorization" and value:
            return str(value)
    auth_headers = getattr(client, "auth_headers", None)
    if isinstance(auth_headers, dict):
        for key, value in auth_headers.items():
            if str(key).lower() == "authorization" and value:
                return str(value)
    return None


def test_auth_patch_does_not_mutate_shared_client() -> None:
    """A request-scoped Authorization must not stick on the cached AsyncOpenAI client."""
    apply_openai_auth_header_patch()
    api_key = "shared-cache-api-key"
    api_base = "https://shared-cache.invalid/v1"
    polluted = oai_client_mod.OpenAIModelClient(
        ModelRequestConfig(),
        ModelClientConfig(
            api_key=api_key,
            api_base=api_base,
            client_provider="OpenAI",
            verify_ssl=False,
            custom_headers={"Authorization": "Basic c2hhcmVk"},
        ),
    )
    clean = oai_client_mod.OpenAIModelClient(
        ModelRequestConfig(),
        ModelClientConfig(
            api_key=api_key,
            api_base=api_base,
            client_provider="OpenAI",
            verify_ssl=False,
        ),
    )
    cache_key = oai_client_mod.OpenAIModelClient.connection_key(clean.model_client_config)
    try:
        first = polluted._create_async_openai_client()
        second = clean._create_async_openai_client()
        cached = oai_client_mod.OpenAIModelClient._client_cache[cache_key]
        assert _effective_authorization(first) == "Basic c2hhcmVk"
        assert _effective_authorization(second) == f"Bearer {api_key}"
        custom = dict(getattr(cached, "_custom_headers", None) or {})
        assert not any(str(key).lower() == "authorization" for key in custom)
    finally:
        cached = oai_client_mod.OpenAIModelClient._client_cache.pop(cache_key, None)
        if cached is not None:
            asyncio.run(cached.close())


def test_explicit_client_authorization_wins_over_task_overlay() -> None:
    """The existing explicit custom_headers path remains authoritative."""
    apply_openai_auth_header_patch()
    token = bind_task_env_overlay(
        {"default_headers": '{"Authorization":"Basic b3ZlcmxheQ=="}'}
    )
    client = None
    try:
        model_client = oai_client_mod.OpenAIModelClient(
            ModelRequestConfig(),
            ModelClientConfig(
                api_key="huawei-maas-session",
                api_base="https://example.invalid/v1",
                client_provider="OpenAI",
                use_shared_llm_http_client=False,
                custom_headers={"Authorization": "Basic ZXhwbGljaXQ="},
            ),
        )
        client = model_client._create_async_openai_client()
        assert client._custom_headers.get("Authorization") == "Basic ZXhwbGljaXQ="
    finally:
        reset_task_env_overlay(token)
        if client is not None:
            asyncio.run(client.close())
