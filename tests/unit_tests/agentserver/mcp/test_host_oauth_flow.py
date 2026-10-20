# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for host-managed OAuth (Canva) MCP flow."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.mcp.oauth_flow import OAuthFlowManager


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
