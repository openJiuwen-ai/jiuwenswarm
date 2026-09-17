# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.auth import apig, model_catalog
from jiuwenswarm.common.auth.model_catalog import (
    LOGIN_MODEL_SOURCE,
    build_model_entry,
    is_login_model,
)
from jiuwenswarm.common.auth.service import ModelAuthRequired


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth"
    auth_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(model_catalog, "_cache_path", lambda: auth_path / "model_catalog.json")
    monkeypatch.setattr(model_catalog, "_memo", None, raising=False)
    monkeypatch.setattr(model_catalog, "_last_discovery_failure_at", 0.0, raising=False)
    monkeypatch.setattr(
        model_catalog,
        "get_auth_service",
        lambda: SimpleNamespace(resolve_session=lambda _s=None: None),
    )
    _remote(monkeypatch)
    yield auth_path
    model_catalog._memo = None
    from jiuwenswarm.common.auth import remote_config

    remote_config.set_config_for_test(None)


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


SESSION = "sess-1"


class _Apig:

    def __init__(self, payload=None, status: int = 200) -> None:
        self.payload = payload if payload is not None else {"data": [{"id": "glm-5"}]}
        self.status = status
        self.calls: list[dict] = []

    def __call__(self, method, url, headers=None, **kwargs):
        self.calls.append({"method": method, "url": url, "headers": headers})
        return SimpleNamespace(status_code=self.status, json=lambda: self.payload)


def _serve(monkeypatch, payload=None, status: int = 200) -> _Apig:
    fake = _Apig(payload, status)
    monkeypatch.setattr(apig, "requests_request", fake)
    return fake


def _login(monkeypatch, tokens: dict | None = None) -> list:
    tokens = {SESSION: "id-local"} if tokens is None else tokens
    seen: list = []

    def fake_resolve(session_id=None, allow_refresh=True):
        seen.append((session_id, allow_refresh))
        if session_id not in tokens:
            raise ModelAuthRequired("请先登录后再使用该模型", "not_logged_in")
        return tokens[session_id]

    monkeypatch.setattr(model_catalog, "resolve_id_token", fake_resolve)
    return seen


def test_discover_models_calls_apig_with_the_sessions_id_token(monkeypatch):
    fake = _serve(monkeypatch, {"data": [{"id": "glm-5", "name": "GLM-5"}]})
    _login(monkeypatch, {"sess-a": "id-a"})

    models = model_catalog.discover_models("sess-a")
    assert [m.model_name for m in models] == ["glm-5"]
    assert fake.calls[0]["url"] == "https://apig.example.com/v1/models"
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer id-a"


def test_discover_without_apig_is_empty_and_offline(monkeypatch):
    _remote(monkeypatch, apig_base="")
    fake = _serve(monkeypatch)
    _login(monkeypatch)
    assert model_catalog.discover_models(SESSION) == []
    assert fake.calls == []


def test_discover_returns_empty_when_apig_fails(monkeypatch):
    _serve(monkeypatch, {"error": "boom"}, status=500)
    _login(monkeypatch)
    assert model_catalog.discover_models(SESSION) == []


def test_discover_propagates_not_logged_in(monkeypatch):
    _serve(monkeypatch)
    _login(monkeypatch, tokens={})
    with pytest.raises(ModelAuthRequired):
        model_catalog.discover_models(SESSION)


def test_whitelist_filters_by_id_or_display_name(monkeypatch):
    _remote(monkeypatch, whitelist=["GLM-5", "DeepSeek-V3.2"])
    _serve(monkeypatch, {"data": [{"id": "glm-5", "name": "GLM-5"}, {"id": "qwen-plus", "name": "Qwen-Plus"}]})
    _login(monkeypatch)
    assert [m.model_name for m in model_catalog.discover_models(SESSION)] == ["glm-5"]


def test_blocklist_removes_models(monkeypatch):
    _remote(monkeypatch, blocklist=["deepseek-r1-batch"])
    _serve(monkeypatch, {"data": [{"id": "glm-5"}, {"id": "deepseek-r1-batch"}]})
    _login(monkeypatch)
    assert [m.model_name for m in model_catalog.discover_models(SESSION)] == ["glm-5"]


def test_get_models_uses_cache_without_network(monkeypatch):
    fake = _serve(monkeypatch)
    _login(monkeypatch)

    assert [m.model_name for m in model_catalog.get_models(SESSION)] == ["glm-5"]
    assert len(fake.calls) == 1

    model_catalog._memo = None  # 模拟新进程，只剩磁盘缓存
    assert [m.model_name for m in model_catalog.get_models(SESSION)] == ["glm-5"]
    assert len(fake.calls) == 1  # 命中缓存，没有再打网络


def test_cache_holds_no_credentials(monkeypatch, _isolated):
    _serve(monkeypatch)
    _login(monkeypatch, {SESSION: "id-SECRET"})
    model_catalog.refresh(SESSION)
    assert "id-SECRET" not in (_isolated / "model_catalog.json").read_text(encoding="utf-8")


def test_get_models_returns_empty_when_not_logged_in(monkeypatch):
    _serve(monkeypatch)
    _login(monkeypatch, tokens={})
    assert model_catalog.get_models(SESSION) == []


def test_clear_cache_removes_file(monkeypatch, _isolated):
    _serve(monkeypatch)
    _login(monkeypatch)
    model_catalog.refresh(SESSION)
    assert (_isolated / "model_catalog.json").exists()
    model_catalog.clear_cache()
    assert not (_isolated / "model_catalog.json").exists()


def test_build_model_entry_shape():
    entry = build_model_entry(
        model_name="glm-5", api_base="https://apig.example.com/v1", api_key="id-1", display_name="GLM-5"
    )
    mcc = entry["model_client_config"]
    assert mcc["model_name"] == "glm-5"
    assert mcc["client_provider"] == "OpenAI"
    assert mcc["api_key"] == "id-1"
    assert "custom_headers" not in mcc
    assert entry["source"] == LOGIN_MODEL_SOURCE
    assert entry["read_only"] is True
    assert entry["is_default"] is not False
    assert entry["is_free"] is True
    assert entry["alias"] == "GLM-5"


def test_is_login_model():
    assert is_login_model({"source": LOGIN_MODEL_SOURCE})
    assert is_login_model({"model_source": LOGIN_MODEL_SOURCE})  # 前端回传的扁平形态
    assert not is_login_model({"source": "config"})
    assert not is_login_model({})
    assert not is_login_model(None)


def test_list_entries_use_apig_invoke_base_and_carry_no_token(monkeypatch):
    _serve(monkeypatch, {"data": [{"id": "glm-5", "baseUrl": "https://gateway.internal/v1"}]})
    _login(monkeypatch, {"sess-a": "id-a"})
    entries = model_catalog.list_login_model_entries("sess-a")
    assert len(entries) == 1
    mcc = entries[0]["model_client_config"]
    assert mcc["api_base"] == "https://apig.example.com/v1"
    assert mcc["api_key"] == ""
    assert "id-a" not in str(entries)


def test_list_entries_use_the_given_session(monkeypatch):
    _serve(monkeypatch)
    seen = _login(monkeypatch, {"sess-a": "id-a", "sess-b": "id-b"})

    entries = model_catalog.list_login_model_entries("sess-b")
    assert entries and "id-b" not in str(entries), "条目里不放 token"
    assert seen[0][0] == "sess-b", "登录校验用的是传进来的会话"
    assert model_catalog.list_login_model_entries() == []


def test_list_entries_can_skip_network(monkeypatch, _isolated):
    fake = _serve(monkeypatch)
    seen = _login(monkeypatch)

    model_catalog.get_models(SESSION)
    assert len(fake.calls) == 1
    cache_file = _isolated / "model_catalog.json"
    stale = json.loads(cache_file.read_text(encoding="utf-8"))
    stale["fetched_at"] = 0.0
    cache_file.write_text(json.dumps(stale), encoding="utf-8")
    model_catalog._memo = None
    seen.clear()

    entries = model_catalog.list_login_model_entries(SESSION, allow_refresh=False)
    assert len(fake.calls) == 1, "allow_refresh=False 时不该再打网络"
    assert seen and all(allow is False for _, allow in seen), "取 token 也不能同步续期"
    assert [e["model_client_config"]["model_name"] for e in entries] == ["glm-5"], (
        "过期缓存也要照常给出条目——少认识一个模型比卡住请求好，但不能直接返回空"
    )

    model_catalog._memo = None
    model_catalog.list_login_model_entries(SESSION)
    assert len(fake.calls) == 2


def test_campaign_off_yields_no_models_even_with_a_warm_cache(monkeypatch, _isolated):
    _serve(monkeypatch)
    _login(monkeypatch)
    assert [m.model_name for m in model_catalog.get_models(SESSION)] == ["glm-5"]  # 缓存已预热

    from jiuwenswarm.common.auth import remote_config

    for config in (None, remote_config.parse_config({"is_effective": False})):
        remote_config.set_config_for_test(config)
        model_catalog._memo = None
        assert model_catalog.get_models(SESSION) == []
        assert model_catalog.list_login_model_entries(SESSION) == []


def test_failed_discovery_is_not_retried_on_every_models_list(monkeypatch):
    """APIG 不通时，每次 models.list 都再撞一次（最长 10 秒）：下拉迟迟出不来、日志刷屏。"""
    _login(monkeypatch)
    fake = _serve(monkeypatch, {"error_msg": "Backend unavailable", "error_code": "APIG.0202"}, status=502)
    for _ in range(5):
        assert model_catalog.get_models(SESSION) == []
    assert len(fake.calls) == 1


def test_discovery_is_retried_after_the_cooldown(monkeypatch):
    _login(monkeypatch)
    fake = _serve(monkeypatch, {"error_code": "APIG.0202"}, status=502)
    model_catalog.get_models(SESSION)
    monkeypatch.setattr(
        model_catalog, "_last_discovery_failure_at",
        model_catalog._last_discovery_failure_at - model_catalog._DISCOVERY_RETRY_AFTER_FAILURE_S - 1,
    )
    fake.status, fake.payload = 200, {"data": [{"id": "glm-5"}]}
    assert [m.model_name for m in model_catalog.get_models(SESSION)] == ["glm-5"]
    assert len(fake.calls) == 2


def test_not_logged_in_does_not_start_the_cooldown(monkeypatch):
    tokens: dict = {}
    _login(monkeypatch, tokens)
    fake = _serve(monkeypatch)
    assert model_catalog.get_models(SESSION) == []
    tokens[SESSION] = "id-now-logged-in"
    assert [m.model_name for m in model_catalog.get_models(SESSION)] == ["glm-5"]
    assert len(fake.calls) == 1


def test_explicit_refresh_after_login_ignores_the_cooldown(monkeypatch):
    _login(monkeypatch)
    fake = _serve(monkeypatch, {"error_code": "APIG.0202"}, status=502)
    model_catalog.get_models(SESSION)
    fake.status, fake.payload = 200, {"data": [{"id": "glm-5"}]}
    assert [m.model_name for m in model_catalog.refresh(SESSION)] == ["glm-5"]
