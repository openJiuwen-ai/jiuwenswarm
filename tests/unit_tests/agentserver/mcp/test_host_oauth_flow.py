# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for host-managed OAuth (Canva) MCP flow."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest

from jiuwenswarm.server.runtime.mcp.oauth_flow import (
    _FLOW_TTL_S,
    McpOAuthError,
    OAuthFlowManager,
    _parse_callback_url,
)


@pytest.fixture(autouse=True)
def capture_oauth_logs(caplog, monkeypatch):
    target = logging.getLogger("jiuwenswarm.server.runtime.mcp.oauth_flow")
    monkeypatch.setattr(target, "handlers", [caplog.handler])
    caplog.set_level(logging.DEBUG, logger=target.name)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, calls: list[tuple[str, str, dict]], routes: dict[str, _FakeResponse]):
        self._calls = calls
        self._routes = routes

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url: str):
        self._calls.append(("GET", url, {}))
        return self._routes[url]

    def post(self, url: str, json=None, data=None):
        self._calls.append(("POST", url, {"json": json, "data": data}))
        return self._routes[url]


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch):
    from jiuwenswarm.server.runtime.mcp import credential
    from jiuwenswarm.server.runtime.mcp import registry
    from jiuwenswarm.server.runtime.mcp import oauth_flow, skill_installer, state_store
    monkeypatch.setattr(registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(credential, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(oauth_flow, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(state_store, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(skill_installer, "get_workspace_dir", lambda: tmp_path)
    return tmp_path


def test_start_and_complete_canva_flow(workspace: Path):
    calls: list[tuple[str, str, dict]] = []
    routes = {
        "https://mcp.canva.cn/.well-known/oauth-protected-resource": _FakeResponse(200, {
            "authorization_servers": ["https://mcp.canva.cn"],
        }),
        "https://mcp.canva.cn/.well-known/oauth-authorization-server": _FakeResponse(200, {
            "authorization_endpoint": "https://mcp.canva.cn/authorize",
            "token_endpoint": "https://mcp.canva.cn/token",
            "registration_endpoint": "https://mcp.canva.cn/register",
            "response_types_supported": ["code"],
            "token_endpoint_auth_methods_supported": ["none"],
        }),
        "https://mcp.canva.cn/register": _FakeResponse(201, {"client_id": "client-1"}),
        "https://mcp.canva.cn/token": _FakeResponse(200, {
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "profile:read design:content:write",
        }),
    }
    manager = OAuthFlowManager(workspace_dir=workspace, http_client=lambda: _FakeClient(calls, routes))

    started = manager.start("canva", "http://127.0.0.1:19000/")
    assert started["auth_required"] is True
    assert started["flow"]
    assert started["auth_url"].startswith("https://mcp.canva.cn/authorize?")
    assert "code_challenge_method=S256" in started["auth_url"]
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A19000%2Fmcp%2Foauth%2Fcallback" in started["auth_url"]


def test_canva_redirect_uri_is_pinned_to_loopback(workspace: Path):
    manager = OAuthFlowManager(workspace_dir=workspace, http_client=lambda: _FakeClient([], {
        "https://mcp.canva.cn/.well-known/oauth-protected-resource": _FakeResponse(200, {
            "authorization_servers": ["https://mcp.canva.cn"],
        }),
        "https://mcp.canva.cn/.well-known/oauth-authorization-server": _FakeResponse(200, {
            "authorization_endpoint": "https://mcp.canva.cn/authorize",
            "token_endpoint": "https://mcp.canva.cn/token",
            "registration_endpoint": "https://mcp.canva.cn/register",
            "response_types_supported": ["code"],
            "token_endpoint_auth_methods_supported": ["none"],
        }),
        "https://mcp.canva.cn/register": _FakeResponse(201, {"client_id": "client-1"}),
        "https://mcp.canva.cn/token": _FakeResponse(200, {"access_token": "access-1"}),
    }))

    started = manager.start("canva", "http://127.0.0.1:12345/")
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A19000%2Fmcp%2Foauth%2Fcallback" in started["auth_url"]


def test_oauth_flow_survives_manager_restart(workspace: Path):
    calls: list[tuple[str, str, dict]] = []
    routes = {
        "https://mcp.canva.cn/.well-known/oauth-protected-resource": _FakeResponse(200, {
            "authorization_servers": ["https://mcp.canva.cn"],
        }),
        "https://mcp.canva.cn/.well-known/oauth-authorization-server": _FakeResponse(200, {
            "authorization_endpoint": "https://mcp.canva.cn/authorize",
            "token_endpoint": "https://mcp.canva.cn/token",
            "registration_endpoint": "https://mcp.canva.cn/register",
            "response_types_supported": ["code"],
            "token_endpoint_auth_methods_supported": ["none"],
        }),
        "https://mcp.canva.cn/register": _FakeResponse(201, {"client_id": "client-1"}),
        "https://mcp.canva.cn/token": _FakeResponse(200, {
            "access_token": "access-1",
            "refresh_token": "refresh-1",
        }),
    }
    manager = OAuthFlowManager(workspace_dir=workspace, http_client=lambda: _FakeClient(calls, routes))
    started = manager.start("canva", "http://127.0.0.1:19000/")
    flow_id = started["flow"]

    restarted = OAuthFlowManager(workspace_dir=workspace, http_client=lambda: _FakeClient(calls, routes))
    assert restarted.complete({
        "flow": flow_id,
        "state": started["auth_url"].split("state=")[1].split("&")[0],
        "code": "code-1",
    }) is True


def test_oauth_flow_can_be_found_by_state_when_flow_id_missing(workspace: Path):
    calls: list[tuple[str, str, dict]] = []
    routes = {
        "https://mcp.canva.cn/.well-known/oauth-protected-resource": _FakeResponse(200, {
            "authorization_servers": ["https://mcp.canva.cn"],
        }),
        "https://mcp.canva.cn/.well-known/oauth-authorization-server": _FakeResponse(200, {
            "authorization_endpoint": "https://mcp.canva.cn/authorize",
            "token_endpoint": "https://mcp.canva.cn/token",
            "registration_endpoint": "https://mcp.canva.cn/register",
            "response_types_supported": ["code"],
            "token_endpoint_auth_methods_supported": ["none"],
        }),
        "https://mcp.canva.cn/register": _FakeResponse(201, {"client_id": "client-1"}),
        "https://mcp.canva.cn/token": _FakeResponse(200, {
            "access_token": "access-1",
            "refresh_token": "refresh-1",
        }),
    }
    manager = OAuthFlowManager(workspace_dir=workspace, http_client=lambda: _FakeClient(calls, routes))
    started = manager.start("canva", "http://127.0.0.1:19000/")
    state = started["auth_url"].split("state=")[1].split("&")[0]

    restarted = OAuthFlowManager(workspace_dir=workspace, http_client=lambda: _FakeClient(calls, routes))
    assert restarted.complete({"state": state, "code": "code-1"}) is True


def test_expired_persisted_flow_cannot_complete_by_state(workspace: Path):
    calls: list[tuple[str, str, dict]] = []
    routes = {
        "https://mcp.canva.cn/.well-known/oauth-protected-resource": _FakeResponse(200, {
            "authorization_servers": ["https://mcp.canva.cn"],
        }),
        "https://mcp.canva.cn/.well-known/oauth-authorization-server": _FakeResponse(200, {
            "authorization_endpoint": "https://mcp.canva.cn/authorize",
            "token_endpoint": "https://mcp.canva.cn/token",
            "registration_endpoint": "https://mcp.canva.cn/register",
            "response_types_supported": ["code"],
            "token_endpoint_auth_methods_supported": ["none"],
        }),
        "https://mcp.canva.cn/register": _FakeResponse(201, {"client_id": "client-1"}),
        "https://mcp.canva.cn/token": _FakeResponse(200, {"access_token": "access-1"}),
    }
    now = [0.0]
    manager = OAuthFlowManager(
        workspace_dir=workspace,
        http_client=lambda: _FakeClient(calls, routes),
        time_fn=lambda: now[0],
    )
    started = manager.start("canva", "http://127.0.0.1:19000/")
    state = started["auth_url"].split("state=")[1].split("&")[0]

    now[0] = _FLOW_TTL_S + 1
    restarted = OAuthFlowManager(
        workspace_dir=workspace,
        http_client=lambda: _FakeClient(calls, routes),
        time_fn=lambda: now[0],
    )
    with pytest.raises(McpOAuthError, match="OAuth flow is unknown or expired"):
        restarted.complete({"state": state, "code": "code-1"})
    assert not [call for call in calls if call[0] == "POST" and call[1] == "https://mcp.canva.cn/token"]


def test_provider_error_message_names_the_connector(workspace: Path):
    routes = {
        "https://mcp.canva.cn/.well-known/oauth-protected-resource": _FakeResponse(200, {
            "authorization_servers": ["https://mcp.canva.cn"],
        }),
        "https://mcp.canva.cn/.well-known/oauth-authorization-server": _FakeResponse(200, {
            "authorization_endpoint": "https://mcp.canva.cn/authorize",
            "token_endpoint": "https://mcp.canva.cn/token",
            "registration_endpoint": "https://mcp.canva.cn/register",
            "response_types_supported": ["code"],
            "token_endpoint_auth_methods_supported": ["none"],
        }),
        "https://mcp.canva.cn/register": _FakeResponse(201, {"client_id": "client-1"}),
        "https://mcp.canva.cn/token": _FakeResponse(200, {"access_token": "access-1"}),
    }
    manager = OAuthFlowManager(workspace_dir=workspace, http_client=lambda: _FakeClient([], routes))
    started = manager.start("canva", "http://127.0.0.1:19000/")
    state = started["auth_url"].split("state=")[1].split("&")[0]

    with pytest.raises(McpOAuthError, match=r"MCP 'canva' authorization failed: access_denied"):
        manager.complete({"state": state, "error": "access_denied"})


def test_complete_host_oauth_auth_writes_connecting_record(workspace: Path):
    from jiuwenswarm.server.runtime.mcp.registry import complete_host_oauth_auth
    from jiuwenswarm.server.runtime.mcp import state_store
    from tests.unit_tests.agentserver.mcp.manifest_helpers import write_manifest

    package = workspace / "mcp" / "mcp_hub" / "canva"
    package.mkdir(parents=True)
    (package / "mcp.json").write_text(
        '{"mcpServers":{"canva":{"url":"https://mcp.canva.cn/mcp"}}}',
        encoding="utf-8",
    )
    write_manifest(package, "remote-mcp", credentials_type="oauth")

    item = complete_host_oauth_auth("canva")

    rec = state_store.get_mcp_record("canva")
    assert rec is not None
    assert rec["state"] == "connecting"
    assert rec["integration_type"] == "remote-mcp"
    assert item["transport"] == "streamable-http"
    assert item["url"] == "https://mcp.canva.cn/mcp"


@pytest.mark.parametrize("callback_url,expected", [
    ("http://127.0.0.1:19000", ("127.0.0.1", 19000)),
    ("http://127.0.0.1:12345/", ("127.0.0.1", 12345)),
])
def test_callback_url_accepts_only_loopback_origins(callback_url, expected):
    assert _parse_callback_url(callback_url) == expected


@pytest.mark.parametrize("callback_url", [
    "https://127.0.0.1:19000",
    "http://example.test:19000",
    "http://localhost:19000",
    "http://127.0.0.1",
    "http://127.0.0.1:0",
    "http://127.0.0.1:19000/mcp/oauth/callback",
    "http://127.0.0.1:19000?code=mock-private-code",
    "http://127.0.0.1:19000#mock-private-code",
    "http://127.0.0.1:invalid",
    "http://127.0.0.1:65536",
    "http://[invalid:19000",
])
def test_callback_url_rejects_invalid_origins_without_echoing_input(callback_url):
    with pytest.raises(McpOAuthError, match="OAuth callback must be a loopback base URL") as error:
        _parse_callback_url(callback_url)
    assert callback_url not in str(error.value)


@pytest.mark.parametrize("contents", [b'{"mock-private-state":', b'\xffmock-private-verifier'])
def test_unreadable_flow_json_is_diagnosed_without_exposing_contents(tmp_path, caplog, contents):
    path = tmp_path / "mcp/oauth_flows.json"
    path.parent.mkdir()
    path.write_bytes(contents)

    with caplog.at_level(logging.WARNING):
        manager = OAuthFlowManager(workspace_dir=tmp_path)

    assert manager._flows == {}
    assert "Cannot restore persisted flows" in caplog.text
    assert "mock-private" not in caplog.text
    assert path.read_bytes() == contents


def test_flow_read_error_is_diagnosed_without_logging_exception_details(tmp_path, monkeypatch, caplog):
    def fail_read(*args, **kwargs):
        raise PermissionError("mock-private-path-and-token")

    monkeypatch.setattr(Path, "read_text", fail_read)
    with caplog.at_level(logging.WARNING):
        manager = OAuthFlowManager(workspace_dir=tmp_path)

    assert manager._flows == {}
    assert "Cannot restore persisted flows (PermissionError)" in caplog.text
    assert "mock-private" not in caplog.text


@pytest.mark.parametrize("contents", ["[]", '{"flows": []}'])
def test_invalid_flow_document_is_diagnosed(tmp_path, caplog, contents):
    path = tmp_path / "mcp/oauth_flows.json"
    path.parent.mkdir()
    path.write_text(contents, encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        manager = OAuthFlowManager(workspace_dir=tmp_path)

    assert manager._flows == {}
    assert "Persisted flows have an invalid structure" in caplog.text


@pytest.mark.parametrize("invalid_field,value", [
    ("name", None),
    ("provider", 12),
    ("created_at", "mock-private-verifier"),
])
def test_invalid_flow_record_does_not_hide_valid_flows(tmp_path, caplog, invalid_field, value):
    valid = {
        "name": "canva",
        "provider": {"mcp_url": "https://mcp.canva.cn/mcp"},
        "metadata": {"token_endpoint": "https://mcp.canva.cn/token"},
        "client_id": "mock-client",
        "redirect_uri": "http://127.0.0.1:19000/mcp/oauth/callback",
        "state": "mock-private-state",
        "code_verifier": "mock-private-verifier",
        "created_at": 100,
    }
    invalid = {**valid, invalid_field: value}
    if invalid_field == "name":
        invalid.pop("name")
    path = tmp_path / "mcp/oauth_flows.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"flows": {
        "mock-private-invalid-flow-id": invalid,
        "mock-private-invalid-shape": "mock-private-payload",
        "valid": valid,
    }}), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        manager = OAuthFlowManager(workspace_dir=tmp_path, time_fn=lambda: 100)

    assert list(manager._flows) == ["valid"]
    assert manager._flows["valid"].state == valid["state"]
    assert "Skipping an invalid persisted flow" in caplog.text
    assert "Skipping a persisted flow with an invalid structure" in caplog.text
    assert "mock-private" not in caplog.text


def test_missing_flow_file_is_an_expected_first_start(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        manager = OAuthFlowManager(workspace_dir=tmp_path)

    assert manager._flows == {}
    assert "No persisted flows to restore" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_provider_lookup_has_no_instance_dependency():
    assert OAuthFlowManager._provider("canva")["mcp_url"] == "https://mcp.canva.cn/mcp"
    with pytest.raises(McpOAuthError, match="does not support host OAuth"):
        OAuthFlowManager._provider("unknown-provider")


def test_registry_callback_preserves_query_and_shared_error_identity(monkeypatch):
    from jiuwenswarm.common.mcp_oauth import McpOAuthError as SharedOAuthError
    from jiuwenswarm.server.runtime.mcp import oauth_flow, registry

    query = {"code": "mock-code:part+value/=", "state": "mock-state"}
    manager = Mock()
    manager.complete.return_value = True
    monkeypatch.setattr(oauth_flow, "_manager", manager)

    assert McpOAuthError is SharedOAuthError
    assert registry.complete_oauth_callback(query) is True
    manager.complete.assert_called_once_with(query)

    manager.complete.side_effect = SharedOAuthError("OAuth flow is unknown or expired")
    with pytest.raises(SharedOAuthError, match="unknown or expired"):
        registry.complete_oauth_callback(query)
