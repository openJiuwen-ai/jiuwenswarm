# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import base64
import json
import threading
import time
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.auth import account_kit, service as service_mod
from jiuwenswarm.common.auth import session_store as store_mod
from jiuwenswarm.common.auth.account_kit import AccountKitFlow, OAuthConfig, claim_fingerprint
from jiuwenswarm.common.auth.service import AuthService

CALLBACK_URL = "https://aigw.example.com/account-kit/callback"
CLAIM_URL = "https://aigw.example.com/account-kit/claim"
EXCHANGE_URL = "https://aigw.example.com/account-kit/token"
FINGERPRINT_VECTOR = ("claim-token-abcdefghijklmnop", "40bf44d358dd49a857822f1601f433d4")


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _id_token(openid: str = "openid-1") -> str:
    payload = {"openid": openid, "exp": int(time.time()) + 3600, "display_name": "138******00"}
    return f"{_b64({'alg': 'RS256'})}.{_b64(payload)}.sig"


class FakeExchange:

    def __init__(self) -> None:
        self.claims: list[dict] = []
        self.token_forms: list[dict] = []
        self.claim_responses: list[tuple[int, dict]] = []
        self.claim_failures = 0

    def __call__(self, method, url, data=None, **kwargs):
        form = dict(data or {})
        if url == CLAIM_URL:
            self.claims.append(form)
            if self.claim_failures > 0:
                self.claim_failures -= 1
                raise OSError("connection reset")
            status, payload = self.claim_responses.pop(0) if self.claim_responses else (202, {"pending": True})
            return SimpleNamespace(status_code=status, json=lambda: payload)
        self.token_forms.append(form)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"id_token": _id_token(), "refresh_token": "RT", "expires_in": 3600},
        )


@pytest.fixture
def exchange(monkeypatch) -> FakeExchange:
    fake = FakeExchange()
    monkeypatch.setattr(account_kit, "requests_request", fake)
    return fake


@pytest.fixture
def service(monkeypatch, tmp_path, exchange) -> AuthService:
    monkeypatch.setattr(store_mod, "auth_dir", lambda: tmp_path)
    monkeypatch.setattr(service_mod, "CLAIM_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(service_mod, "STATE_TTL_S", 1.0)
    flow = AccountKitFlow(
        OAuthConfig(
            client_id="118944053",
            redirect_uri="http://localhost:19000/api/v1/auth/callback",
            scope="openid profile",
            exchange_url=EXCHANGE_URL,
            callback_url=CALLBACK_URL,
        )
    )
    service = AuthService(flow=flow, store=store_mod.AuthSessionStore(persist=False))
    yield service
    with flow.pending._lock:
        flow.pending._items.clear()
    time.sleep(0.05)


def _wait_for(predicate, timeout_s: float = 3.0) -> None:
    deadline = time.time() + timeout_s
    while not predicate():
        assert time.time() < deadline, "等待超时"
        time.sleep(0.01)


def test_fingerprint_matches_the_exchange_service():
    claim_token, expected = FINGERPRINT_VECTOR
    assert claim_fingerprint(claim_token) == expected


def test_claim_url_is_derived_from_the_callback_url():
    config = OAuthConfig(client_id="1", redirect_uri="http://localhost:19000/cb", scope="openid",
                         exchange_url=EXCHANGE_URL, callback_url=CALLBACK_URL)
    assert config.effective_claim_url == CLAIM_URL
    assert config.effective_redirect_uri == CALLBACK_URL


def test_without_callback_url_everything_stays_local():
    config = OAuthConfig(client_id="1", redirect_uri="http://localhost:19000/cb", scope="openid",
                         exchange_url=EXCHANGE_URL)
    assert config.effective_redirect_uri == "http://localhost:19000/cb"
    assert config.effective_claim_url == ""


def test_authorize_url_points_at_the_exchange_and_state_carries_the_fingerprint(service):
    request = service.create_authorization_request()
    assert f"redirect_uri={CALLBACK_URL.replace(':', '%3A').replace('/', '%2F')}" in request["authorizeUrl"]
    assert request["state"].endswith(claim_fingerprint(request["claimToken"]))
    assert request["claimToken"] not in request["authorizeUrl"], "认领凭证不能进地址栏"


def test_claimed_code_completes_the_login(service, exchange):
    exchange.claim_responses = [(202, {"pending": True}), (200, {"code": "auth-code-1"})]
    request = service.create_authorization_request()

    assert service.claim(request["state"], request["claimToken"]) is None
    _wait_for(lambda: (service.flow.pending.get(request["state"]) or SimpleNamespace(session_id=None)).session_id)
    session = service.claim(request["state"], request["claimToken"])
    assert session is not None and session.user_id == "openid-1"

    assert exchange.claims[0] == {"state": request["state"], "claim_token": request["claimToken"]}
    assert exchange.token_forms[0]["redirect_uri"] == CALLBACK_URL
    assert exchange.token_forms[0]["grant_type"] == "authorization_code"


def test_user_cancelled_reaches_the_starter(service, exchange):
    exchange.claim_responses = [(200, {"error": "access_denied", "error_description": "用户取消"})]
    request = service.create_authorization_request()

    def _claimed_error():
        try:
            service.claim(request["state"], request["claimToken"])
        except account_kit.OAuthError as error:
            return error.code == "oauth_access_denied"
        return False

    _wait_for(_claimed_error)


def test_network_hiccups_do_not_kill_the_login(service, exchange):
    exchange.claim_failures = 2
    exchange.claim_responses = [(200, {"code": "auth-code-2"})]
    service.create_authorization_request()

    _wait_for(lambda: service.store.any_session() is not None)
    assert service.store.any_session().user_id == "openid-1"
    assert len(exchange.claims) >= 3, "抖了两次之后还在继续轮询"


def test_polling_stops_once_the_login_is_gone(service, exchange):
    request = service.create_authorization_request()
    _wait_for(lambda: exchange.claims)
    with service.flow.pending._lock:
        service.flow.pending._items.pop(request["state"], None)  # 取消 = 丢掉待完成记录
    _wait_for(lambda: not any(t.name == "auth-claim-poll" and t.is_alive() for t in threading.enumerate()))
    before = len(exchange.claims)
    time.sleep(0.05)
    assert len(exchange.claims) == before, "待完成记录没了就该停"
