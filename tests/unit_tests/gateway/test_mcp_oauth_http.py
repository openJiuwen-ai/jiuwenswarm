# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise the loopback callback through HTTP, including persisted OAuth flows."""

from __future__ import annotations

import hashlib
import json
from base64 import urlsafe_b64encode
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jiuwenswarm.gateway.channel_manager.web import mcp_oauth_http
from jiuwenswarm.server.runtime.mcp.credential import CredentialStore
from jiuwenswarm.server.runtime.mcp.oauth_flow import OAuthFlowManager


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
    monkeypatch.setattr(mcp_oauth_http, "complete_oauth_flow", gateway.complete)
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
