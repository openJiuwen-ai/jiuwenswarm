# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the custom model gateway catalogue plugin."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from jiuwenswarm.extensions.custom_model_catalog import extension as plugin


class _FakeResponse:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self._body = body

    def iter_bytes(self) -> Any:
        yield self._body


class _FakeStream:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def __enter__(self) -> _FakeResponse:
        return self._response

    def __exit__(self, *args: Any) -> None:
        return None


class _FakeClient:
    def __init__(self, response: _FakeResponse, **client_kwargs: Any) -> None:
        self._response = response
        self.client_kwargs = client_kwargs

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def stream(self, method: str, endpoint: str, headers: dict[str, str]) -> _FakeStream:
        del method, endpoint, headers
        return _FakeStream(self._response)


class _FakeChannel:
    def __init__(self) -> None:
        self.methods: dict[str, Any] = {}
        self.local_only: set[str] = set()
        self.available_when_disabled: dict[str, bool] = {}

    def register_method(self, method, handler, *, local_only=False, available_when_disabled=False):  # noqa: ANN001
        self.methods[method] = handler
        self.available_when_disabled[method] = available_when_disabled
        if local_only:
            self.local_only.add(method)

    async def send_response(self, ws, req_id, **payload):  # noqa: ANN001
        ws.append({"id": req_id, **payload})


def _fake_httpx(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> list[dict[str, Any]]:
    """Install a fake httpx module and return the kwargs each Client was built with."""
    client_kwargs: list[dict[str, Any]] = []

    def make_client(**kwargs: Any) -> _FakeClient:
        client_kwargs.append(kwargs)
        return _FakeClient(response, **kwargs)

    module = types.ModuleType("httpx")
    module.Client = make_client
    module.Response = lambda status, content: types.SimpleNamespace(  # noqa: ARG005
        json=lambda: __import__("json").loads(content)
    )
    monkeypatch.setitem(sys.modules, "httpx", module)
    return client_kwargs


def test_normalize_api_base_accepts_http_urls_and_strips_trailing_slashes() -> None:
    assert plugin._normalize_api_base("https://api.example/v1/") == "https://api.example/v1"
    assert plugin._normalize_api_base("  http://localhost:8080  ") == "http://localhost:8080"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        "api.example/v1",
        "ftp://api.example",
        "https://",
        "https://user:secret@api.example/v1",
        "https://user@api.example/v1",
        "https://:secret@api.example/v1",
        "https://api.example/" + "a" * 600,
    ],
)
def test_normalize_api_base_rejects_missing_or_invalid_urls(raw: Any) -> None:
    with pytest.raises(plugin._FetchFailure) as failure:
        plugin._normalize_api_base(raw)
    assert failure.value.reason in {plugin._REASON_API_BASE_REQUIRED, plugin._REASON_API_BASE_INVALID}


def test_normalize_api_base_accepts_a_url_exactly_at_the_length_limit() -> None:
    """The cap is inclusive; only a longer base is rejected."""
    at_limit = "https://api.example/" + "a" * (plugin._MAX_API_BASE_CHARS - len("https://api.example/"))
    assert len(at_limit) == plugin._MAX_API_BASE_CHARS
    assert plugin._normalize_api_base(at_limit) == at_limit


def test_candidate_endpoints_prefers_the_openai_shape_and_keeps_a_version_fallback() -> None:
    assert plugin._candidate_endpoints("https://api.example/v1", plugin._PROTOCOL_OPENAI) == [
        "https://api.example/v1/models"
    ]
    assert plugin._candidate_endpoints("https://api.example", plugin._PROTOCOL_OPENAI) == [
        "https://api.example/models",
        "https://api.example/v1/models",
    ]
    assert plugin._candidate_endpoints("https://api.example", plugin._PROTOCOL_ANTHROPIC) == [
        "https://api.example/v1/models",
        "https://api.example/models",
    ]


def test_auth_headers_use_the_protocol_credential_transport() -> None:
    assert plugin._auth_headers("sk-test", plugin._PROTOCOL_OPENAI) == {
        "Accept": "application/json",
        "Authorization": "Bearer sk-test",
    }
    assert plugin._auth_headers("sk-test", plugin._PROTOCOL_ANTHROPIC) == {
        "Accept": "application/json",
        "x-api-key": "sk-test",
        "anthropic-version": "2023-06-01",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"id": "glm-4.7"}, {"id": "glm-4.7"}, {"id": "  "}]},
        {"models": [{"name": "glm-4.7"}]},
        {"result": [{"model": "glm-4.7"}]},
        ["glm-4.7", "glm-4.7"],
    ],
)
def test_parse_model_ids_accepts_common_catalogue_shapes(payload: Any) -> None:
    assert plugin._parse_model_ids(payload) == ["glm-4.7"]


def test_parse_model_ids_rejects_unrecognized_bodies() -> None:
    assert plugin._parse_model_ids({"object": "list"}) == []
    assert plugin._parse_model_ids("glm-4.7") == []
    assert plugin._parse_model_ids(None) == []


def test_collect_models_caps_count_and_skips_oversized_ids() -> None:
    entries = [{"id": f"model-{index}"} for index in range(plugin._MAX_MODELS + 5)]
    assert len(plugin._collect_models(entries)) == plugin._MAX_MODELS
    assert plugin._collect_models([{"id": "m" * (plugin._MAX_MODEL_ID_CHARS + 1)}]) == []


def test_collect_models_keeps_an_id_exactly_at_the_length_limit() -> None:
    """The limit is inclusive: a 100-char id is still saveable by the dialogs."""
    at_limit = "m" * plugin._MAX_MODEL_ID_CHARS
    assert plugin._collect_models([{"id": at_limit}]) == [at_limit]


@pytest.mark.asyncio
async def test_fetch_custom_models_returns_remote_models(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fetch(endpoint: str, headers: dict[str, str]) -> list[str]:
        assert endpoint == "https://api.example/v1/models"
        assert headers["Authorization"] == "Bearer sk-test"
        return ["glm-4.7", "glm-4.6"]

    monkeypatch.setattr(plugin, "_fetch_sync", fake_fetch)

    result = await plugin._fetch_custom_models(
        {"api_base": "https://api.example/v1", "api_key": "sk-test", "protocol": "openai"}
    )

    assert result == {"models": ["glm-4.7", "glm-4.6"], "source": "remote"}


@pytest.mark.asyncio
async def test_fetch_custom_models_retries_only_when_the_version_segment_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_fetch(endpoint: str, headers: dict[str, str]) -> list[str]:
        del headers
        calls.append(endpoint)
        if endpoint == "https://api.example/models":
            raise plugin._FetchFailure("remote returned HTTP 404")
        return ["glm-4.7"]

    monkeypatch.setattr(plugin, "_fetch_sync", fake_fetch)

    result = await plugin._fetch_custom_models(
        {"api_base": "https://api.example", "api_key": "sk-test", "protocol": "openai"}
    )

    assert calls == ["https://api.example/models", "https://api.example/v1/models"]
    assert result == {"models": ["glm-4.7"], "source": "remote"}


@pytest.mark.asyncio
async def test_fetch_custom_models_reports_the_first_failure_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_fetch(endpoint: str, headers: dict[str, str]) -> list[str]:
        del headers
        calls.append(endpoint)
        raise plugin._FetchFailure("remote returned HTTP 401")

    monkeypatch.setattr(plugin, "_fetch_sync", fake_fetch)

    result = await plugin._fetch_custom_models(
        {"api_base": "https://api.example", "api_key": "sk-test", "protocol": "openai"}
    )

    assert calls == ["https://api.example/models"]
    assert result == {"models": [], "source": "unavailable", "reason": "remote returned HTTP 401"}


@pytest.mark.asyncio
async def test_fetch_custom_models_requires_credentials_before_calling_remote() -> None:
    with pytest.raises(plugin._FetchFailure) as failure:
        await plugin._fetch_custom_models({"api_base": "https://api.example/v1"})
    assert failure.value.reason == plugin._REASON_API_KEY_REQUIRED

    with pytest.raises(plugin._FetchFailure) as failure:
        await plugin._fetch_custom_models({"api_key": "sk-test"})
    assert failure.value.reason == plugin._REASON_API_BASE_REQUIRED

    with pytest.raises(plugin._FetchFailure) as failure:
        await plugin._fetch_custom_models({"api_base": "https://api.example/v1", "api_key": "k" * 3000})
    assert failure.value.reason == plugin._REASON_API_KEY_TOO_LONG


@pytest.mark.asyncio
async def test_fetch_custom_models_accepts_a_key_exactly_at_the_length_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, str]] = []

    def fake_fetch(endpoint: str, headers: dict[str, str]) -> list[str]:
        del endpoint
        captured.append(headers)
        return ["glm-4.7"]

    monkeypatch.setattr(plugin, "_fetch_sync", fake_fetch)

    result = await plugin._fetch_custom_models(
        {"api_base": "https://api.example/v1", "api_key": "k" * plugin._MAX_API_KEY_CHARS}
    )

    assert result == {"models": ["glm-4.7"], "source": "remote"}
    assert captured[0]["Authorization"] == "Bearer " + "k" * plugin._MAX_API_KEY_CHARS


@pytest.mark.asyncio
async def test_fetch_custom_models_treats_a_missing_protocol_as_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """``None``/absent protocol must fall back, not be stringified into a truthy value."""
    captured: list[dict[str, str]] = []

    def fake_fetch(endpoint: str, headers: dict[str, str]) -> list[str]:
        del endpoint
        captured.append(headers)
        return ["glm-4.7"]

    monkeypatch.setattr(plugin, "_fetch_sync", fake_fetch)

    await plugin._fetch_custom_models({"api_base": "https://api.example/v1", "api_key": "sk-test", "protocol": None})

    assert captured[0]["Authorization"] == "Bearer sk-test"
    assert "x-api-key" not in captured[0]


@pytest.mark.asyncio
async def test_fetch_custom_models_keeps_the_anthropic_protocol_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request protocol must survive the normalization step, not collapse to empty."""
    captured: list[dict[str, str]] = []

    def fake_fetch(endpoint: str, headers: dict[str, str]) -> list[str]:
        del endpoint
        captured.append(headers)
        return ["claude-sonnet-5"]

    monkeypatch.setattr(plugin, "_fetch_sync", fake_fetch)

    result = await plugin._fetch_custom_models(
        {"api_base": "https://api.example", "api_key": "sk-test", "protocol": "Anthropic"}
    )

    assert result == {"models": ["claude-sonnet-5"], "source": "remote"}
    assert captured[0]["x-api-key"] == "sk-test"
    assert "Authorization" not in captured[0]


@pytest.mark.asyncio
async def test_fetch_custom_models_defaults_unknown_protocols_to_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, str]] = []

    def fake_fetch(endpoint: str, headers: dict[str, str]) -> list[str]:
        del endpoint
        captured.append(headers)
        return ["glm-4.7"]

    monkeypatch.setattr(plugin, "_fetch_sync", fake_fetch)

    await plugin._fetch_custom_models(
        {"api_base": "https://api.example/v1", "api_key": "sk-test", "protocol": "vendor-specific"}
    )

    assert captured[0]["Authorization"] == "Bearer sk-test"
    assert "x-api-key" not in captured[0]


def test_fetch_sync_rejects_http_errors_without_leaking_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_httpx(monkeypatch, _FakeResponse(401, b'{"error": "invalid key"}'))

    with pytest.raises(plugin._FetchFailure) as failure:
        plugin._fetch_sync("https://api.example/v1/models", {"Authorization": "Bearer sk-test"})

    assert failure.value.reason == "remote returned HTTP 401"
    assert "sk-test" not in failure.value.reason


def test_fetch_sync_logs_only_the_exception_type(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    class _ExplodingClient:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        def __enter__(self) -> "_ExplodingClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def stream(self, method: str, endpoint: str, headers: dict[str, str]) -> Any:
            del method, endpoint, headers
            raise RuntimeError("failed at https://api.example/v1/models with Bearer sk-test")

    module = types.ModuleType("httpx")
    module.Client = _ExplodingClient
    monkeypatch.setitem(sys.modules, "httpx", module)
    caplog.set_level("DEBUG", logger=plugin.__name__)

    with pytest.raises(plugin._FetchFailure) as failure:
        plugin._fetch_sync("https://api.example/v1/models", {"Authorization": "Bearer sk-test"})

    assert failure.value.reason == plugin._REASON_NETWORK
    messages = [record.getMessage() for record in caplog.records]
    assert messages
    assert all("api.example" not in message and "sk-test" not in message for message in messages)
    assert "RuntimeError" in messages[0]


def test_fetch_sync_rejects_non_json_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_httpx(monkeypatch, _FakeResponse(200, b"<html>gateway error</html>"))

    with pytest.raises(plugin._FetchFailure) as failure:
        plugin._fetch_sync("https://api.example/v1/models", {})

    assert failure.value.reason == plugin._REASON_NON_JSON


def test_fetch_sync_caps_the_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_httpx(monkeypatch, _FakeResponse(200, b"x" * (plugin._MAX_RESPONSE_BYTES + 1)))

    with pytest.raises(plugin._FetchFailure) as failure:
        plugin._fetch_sync("https://api.example/v1/models", {})

    assert failure.value.reason == plugin._REASON_BODY_TOO_LARGE


def test_fetch_sync_accepts_a_body_exactly_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b'{"data": [{"id": "glm-4.7"}]}'
    padding = b" " * (plugin._MAX_RESPONSE_BYTES - len(body))
    _fake_httpx(monkeypatch, _FakeResponse(200, body + padding))

    assert plugin._fetch_sync("https://api.example/v1/models", {}) == ["glm-4.7"]


def test_fetch_sync_disables_redirects_and_applies_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user-supplied endpoint must not be able to bounce the request elsewhere."""
    client_kwargs = _fake_httpx(monkeypatch, _FakeResponse(200, b'{"data": [{"id": "glm-4.7"}]}'))

    plugin._fetch_sync("https://api.example/v1/models", {})

    assert client_kwargs == [{"timeout": plugin._TIMEOUT_SECONDS, "follow_redirects": False}]


def test_fetch_sync_rejects_an_empty_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_httpx(monkeypatch, _FakeResponse(200, b'{"data": []}'))

    with pytest.raises(plugin._FetchFailure) as failure:
        plugin._fetch_sync("https://api.example/v1/models", {})

    assert failure.value.reason == plugin._REASON_NETWORK


def test_fetch_sync_returns_model_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_httpx(monkeypatch, _FakeResponse(200, b'{"data": [{"id": "glm-4.7"}]}'))

    assert plugin._fetch_sync("https://api.example/v1/models", {}) == ["glm-4.7"]


@pytest.mark.asyncio
async def test_plugin_registers_a_local_only_method_that_returns_the_reason_payload() -> None:
    channel = _FakeChannel()
    plugin.CustomModelCatalogPlugin().bind_web_channel(channel, object())

    assert channel.local_only == {plugin.FETCH_METHOD}
    # Business runtime RPC: never answer while the plugin is disabled.
    assert channel.available_when_disabled[plugin.FETCH_METHOD] is False

    responses: list[dict[str, Any]] = []
    await channel.methods[plugin.FETCH_METHOD](responses, "req-1", {"api_base": "https://api.example/v1"}, "s")

    assert responses[0]["ok"] is True
    assert responses[0]["payload"] == {
        "models": [],
        "source": "unavailable",
        "reason": plugin._REASON_API_KEY_REQUIRED,
    }


@pytest.mark.asyncio
async def test_plugin_lifecycle_hooks_are_no_ops() -> None:
    instance = plugin.CustomModelCatalogPlugin()

    await instance.initialize({"ignored": True})
    await instance.shutdown()


@pytest.mark.asyncio
async def test_plugin_returns_the_fetched_payload_for_a_successful_call(monkeypatch: pytest.MonkeyPatch) -> None:
    async def succeed(params: dict[str, Any]) -> dict[str, Any]:
        del params
        return {"models": ["glm-4.7"], "source": "remote"}

    monkeypatch.setattr(plugin, "_fetch_custom_models", succeed)
    channel = _FakeChannel()
    plugin.CustomModelCatalogPlugin().bind_web_channel(channel, object())

    responses: list[dict[str, Any]] = []
    await channel.methods[plugin.FETCH_METHOD](responses, "req-1", {"api_base": "https://api.example/v1"}, "s")

    assert responses[0] == {"id": "req-1", "ok": True, "payload": {"models": ["glm-4.7"], "source": "remote"}}


@pytest.mark.asyncio
async def test_plugin_converts_unexpected_errors_into_the_network_reason(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def explode(params: dict[str, Any]) -> dict[str, Any]:
        del params
        raise RuntimeError("failed at https://api.example/v1/models with Bearer sk-test")

    monkeypatch.setattr(plugin, "_fetch_custom_models", explode)
    caplog.set_level("WARNING", logger=plugin.__name__)
    channel = _FakeChannel()
    plugin.CustomModelCatalogPlugin().bind_web_channel(channel, object())

    responses: list[dict[str, Any]] = []
    await channel.methods[plugin.FETCH_METHOD](responses, "req-1", {"api_base": "https://api.example/v1"}, "s")

    assert responses[0]["ok"] is True
    assert responses[0]["payload"] == {
        "models": [],
        "source": "unavailable",
        "reason": plugin._REASON_NETWORK,
    }
    messages = [record.getMessage() for record in caplog.records]
    assert messages
    assert all("api.example" not in message and "sk-test" not in message for message in messages)
    assert "RuntimeError" in messages[0]


@pytest.mark.asyncio
async def test_plugin_rejects_non_object_params() -> None:
    channel = _FakeChannel()
    plugin.CustomModelCatalogPlugin().bind_web_channel(channel, object())

    responses: list[dict[str, Any]] = []
    await channel.methods[plugin.FETCH_METHOD](responses, "req-1", ["api_base"], "s")

    assert responses[0]["ok"] is False
    assert responses[0]["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_register_extensions_registers_the_plugin() -> None:
    registry = types.SimpleNamespace(registered=[])

    def register_application_plugin(extension: Any) -> None:
        registry.registered.append(extension)

    registry.register_application_plugin = register_application_plugin  # type: ignore[attr-defined]
    registered = await plugin.register_extensions(registry)

    assert registered == registry.registered
    assert registered[0].plugin_id == "custom-model-catalog"
    assert registered[0].is_enabled() is True
