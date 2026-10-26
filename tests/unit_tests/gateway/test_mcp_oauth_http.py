# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise the loopback callback through HTTP, including persisted OAuth flows."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from base64 import urlsafe_b64encode
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jiuwenswarm.common import external_providers
from gateway.channel_manager.web import mcp_oauth_http
from jiuwenswarm.server.runtime.mcp import oauth_flow, registry
from jiuwenswarm.server.runtime.mcp.credential import CredentialStore
from jiuwenswarm.server.runtime.mcp.oauth_flow import OAuthFlowManager


@pytest.fixture(autouse=True)
def capture_callback_logs(caplog, monkeypatch):
    target = mcp_oauth_http.logger
    monkeypatch.setattr(target, "handlers", [caplog.handler])
    caplog.set_level(logging.WARNING, logger=target.name)


@pytest.fixture
def oauth_callback(tmp_path, monkeypatch):
    token_requests = []
    token_response = {"access_token": "mock-access-token", "refresh_token": "mock-refresh-token"}

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={"authorization_servers": ["https://mcp.canva.cn"]})
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={
                "authorization_endpoint": "https://mcp.canva.cn/authorize",
                "token_endpoint": "https://mcp.canva.cn/token",
                "registration_endpoint": "https://mcp.canva.cn/register",
                "response_types_supported": ["code"],
                "token_endpoint_auth_methods_supported": ["none"],
            })
        if request.url.path == "/register":
            return httpx.Response(201, json={"client_id": "mock-client-id"})
        if request.url.path == "/token":
            token_requests.append(parse_qs(request.content.decode("ascii")))
            return httpx.Response(200, json=token_response)
        raise AssertionError(f"Unexpected provider request: {request.method} {request.url.path}")

    def http_client():
        return httpx.Client(transport=httpx.MockTransport(provider))

    # The Gateway is already running when the Agent creates the flow in another process.
    gateway = OAuthFlowManager(workspace_dir=tmp_path, http_client=http_client)
    agent = OAuthFlowManager(workspace_dir=tmp_path, http_client=http_client)
    started = agent.start("canva", "http://127.0.0.1:19000/")
    authorize_query = parse_qs(urlsplit(started["auth_url"]).query)
    monkeypatch.setattr(oauth_flow, "_manager", gateway)
    monkeypatch.setitem(external_providers._providers, "mcp_registry", registry)
    app = FastAPI()
    mcp_oauth_http.attach_mcp_oauth_routes(app)
    with TestClient(app) as client:
        yield client, authorize_query, token_requests, tmp_path


def test_callback_exchanges_full_code_from_persisted_state(oauth_callback):
    client, authorize_query, token_requests, workspace = oauth_callback
    code = "mock-code:mock-part:mock-final+value/="
    response = client.get("/mcp/oauth/callback", params={
        "code": code,
        "state": authorize_query["state"][0],
    })

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert len(token_requests) == 1
    form = token_requests[0]
    assert form["code"] == [code]
    assert form["client_id"] == ["mock-client-id"]
    assert form["redirect_uri"] == ["http://127.0.0.1:19000/mcp/oauth/callback"]
    verifier = form["code_verifier"][0]
    challenge = urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    assert [challenge] == authorize_query["code_challenge"]
    stored = CredentialStore(workspace_dir=workspace).get_all("canva")
    assert stored["OAUTH_ACCESS_TOKEN"] == "mock-access-token"
    assert stored["OAUTH_REFRESH_TOKEN"] == "mock-refresh-token"
    assert stored["OAUTH_CLIENT_ID"] == "mock-client-id"
    assert json.loads((workspace / "mcp" / "oauth_flows.json").read_text(encoding="utf-8"))["flows"] == {}
    for secret in (code, verifier, "mock-access-token", "mock-refresh-token"):
        assert secret not in response.text


def test_callback_rejects_unknown_state_without_exchanging_code(oauth_callback):
    client, _authorize_query, token_requests, workspace = oauth_callback
    response = client.get("/mcp/oauth/callback", params={"code": "mock-code", "state": "unknown-state"})

    assert response.status_code == 400
    assert "OAuth flow is unknown or expired" in response.text
    assert token_requests == []
    assert CredentialStore(workspace_dir=workspace).get_all("canva") == {}


def test_callback_reports_missing_code(oauth_callback):
    client, authorize_query, token_requests, _workspace = oauth_callback
    response = client.get("/mcp/oauth/callback", params={"state": authorize_query["state"][0]})

    assert response.status_code == 400
    assert "OAuth callback is missing the authorization code" in response.text
    assert token_requests == []


def test_callback_reports_and_escapes_provider_error(oauth_callback):
    client, authorize_query, token_requests, _workspace = oauth_callback
    response = client.get("/mcp/oauth/callback", params={
        "state": authorize_query["state"][0],
        "error": "access_denied<script>mock</script>",
    })

    assert response.status_code == 400
    assert "access_denied&lt;script&gt;mock&lt;/script&gt;" in response.text
    assert "<script>mock</script>" not in response.text
    assert token_requests == []


def test_callback_reports_unavailable_provider_without_exposing_details(monkeypatch, caplog):
    monkeypatch.delitem(external_providers._providers, "mcp_registry", raising=False)
    app = FastAPI()
    mcp_oauth_http.attach_mcp_oauth_routes(app)
    with TestClient(app) as client:
        response = client.get("/mcp/oauth/callback", params={
            "code": "mock-private-code", "state": "mock-private-state",
        })

    assert response.status_code == 500
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "ExternalProviderUnregistered" in caplog.text
    for secret in ("mock-private-code", "mock-private-state", "mcp_registry"):
        assert secret not in response.text


def test_callback_unexpected_failure_does_not_expose_exception_details(monkeypatch, caplog):
    def fail(query):
        raise OSError("mock-private-access-token")

    monkeypatch.setattr(registry, "complete_oauth_callback", fail)
    monkeypatch.setitem(external_providers._providers, "mcp_registry", registry)
    app = FastAPI()
    mcp_oauth_http.attach_mcp_oauth_routes(app)
    with TestClient(app) as client:
        response = client.get("/mcp/oauth/callback", params={"code": "mock-code", "state": "mock-state"})

    assert response.status_code == 500
    assert "OSError" in caplog.text
    assert "mock-private-access-token" not in response.text
    assert "mock-private-access-token" not in caplog.text


@pytest.mark.parametrize("setup", ["gateway", "front"])
def test_callback_uses_providers_registered_by_real_process_setup(tmp_path, setup):
    code = (
        "import sys\n"
        "from fastapi import FastAPI\n"
        "from fastapi.testclient import TestClient\n"
        "from jiuwenswarm.common.external_providers import registered_domains\n"
        "from gateway.channel_manager.web.mcp_oauth_http import attach_mcp_oauth_routes\n"
        "assert 'jiuwenswarm.server.runtime.mcp.oauth_flow' not in sys.modules\n"
        f"if {setup!r} == 'gateway':\n"
        "    from gateway.server_store_bridge import install\n"
        "else:\n"
        "    from jiuwenswarm.server.runtime.provider_setup import install_front as install\n"
        "install()\n"
        "assert 'mcp_registry' in registered_domains()\n"
        "app = FastAPI()\n"
        "attach_mcp_oauth_routes(app)\n"
        "with TestClient(app) as client:\n"
        "    response = client.get('/mcp/oauth/callback', params={'code':'mock-code','state':'unknown-state'})\n"
        "assert response.status_code == 400, response.text\n"
        "assert 'OAuth flow is unknown or expired' in response.text\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[3],
        env={**os.environ, "JIUWENSWARM_DATA_DIR": str(tmp_path / "data"), "PYTHONUTF8": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]
