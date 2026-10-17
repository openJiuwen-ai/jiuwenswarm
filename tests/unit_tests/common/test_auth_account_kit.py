# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import base64
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.auth import account_kit
from jiuwenswarm.common.auth.account_kit import (
    AccountKitFlow,
    Credential,
    OAuthConfig,
    OAuthError,
    PendingLogins,
)


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _id_token(**claims) -> str:
    return f"{_b64({'alg': 'RS256'})}.{_b64(claims)}.sig"


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


def _flow(**overrides) -> AccountKitFlow:
    config = {
        "client_id": "118944053",
        "redirect_uri": "http://localhost:19000/api/v1/auth/callback",
        "scope": "openid profile",
        "exchange_url": EXCHANGE_URL,
        **overrides,
    }
    return AccountKitFlow(OAuthConfig(**config))


def _token_endpoint(monkeypatch, payload: dict, status: int = 200, urls: list | None = None) -> list:
    sent: list = []

    def fake(method, url, data=None, **kwargs):
        sent.append(dict(data or {}))
        if urls is not None:
            urls.append(url)
        return SimpleNamespace(status_code=status, json=lambda: payload)

    monkeypatch.setattr(account_kit, "requests_request", fake)
    return sent


def test_config_defaults_match_the_agc_registration(monkeypatch):
    _remote(monkeypatch, apig_base="")
    config = OAuthConfig.from_remote()
    assert config.redirect_uri == "http://localhost:19000/api/v1/auth/callback"
    assert config.client_id == "118944053"
    assert config.exchange_url == "", "交换服务地址没有默认值，必须显式配置"


@pytest.mark.parametrize("effective", [True, False])
def test_login_enabled_follows_is_effective(monkeypatch, effective):
    _remote(monkeypatch, effective=effective, apig_base="")
    assert account_kit.login_enabled() is effective


def test_login_disabled_when_the_config_url_is_turned_off(monkeypatch):
    from jiuwenswarm.common.auth import remote_config

    monkeypatch.setenv(remote_config.CONFIG_URL_ENV, "off")
    remote_config.set_config_for_test(None)
    assert account_kit.login_enabled() is False


def test_login_section_drives_the_oauth_config(monkeypatch):
    _remote(monkeypatch, apig_base="", login={
        "client_id": "999", "redirect_uri": "http://localhost:19000/cb",
        "scope": "openid", "exchange_url": EXCHANGE_URL,
    })
    config = OAuthConfig.from_remote()
    assert (config.client_id, config.redirect_uri, config.scope) == ("999", "http://localhost:19000/cb", "openid")
    assert config.exchange_url == EXCHANGE_URL


def test_parse_id_token_returns_payload_and_swallows_garbage():
    assert account_kit.parse_id_token("") == {}
    assert account_kit.parse_id_token(None) == {}
    assert account_kit.parse_id_token("not-a-jwt") == {}
    assert account_kit.parse_id_token("a.not-valid-base64.c") == {}
    token = _id_token(openid="oid-1", display_name="n")
    assert account_kit.parse_id_token(token)["openid"] == "oid-1"


def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = account_kit.generate_pkce()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert "=" not in verifier and 43 <= len(verifier) <= 128  # RFC 7636 的长度要求


def test_exchange_takes_openid_display_name_and_exp(monkeypatch):
    exp = int(time.time()) + 3600
    _token_endpoint(monkeypatch, {
        "access_token": "AT",
        "refresh_token": "RT",
        "id_token": _id_token(openid="oid-1", sub="sub-1", display_name="138******00", exp=exp),
    })
    outcome = _flow().exchange_code("code", "verifier")
    assert outcome.user_id == "oid-1"
    assert outcome.user_name == "138******00"
    assert outcome.credential.expires_at == exp
    assert outcome.credential.refresh_token == "RT"


def test_exchange_without_id_token_fails(monkeypatch):
    _token_endpoint(monkeypatch, {"access_token": "AT", "refresh_token": "RT"})
    with pytest.raises(OAuthError) as excinfo:
        _flow().exchange_code("code", "verifier")
    assert excinfo.value.code == "oauth_token_exchange_failed"


def test_exchange_error_carries_the_error_code(monkeypatch):
    _token_endpoint(monkeypatch, {"error": "invalid_client"}, status=400)
    with pytest.raises(OAuthError) as excinfo:
        _flow().exchange_code("code", "verifier")
    assert excinfo.value.code == "oauth_token_exchange_failed"
    assert "invalid_client" in str(excinfo.value)


def test_refresh_keeps_the_old_refresh_token(monkeypatch):
    sent = _token_endpoint(monkeypatch, {
        "access_token": "AT2",
        "id_token": _id_token(openid="oid-1", exp=int(time.time()) + 3600),
    })
    refreshed = _flow().refresh(Credential(id_token="old", refresh_token="RT-1", expires_at=0))
    assert refreshed is not None
    assert refreshed.refresh_token == "RT-1"
    assert refreshed.id_token != "old"
    assert sent == [{"grant_type": "refresh_token", "refresh_token": "RT-1", "client_id": "118944053"}]


def test_refresh_failure_returns_none(monkeypatch):
    _token_endpoint(monkeypatch, {"error": "invalid_grant"}, status=400)
    assert _flow().refresh(Credential(id_token="old", refresh_token="RT", expires_at=0)) is None


def test_refresh_without_refresh_token_does_not_call_out(monkeypatch):
    sent = _token_endpoint(monkeypatch, {})
    assert _flow().refresh(Credential(id_token="old")) is None
    assert sent == []


def test_token_requests_go_to_the_exchange_service_without_any_secret(monkeypatch):
    urls: list = []
    sent = _token_endpoint(
        monkeypatch,
        {"id_token": _id_token(openid="oid-1", exp=int(time.time()) + 3600), "refresh_token": "RT"},
        urls=urls,
    )
    flow = _flow()
    assert flow.exchange_code("code", "verifier").user_id == "oid-1"
    assert flow.refresh(Credential(id_token="x", refresh_token="RT")) is not None

    assert urls == [EXCHANGE_URL, EXCHANGE_URL]
    assert all("client_secret" not in form for form in sent)
    assert sent[0] == {
        "grant_type": "authorization_code",
        "code": "code",
        "code_verifier": "verifier",
        "redirect_uri": "http://localhost:19000/api/v1/auth/callback",
        "client_id": "118944053",
    }


def test_without_exchange_url_login_fails_fast():
    with pytest.raises(OAuthError) as excinfo:
        _flow(exchange_url="").create_authorization_request()
    assert excinfo.value.code == "exchange_not_configured"


def test_exchange_service_errors_surface_with_their_code(monkeypatch):
    _token_endpoint(monkeypatch, {"error": "rate_limited"}, status=429)
    with pytest.raises(OAuthError) as excinfo:
        _flow().exchange_code("code", "verifier")
    assert excinfo.value.code == "oauth_token_exchange_failed"
    assert "429" in str(excinfo.value) and "rate_limited" in str(excinfo.value)


def test_credential_expiry_windows():
    now = time.time()
    assert Credential(id_token="t", expires_at=now + 3600).is_expired() is False
    assert Credential(id_token="t", expires_at=now - 1).is_expired() is True
    assert Credential(id_token="t", expires_at=now + 120).expires_within(300) is True
    assert Credential(id_token="", expires_at=now + 3600).is_expired() is True, "没有 token 就是不可用"


def test_credential_roundtrip_tolerates_garbage():
    assert Credential.from_dict({"expires_at": "not-a-number"}).expires_at == 0.0
    original = Credential(id_token="i", refresh_token="r", expires_at=123.0)
    assert Credential.from_dict(original.to_dict()) == original


def test_pending_login_expires(monkeypatch):
    pending = PendingLogins()
    item = pending.add("verifier")
    later = time.time() + account_kit.STATE_TTL_S + 1
    monkeypatch.setattr(account_kit.time, "time", lambda: later)
    with pytest.raises(OAuthError) as excinfo:
        pending.begin_callback(item.state)
    assert excinfo.value.code == "oauth_state_invalid"


def test_claim_window_restarts_when_callback_lands(monkeypatch):
    pending = PendingLogins()
    item = pending.add("verifier")
    start = time.time()

    monkeypatch.setattr(account_kit.time, "time", lambda: start + account_kit.STATE_TTL_S - 5)
    pending.begin_callback(item.state)
    pending.finish(item.state, session_id="sess-1")

    monkeypatch.setattr(account_kit.time, "time", lambda: start + account_kit.STATE_TTL_S + 60)
    assert pending.claim(item.state, item.claim_token) == "sess-1"


def test_state_and_claim_token_are_unguessable():
    pending = PendingLogins()
    a, b = pending.add("v"), pending.add("v")
    assert a.state != b.state and a.claim_token != b.claim_token
    assert len(a.state) >= 22 and len(a.claim_token) >= 43  # ≥128 / 256 bit
