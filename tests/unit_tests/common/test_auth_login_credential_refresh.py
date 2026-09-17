# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx
import pytest

from jiuwenswarm.common.auth import login_credentials as lc
from jiuwenswarm.common.auth.login_credentials import credential_ref_for_user, placeholder_api_key
from jiuwenswarm.common.e2a.constants import E2A_MODEL_AUTH_PARAM_KEY

API_BASE = "https://apig.example.com/v1"
REF = "abe633f3a47a2758174eabe9160daf36"


def _jwt(exp: float, marker: str = "a") -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(exp), "m": marker}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


@pytest.fixture(autouse=True)
def _clean_registry():
    lc.reset_for_test()
    yield
    lc.reset_for_test()


def _register(token: str, ref: str = REF) -> None:
    assert lc.login_auth_from_params(
        {E2A_MODEL_AUTH_PARAM_KEY: {"api_base": API_BASE, "api_key": token, "credential_ref": ref}}
    )


def test_active_and_expiring_handle_is_due():
    now = time.time()
    _register(_jwt(now + 10 * 60))
    assert lc.refs_due_for_refresh(now) == [REF]


def test_not_due_while_far_from_expiry():
    now = time.time()
    _register(_jwt(now + 50 * 60))
    assert lc.refs_due_for_refresh(now) == []


def test_idle_handle_is_not_refreshed():
    now = time.time()
    _register(_jwt(now + 10 * 60))
    later = now + lc.ACTIVE_WINDOW_S + 1
    assert lc.refs_due_for_refresh(later) == []


def test_already_expired_but_active_handle_is_still_due():
    now = time.time()
    _register(_jwt(now + 60))
    assert lc.refs_due_for_refresh(now + 5 * 60) == [REF]


def test_requests_are_throttled_until_retry_interval():
    now = time.time()
    _register(_jwt(now + 10 * 60))
    assert lc.refs_due_for_refresh(now) == [REF]
    assert lc.refs_due_for_refresh(now + 30) == [], "Gateway 还没回来就不要重复要"
    assert lc.refs_due_for_refresh(now + lc.REFRESH_RETRY_S + 1) == [REF], "续期失败要能重试"


def test_non_jwt_token_is_never_due():
    _register("opaque-token")
    assert lc.refs_due_for_refresh(time.time()) == []


@pytest.mark.asyncio
async def test_hook_use_keeps_the_handle_active(monkeypatch):
    start = time.time()
    _register(_jwt(start + 60 * 60))
    monkeypatch.setattr(lc.time, "time", lambda: start + 35 * 60)
    request = httpx.Request("POST", f"{API_BASE}/chat/completions",
                            headers={"Authorization": f"Bearer {placeholder_api_key(REF)}"})
    await lc.inject_login_credential(request)
    assert lc.refs_due_for_refresh(start + 50 * 60) == [REF]


@pytest.mark.asyncio
async def test_rejected_requests_do_not_count_as_use(monkeypatch):
    start = time.time()
    _register(_jwt(start + 60 * 60))
    monkeypatch.setattr(lc.time, "time", lambda: start + 35 * 60)
    request = httpx.Request("POST", "https://attacker.example.com/v1/chat/completions",
                            headers={"Authorization": f"Bearer {placeholder_api_key(REF)}"})
    await lc.inject_login_credential(request)
    assert lc.refs_due_for_refresh(start + 50 * 60) == []


@pytest.mark.asyncio
async def test_pushed_token_is_used_by_existing_models():
    _register(_jwt(time.time() + 60, "old"))
    new_token = _jwt(time.time() + 3600, "new")
    assert lc.apply_credential_update({"credential_ref": REF, "api_key": new_token}) is True

    request = httpx.Request("POST", f"{API_BASE}/chat/completions",
                            headers={"Authorization": f"Bearer {placeholder_api_key(REF)}"})
    await lc.inject_login_credential(request)
    assert request.headers["Authorization"] == f"Bearer {new_token}"
    assert lc.refs_due_for_refresh() == [], "续好之后不该再要"


def test_push_cannot_create_a_registration():
    assert lc.apply_credential_update({"credential_ref": REF, "api_key": "t"}) is False
    assert lc._lookup(REF) is None


def test_push_keeps_the_registered_endpoint():
    _register(_jwt(time.time() + 60))
    lc.apply_credential_update({"credential_ref": REF, "api_key": "t", "api_base": "https://attacker.example.com"})
    assert lc._lookup(REF).api_base == API_BASE


def test_revoke_stops_the_handle_immediately():
    _register(_jwt(time.time() + 3600))
    assert lc.apply_credential_update({"credential_ref": REF, "revoked": True}) is True
    assert lc._lookup(REF) is None


@pytest.mark.parametrize(
    "params",
    [None, {}, {"credential_ref": "sess-1", "api_key": "t"}, {"credential_ref": REF}, {"credential_ref": REF, "api_key": " "}],
)
def test_malformed_updates_are_rejected(params):
    _register(_jwt(time.time() + 60, "old"))
    assert lc.apply_credential_update(params) is False


@pytest.mark.asyncio
async def test_background_task_pushes_refresh_requests():
    _register(_jwt(time.time() + 10 * 60))
    sent: list[dict] = []

    async def send_push(msg):
        sent.append(msg)
        return True

    task = asyncio.create_task(lc.run_refresh_requests(send_push, interval_s=0.01))
    for _ in range(100):
        if sent:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(sent) == 1, "同一个句柄在重试间隔内只要一次"
    assert sent[0]["payload"] == {"event_type": lc.CREDENTIAL_REFRESH_EVENT, "credential_ref": REF}
    assert "token" not in json.dumps(sent[0]).lower().replace("credential_ref", "")


@pytest.mark.asyncio
async def test_background_task_survives_push_failures():
    _register(_jwt(time.time() + 10 * 60), ref=REF)
    other = "2b8ea975811361aee25cfeb50cbe084a"
    _register(_jwt(time.time() + 10 * 60), ref=other)
    attempts: list[str] = []

    async def send_push(msg):
        attempts.append(msg["payload"]["credential_ref"])
        raise ConnectionError("gateway gone")

    task = asyncio.create_task(lc.run_refresh_requests(send_push, interval_s=0.01))
    for _ in range(100):
        if len(attempts) >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert set(attempts) == {REF, other}, "一个发失败不能挡住其他句柄"


@pytest.fixture
def gateway_auth(tmp_path, monkeypatch):
    from jiuwenswarm.common.auth import service as service_mod
    from jiuwenswarm.common.auth import session_store as store_mod

    monkeypatch.setattr(store_mod, "auth_dir", lambda: tmp_path)
    monkeypatch.setattr(lc, "_ref_key_cache", b"k" * 32)
    store = store_mod.AuthSessionStore()
    monkeypatch.setattr(store_mod, "_store", store, raising=False)
    monkeypatch.setattr(store_mod, "get_session_store", lambda: store)

    class Flow:
        calls = 0
        result = "fresh"

        def refresh(self, credential):
            Flow.calls += 1
            if Flow.result == "fresh":
                from jiuwenswarm.common.auth.account_kit import Credential

                return Credential(id_token="id-new", refresh_token=credential.refresh_token,
                                  expires_at=time.time() + 3600)
            return None

    service = service_mod.AuthService(flow=Flow(), store=store)
    monkeypatch.setattr(service_mod, "_service", service)
    return store, Flow


def _login(store, ttl_s: float, user_id: str = "openid-1"):
    from jiuwenswarm.common.auth.account_kit import Credential, LoginOutcome

    return store.create(LoginOutcome(
        credential=Credential(id_token="id-old", refresh_token="rt", expires_at=time.time() + ttl_s),
        user_id=user_id,
        user_name="u",
    ))


def test_gateway_refreshes_ahead_of_its_own_threshold(gateway_auth):
    from jiuwenswarm.common.auth.passthrough import refreshed_credential_for_ref

    store, flow = gateway_auth
    session = _login(store, ttl_s=10 * 60)
    update = refreshed_credential_for_ref(credential_ref_for_user(session.user_id))
    assert update == {"credential_ref": credential_ref_for_user(session.user_id), "api_key": "id-new"}
    assert flow.calls == 1


def test_gateway_revokes_when_the_session_is_gone(gateway_auth):
    from jiuwenswarm.common.auth.passthrough import refreshed_credential_for_ref

    ref = credential_ref_for_user("openid-logged-out")
    assert refreshed_credential_for_ref(ref) == {"credential_ref": ref, "revoked": True}


def test_gateway_revokes_when_expired_and_refresh_fails(gateway_auth):
    from jiuwenswarm.common.auth.passthrough import refreshed_credential_for_ref

    store, flow = gateway_auth
    flow.result = None
    session = _login(store, ttl_s=-10)
    ref = credential_ref_for_user(session.user_id)
    assert refreshed_credential_for_ref(ref) == {"credential_ref": ref, "revoked": True}


def test_gateway_keeps_the_old_token_when_refresh_fails_but_it_is_still_valid(gateway_auth):
    from jiuwenswarm.common.auth.passthrough import refreshed_credential_for_ref

    store, flow = gateway_auth
    flow.result = None
    session = _login(store, ttl_s=10 * 60)
    ref = credential_ref_for_user(session.user_id)
    assert refreshed_credential_for_ref(ref) == {"credential_ref": ref, "api_key": "id-old"}


def test_gateway_picks_the_right_users_session(gateway_auth):
    from jiuwenswarm.common.auth.passthrough import refreshed_credential_for_ref

    store, _flow = gateway_auth
    _login(store, ttl_s=3600, user_id="openid-alice")
    bob = _login(store, ttl_s=3600, user_id="openid-bob")
    bob_ref = credential_ref_for_user(bob.user_id)
    assert refreshed_credential_for_ref(bob_ref)["credential_ref"] == bob_ref
    assert refreshed_credential_for_ref("not-a-ref") is None


def test_gateway_finds_the_account_after_it_logged_in_again(gateway_auth):
    from jiuwenswarm.common.auth.passthrough import refreshed_credential_for_ref

    store, _flow = gateway_auth
    first = _login(store, ttl_s=3600, user_id="openid-1")
    second = _login(store, ttl_s=3600, user_id="openid-1")  # 重新登录：旧会话被顶掉
    assert store.get(first.session_id) is None
    update = refreshed_credential_for_ref(credential_ref_for_user("openid-1"))
    assert update["api_key"] == second.credential.id_token
    assert "revoked" not in update
