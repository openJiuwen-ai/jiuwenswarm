# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.common.auth import login_credentials, passthrough
from jiuwenswarm.common.auth.login_credentials import credential_ref_for_user
from jiuwenswarm.common.auth.model_catalog import LoginModel
from jiuwenswarm.common.e2a.constants import E2A_MODEL_AUTH_PARAM_KEY


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


@pytest.fixture(autouse=True)
def _fixed_ref_key(monkeypatch):
    monkeypatch.setattr(login_credentials, "_ref_key_cache", b"k" * 32)


@pytest.fixture
def apig_env(monkeypatch):
    _remote(monkeypatch)


@pytest.fixture
def login_model(monkeypatch):
    seen: list = []
    monkeypatch.setattr(
        "jiuwenswarm.common.auth.model_catalog.get_models",
        lambda session_id=None, allow_refresh=True: [LoginModel(model_name="glm-5", display_name="GLM-5")],
    )

    def _live(session_id=None, allow_refresh=True):
        seen.append((session_id, allow_refresh))
        return SimpleNamespace(user_id=f"openid-{session_id}", credential=SimpleNamespace(id_token=f"id-{session_id}"))

    monkeypatch.setattr("jiuwenswarm.common.auth.service.live_session", _live)
    return seen


def test_client_supplied_auth_is_always_dropped():
    params = {
        "query": "hi",
        E2A_MODEL_AUTH_PARAM_KEY: {
            "api_base": "https://attacker.example.com",
            "api_key": "stolen-token",
        },
    }
    passthrough.normalize_model_auth(params, session_id=None)
    assert E2A_MODEL_AUTH_PARAM_KEY not in params


def test_client_supplied_auth_dropped_even_for_a_real_login_model(apig_env, login_model):
    params = {
        "model_name": "glm-5",
        E2A_MODEL_AUTH_PARAM_KEY: {
            "api_base": "https://attacker.example.com",
            "api_key": "stolen-token",
        },
    }
    passthrough.normalize_model_auth(params, session_id="sess-1")
    injected = params[E2A_MODEL_AUTH_PARAM_KEY]
    assert injected == {
        "api_base": "https://apig.example.com/v1",
        "api_key": "id-sess-1",
        "credential_ref": credential_ref_for_user("openid-sess-1"),
    }
    assert "attacker" not in str(injected)


def test_injects_the_callers_id_token_as_api_key(apig_env, login_model):
    params = {"model_name": "glm-5"}
    passthrough.normalize_model_auth(params, session_id="sess-1")
    assert params[E2A_MODEL_AUTH_PARAM_KEY] == {
        "api_base": "https://apig.example.com/v1",
        "api_key": "id-sess-1",
        "credential_ref": credential_ref_for_user("openid-sess-1"),
    }
    assert login_model == [("sess-1", False)]


def test_credential_ref_differs_per_account_and_reveals_nothing(apig_env, login_model):
    first = {"model_name": "glm-5"}
    second = {"model_name": "glm-5"}
    passthrough.normalize_model_auth(first, session_id="sess-1")
    passthrough.normalize_model_auth(second, session_id="sess-2")
    ref_1 = first[E2A_MODEL_AUTH_PARAM_KEY]["credential_ref"]
    ref_2 = second[E2A_MODEL_AUTH_PARAM_KEY]["credential_ref"]
    assert ref_1 != ref_2
    assert "sess-1" not in ref_1 and "openid" not in ref_1


def test_credential_ref_is_keyed_so_openid_alone_does_not_give_it(monkeypatch):
    ref_a = credential_ref_for_user("openid-1")
    monkeypatch.setattr(login_credentials, "_ref_key_cache", b"x" * 32)
    assert credential_ref_for_user("openid-1") != ref_a


def test_no_injection_without_apig(monkeypatch, login_model):
    _remote(monkeypatch, apig_base="")
    params = {"model_name": "glm-5"}
    passthrough.normalize_model_auth(params, session_id="sess-1")
    assert E2A_MODEL_AUTH_PARAM_KEY not in params


def test_injects_when_model_name_carries_global_index(apig_env, login_model):
    params = {"model_name": "glm-5#3"}
    passthrough.normalize_model_auth(params, session_id="sess-1")
    assert E2A_MODEL_AUTH_PARAM_KEY in params


def test_no_injection_for_user_configured_model(apig_env, login_model):
    params = {"model_name": "my-own-gpt"}
    passthrough.normalize_model_auth(params, session_id="sess-1")
    assert E2A_MODEL_AUTH_PARAM_KEY not in params


def test_no_injection_without_model_name(apig_env, login_model):
    params = {"query": "hi"}
    passthrough.normalize_model_auth(params, session_id="sess-1")
    assert E2A_MODEL_AUTH_PARAM_KEY not in params


def test_expired_session_injects_nothing_instead_of_raising(apig_env, monkeypatch, login_model):
    from jiuwenswarm.common.auth.service import ModelAuthRequired

    def _expired(session_id=None, allow_refresh=True):
        raise ModelAuthRequired("登录已过期", "session_expired")

    monkeypatch.setattr("jiuwenswarm.common.auth.service.live_session", _expired)
    params = {"model_name": "glm-5"}
    passthrough.normalize_model_auth(params, session_id="sess-1")
    assert E2A_MODEL_AUTH_PARAM_KEY not in params


def test_catalog_lookup_never_triggers_a_network_refresh(apig_env, monkeypatch):
    seen: dict = {}

    def _get_models(session_id=None, allow_refresh=True):
        seen["allow_refresh"] = allow_refresh
        return []

    monkeypatch.setattr(
        "jiuwenswarm.common.auth.model_catalog.get_models", _get_models
    )
    passthrough.normalize_model_auth({"model_name": "glm-5"}, session_id="sess-1")
    assert seen["allow_refresh"] is False


def test_request_without_session_gets_nothing_even_if_someone_is_logged_in(apig_env, monkeypatch, tmp_path):
    import time

    from jiuwenswarm.common.auth import service as service_mod
    from jiuwenswarm.common.auth import session_store as store_mod
    from jiuwenswarm.common.auth.account_kit import Credential, LoginOutcome

    monkeypatch.setattr(store_mod, "auth_dir", lambda: tmp_path)
    store = store_mod.AuthSessionStore()
    monkeypatch.setattr(service_mod, "_service", service_mod.AuthService(store=store))
    monkeypatch.setattr(
        "jiuwenswarm.common.auth.model_catalog.get_models",
        lambda session_id=None, allow_refresh=True: [LoginModel(model_name="glm-5", display_name="GLM-5")],
    )
    session = store.create(LoginOutcome(
        credential=Credential(id_token="id-owner", refresh_token="rt", expires_at=time.time() + 3600),
        user_id="openid-owner",
        user_name="owner",
    ))

    anonymous = {"model_name": "glm-5"}
    passthrough.normalize_model_auth(anonymous, session_id=None)
    assert E2A_MODEL_AUTH_PARAM_KEY not in anonymous

    owner = {"model_name": "glm-5"}
    passthrough.normalize_model_auth(owner, session_id=session.session_id)
    assert owner[E2A_MODEL_AUTH_PARAM_KEY]["api_key"] == "id-owner"


def test_logging_in_again_keeps_the_credential_ref(apig_env, monkeypatch, tmp_path):
    import time

    from jiuwenswarm.common.auth import service as service_mod
    from jiuwenswarm.common.auth import session_store as store_mod
    from jiuwenswarm.common.auth.account_kit import Credential, LoginOutcome

    monkeypatch.setattr(store_mod, "auth_dir", lambda: tmp_path)
    store = store_mod.AuthSessionStore()
    monkeypatch.setattr(service_mod, "_service", service_mod.AuthService(store=store))
    monkeypatch.setattr(
        "jiuwenswarm.common.auth.model_catalog.get_models",
        lambda session_id=None, allow_refresh=True: [LoginModel(model_name="glm-5", display_name="GLM-5")],
    )

    def _login(token):
        return store.create(LoginOutcome(
            credential=Credential(id_token=token, refresh_token="rt", expires_at=time.time() + 3600),
            user_id="openid-owner",
            user_name="owner",
        ))

    before = {"model_name": "glm-5"}
    passthrough.normalize_model_auth(before, session_id=_login("id-first").session_id)
    after = {"model_name": "glm-5"}
    passthrough.normalize_model_auth(after, session_id=_login("id-second").session_id)

    assert after[E2A_MODEL_AUTH_PARAM_KEY]["credential_ref"] == before[E2A_MODEL_AUTH_PARAM_KEY]["credential_ref"]
    assert after[E2A_MODEL_AUTH_PARAM_KEY]["api_key"] == "id-second"


def test_forwarding_path_never_refreshes_remote_config_synchronously(monkeypatch, login_model):
    seen: dict = {}

    def _resolve(allow_refresh=True):
        seen["allow_refresh"] = allow_refresh
        return None

    monkeypatch.setattr("jiuwenswarm.common.auth.apig.resolve_apig_config", _resolve)
    passthrough.normalize_model_auth({"model_name": "glm-5"}, session_id="sess-1")
    assert seen["allow_refresh"] is False
