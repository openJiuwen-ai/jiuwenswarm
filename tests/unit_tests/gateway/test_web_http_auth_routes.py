# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import base64
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jiuwenswarm.common.auth import account_kit
from jiuwenswarm.common.auth import service as service_mod
from jiuwenswarm.common.auth import session_store as store_mod
from jiuwenswarm.common.auth.account_kit import AccountKitFlow, OAuthConfig
from jiuwenswarm.common.auth.service import AuthService
from jiuwenswarm.gateway.channel_manager.web import web_http_auth

ID_TOKEN_MARKER = "IDTOKEN-LEAK"
REFRESH_TOKEN = "REFRESH-LEAK"


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _id_token(openid: str = "openid-1") -> str:
    payload = {"openid": openid, "sub": openid, "exp": int(time.time()) + 3600, "display_name": "138******00"}
    return f"{_b64({'alg': 'RS256', 'kid': 'k'})}.{_b64(payload)}.{ID_TOKEN_MARKER}"


class FakeTokenEndpoint:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200
        self.payload: dict = {"access_token": "AT", "id_token": _id_token(), "refresh_token": REFRESH_TOKEN}

    def __call__(self, method, url, headers=None, data=None, **kwargs):
        self.requests.append({"url": url, "headers": headers, "data": dict(data or {})})
        return SimpleNamespace(status_code=self.status, json=lambda: self.payload)


@pytest.fixture
def token_endpoint(monkeypatch) -> FakeTokenEndpoint:
    fake = FakeTokenEndpoint()
    monkeypatch.setattr(account_kit, "requests_request", fake)
    return fake


def _remote(monkeypatch, *, effective=True, apig_base="https://apig.example.com",
            whitelist=None, blocklist=None, login=None, **apig_paths):
    from jiuwenswarm.common.auth import remote_config

    remote_config.set_config_for_test(remote_config.parse_config({
        "is_effective": effective,
        "huaweiaccount_login": login or {},
        "gateway": {"base_url": apig_base, **apig_paths},
        "models": {"whitelist": whitelist or [], "blocklist": blocklist or []},
    }))
    monkeypatch.setattr(remote_config, "config_url", lambda: "https://config.example.com")


EXCHANGE_URL = "https://auth.example.com/account-kit/token"


def _make_client(tmp_path, monkeypatch, exchange_url: str = EXCHANGE_URL) -> TestClient:
    monkeypatch.setattr(store_mod, "auth_dir", lambda: tmp_path)
    _remote(monkeypatch, effective=True, apig_base="")
    flow = AccountKitFlow(
        OAuthConfig(
            client_id="118944053",
            redirect_uri="http://localhost:19000/api/v1/auth/callback",
            scope="openid profile",
            exchange_url=exchange_url,
        )
    )
    service = AuthService(flow=flow, store=store_mod.AuthSessionStore())
    monkeypatch.setattr(service_mod, "_service", service)
    monkeypatch.setattr(web_http_auth, "get_auth_service", lambda: service)

    app = FastAPI()
    web_http_auth.register_auth_routes(app)
    return TestClient(app, headers={web_http_auth.AUTH_REQUEST_HEADER: "1"})


@pytest.fixture
def client(tmp_path, monkeypatch, token_endpoint) -> TestClient:
    return _make_client(tmp_path, monkeypatch)


def _authorize(client) -> dict:
    response = client.post("/api/v1/auth/authorize")
    assert response.status_code == 200, response.text
    return response.json()


def _callback(client, state: str, **params):
    return client.get("/api/v1/auth/callback", params={"state": state, **params})


def _claim(client, started: dict, claim_token: str | None = None):
    return client.post(
        "/api/v1/auth/claim",
        json={"state": started["state"], "claimToken": claim_token or started["claimToken"]},
    )


def test_authorize_builds_account_kit_url(client):
    started = _authorize(client)
    url = urlparse(started["authorizeUrl"])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == account_kit.AUTHORIZE_URL
    assert query["client_id"] == "118944053"
    assert query["redirect_uri"] == "http://localhost:19000/api/v1/auth/callback"
    assert query["response_type"] == "code"
    assert query["state"] == started["state"]
    assert query["code_challenge_method"] == "S256"
    assert query["access_type"] == "offline", "要 refresh_token 需要它"
    assert "openid" in query["scope"].split(), "没有 openid 就没有 id_token"
    assert started["claimToken"] not in started["authorizeUrl"]


def test_authorize_fails_fast_without_exchange_service(tmp_path, monkeypatch, token_endpoint):
    response = _make_client(tmp_path, monkeypatch, exchange_url="").post("/api/v1/auth/authorize")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "exchange_not_configured"


def test_routes_are_404_when_login_disabled(client, monkeypatch):
    _remote(monkeypatch, effective=False, apig_base="")
    assert client.post("/api/v1/auth/authorize").status_code == 404
    assert client.post("/api/v1/auth/claim", json={"state": "s", "claimToken": "t"}).status_code == 404
    assert _callback(client, "s", code="c").status_code == 404


def test_full_login_flow(client, token_endpoint):
    started = _authorize(client)

    assert _claim(client, started).status_code == 202

    landing = _callback(client, started["state"], code="auth-code-1")
    assert landing.status_code == 200
    assert "text/html" in landing.headers["content-type"]
    assert "jiuwenswarm_auth=" in landing.headers.get("set-cookie", "")
    assert landing.headers["cache-control"] == "no-store", "URL 里带着授权码"
    assert web_http_auth.AUTH_CALLBACK_MESSAGE in landing.text, "要通知应用标签页"
    assert web_http_auth.AUTH_CALLBACK_CHANNEL in landing.text
    assert "opener" not in landing.text, "不经 window.opener 通知：应用打开授权页时已切断 opener"
    assert "Secure" not in landing.headers["set-cookie"], "本机 http 访问时设了 Secure 浏览器就不存 cookie"

    (sent,) = token_endpoint.requests
    assert sent["url"] == EXCHANGE_URL
    form = sent["data"]
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "auth-code-1"
    assert form["redirect_uri"] == "http://localhost:19000/api/v1/auth/callback"
    assert form["client_id"] == "118944053"
    assert "client_secret" not in form, "客户端里没有 secret，由 ECS 补"
    challenge = parse_qs(urlparse(started["authorizeUrl"]).query)["code_challenge"][0]
    digest = hashlib.sha256(form["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge

    claimed = _claim(client, started)
    assert claimed.status_code == 200
    body = claimed.json()
    assert body["islogin"] is True and body["userId"] == "openid-1"
    session_id = claimed.headers["X-Auth-Session"]
    assert f"jiuwenswarm_auth={session_id}" in claimed.headers.get("set-cookie", "")

    assert _claim(client, started).status_code == 400

    status = client.get("/api/v1/auth/status", headers={"X-Auth-Session": session_id}).json()
    assert status["islogin"] is True
    assert status["userName"] == "138******00"

    for text in (landing.text, claimed.text, json.dumps(status)):
        assert ID_TOKEN_MARKER not in text and REFRESH_TOKEN not in text


def test_status_without_session_is_logged_out(client):
    started = _authorize(client)
    _callback(client, started["state"], code="c")
    fresh_client = TestClient(client.app)  # 没有 cookie jar 里那份
    assert fresh_client.get("/api/v1/auth/status").json()["islogin"] is False


def test_wrong_claim_token_is_rejected_without_burning_the_login(client):
    started = _authorize(client)
    _callback(client, started["state"], code="c")

    bad = _claim(client, started, claim_token="guessed")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "oauth_state_invalid"
    assert _claim(client, started).status_code == 200


def test_user_denies_authorization(client, token_endpoint):
    started = _authorize(client)
    landing = _callback(client, started["state"], error="access_denied")
    assert landing.status_code == 400
    assert token_endpoint.requests == []

    claimed = _claim(client, started)
    assert claimed.status_code == 400
    assert claimed.json()["error"]["code"] == "oauth_access_denied"


def test_huawei_error_description_reaches_the_claimer(client):
    started = _authorize(client)
    landing = _callback(client, started["state"], error="1201", error_description="some reason")
    assert landing.status_code == 400
    assert "some reason" in landing.text
    message = _claim(client, started).json()["error"]["message"]
    assert "1201" in message and "some reason" in message


def test_token_exchange_failure_reaches_the_claimer(client, token_endpoint):
    token_endpoint.status = 400
    token_endpoint.payload = {"error": "invalid_grant", "sub_error": 20156}
    started = _authorize(client)
    assert _callback(client, started["state"], code="c").status_code == 400
    claimed = _claim(client, started)
    assert claimed.status_code == 400
    assert claimed.json()["error"]["code"] == "oauth_token_exchange_failed"


def test_unknown_state_gets_a_neutral_page_and_no_exchange(client, token_endpoint):
    landing = _callback(client, "not-a-real-state", code="c")
    assert landing.status_code == 400
    assert "登录链接已失效" in landing.text
    assert token_endpoint.requests == []


def test_replayed_callback_does_not_exchange_twice(client, token_endpoint):
    started = _authorize(client)
    assert _callback(client, started["state"], code="c").status_code == 200
    assert _callback(client, started["state"], code="c").status_code == 400
    assert len(token_endpoint.requests) == 1
    assert _claim(client, started).status_code == 200, "第一次的结果必须还在"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/v1/auth/authorize", None),
        ("post", "/api/v1/auth/claim", {"state": "s", "claimToken": "t"}),
        ("post", "/api/v1/auth/logout", None),
    ],
)
def test_state_changing_routes_require_the_auth_header(client, method, path, body):
    response = getattr(client, method)(path, json=body, headers={web_http_auth.AUTH_REQUEST_HEADER: ""})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "auth_header_required"


def test_session_cookie_is_secure_over_https(client):
    started = _authorize(client)
    _callback(client, started["state"], code="c")
    claimed = client.post(
        "https://testserver/api/v1/auth/claim",
        json={"state": started["state"], "claimToken": started["claimToken"]},
    )
    assert claimed.status_code == 200
    assert "Secure" in claimed.headers["set-cookie"]


def test_landing_page_escapes_error_text(client):
    started = _authorize(client)
    landing = _callback(client, started["state"], error="<img src=x onerror=alert(1)>")
    assert "<img src=x" not in landing.text
    assert "&lt;img src=x" in landing.text


def test_logout_clears_session_and_cookie(client):
    started = _authorize(client)
    _callback(client, started["state"], code="c")
    session_id = _claim(client, started).headers["X-Auth-Session"]

    response = client.post("/api/v1/auth/logout", headers={"X-Auth-Session": session_id})
    assert response.json()["islogin"] is False
    assert 'jiuwenswarm_auth=""' in response.headers.get("set-cookie", "")
    status = client.get("/api/v1/auth/status", headers={"X-Auth-Session": session_id}).json()
    assert status["islogin"] is False


def test_logout_revokes_the_credential_held_by_agent_server(client, monkeypatch):
    from jiuwenswarm.gateway.channel_manager.web import web_http_auth

    revoked: list[str] = []

    async def _revoke(session_id):
        revoked.append(session_id)

    monkeypatch.setattr(web_http_auth, "_revoke_agent_server_credential", _revoke)
    started = _authorize(client)
    _callback(client, started["state"], code="c")
    claimed = _claim(client, started)
    session_id = claimed.headers["X-Auth-Session"]

    client.post("/api/v1/auth/logout", headers={"X-Auth-Session": session_id})
    client.post("/api/v1/auth/logout")  # 没带会话：什么都不撤销
    assert revoked == [claimed.json()["userId"]], "按账号撤销（句柄按账号算）"


def test_models_endpoint_reports_empty_when_logged_out(client):
    body = client.get("/api/v1/auth/models").json()
    assert body["islogin"] is False
    assert body["models"] == []


def test_quota_unavailable_without_apig(client):
    assert client.get("/api/v1/auth/quota").json() == {"ok": True, "available": False, "quota": None}


def test_quota_requires_the_callers_session(client, monkeypatch):
    _remote(monkeypatch, effective=True)
    started = _authorize(client)
    _callback(client, started["state"], code="c")
    response = TestClient(client.app).get("/api/v1/auth/quota")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_logged_in"
