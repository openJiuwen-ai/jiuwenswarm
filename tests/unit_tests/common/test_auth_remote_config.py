# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.auth import remote_config
from jiuwenswarm.common.auth.remote_config import RemoteConfig, get_config, parse_config

FULL = {
    "is_effective": True,
    "ttl_seconds": 900,
    "huaweicloud_login": {
        "auth_base": "https://auth.example.com",
        "iam_base": "https://sts.example.com",
        "claw_base": "https://claw.example.com",
        "client_id": "client-1",
        "redirect_uri": "https://claw.example.com/v1/claw/auth/callback",
    },
    "gateway": {"base_url": "https://gw.example.com", "quota_path": "/billing/usage"},
    "models": {"whitelist": ["GLM-5"], "blocklist": ["deepseek-r1-batch"]},
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    remote_config.set_config_for_test(None)
    monkeypatch.setenv(remote_config.CONFIG_URL_ENV, "https://config.example.com")
    yield
    remote_config.set_config_for_test(None)


def _serve(monkeypatch, payload, status: int = 200, fail: Exception | None = None) -> dict:
    calls = {"n": 0}

    def fake(method, url, **kwargs):
        calls["n"] += 1
        if fail is not None:
            raise fail
        return SimpleNamespace(status_code=status, json=lambda: payload)

    monkeypatch.setattr(remote_config, "requests_request", fake)
    return calls


def test_parses_the_full_payload():
    config = parse_config(FULL)
    assert config.is_effective is True
    assert config.gateway.base_url == "https://gw.example.com"
    assert config.gateway.quota_path == "/billing/usage"
    assert config.gateway.models_path == "/v1/models", "没给的字段用默认值"
    assert config.models.whitelist == frozenset({"glm-5"}), "名单统一小写，大小写不该决定放不放行"
    assert config.models.blocklist == frozenset({"deepseek-r1-batch"})


def test_login_section_is_passed_through_untouched():
    login = parse_config(FULL).login("huaweicloud_login")
    assert login.value("client_id") == "client-1"
    assert login.value("iam_base") == "https://sts.example.com"
    assert login.value("exchange_url", "fallback") == "fallback", "没给就用调用方的默认值"


def test_login_forms_are_indexed_by_section_name():
    config = parse_config({**FULL, "someother_login": {"client_id": "client-2"}})
    assert config.login("huaweicloud_login").value("client_id") == "client-1"
    assert config.login("someother_login").value("client_id") == "client-2"


def test_an_undeclared_login_form_is_empty_not_an_error():
    assert parse_config(FULL).login("nosuch_login").value("client_id", "default") == "default"


@pytest.mark.parametrize("payload", [None, {}, [], "nope", {"huaweicloud_login": "not-a-dict"}])
def test_garbage_payloads_degrade_instead_of_raising(payload):
    config = parse_config(payload)
    assert config.is_effective is False
    assert config.gateway.base_url == ""
    assert config.models.whitelist is None


def test_empty_whitelist_means_no_filtering():
    assert parse_config({"models": {"whitelist": []}}).models.whitelist is None


def test_version_wrapper_is_unwrapped():
    config = parse_config({"v1.0": FULL})
    assert config.is_effective is True
    assert config.gateway.base_url == "https://gw.example.com"
    assert config.login("huaweicloud_login").value("client_id") == "client-1"


def test_newest_version_wins_when_several_are_published():
    config = parse_config({"v1.0": {"is_effective": False}, "v1.10": FULL, "v1.2": {}})
    assert config.is_effective is True, "v1.10 比 v1.2 新，不能按字符串比大小"


def test_a_business_field_is_not_mistaken_for_a_version_wrapper():
    config = parse_config({"is_effective": True, "v2": {"whatever": 1}})
    assert config.is_effective is True


def test_base_url_without_scheme_gets_https():
    assert parse_config({"gateway": {"base_url": "gw.example.com/"}}).gateway.base_url == "https://gw.example.com"


@pytest.mark.parametrize("value", ["", "  ", "off", "none", "0"])
def test_explicitly_disabled_means_off(monkeypatch, value):
    monkeypatch.setenv(remote_config.CONFIG_URL_ENV, value)
    calls = _serve(monkeypatch, FULL)
    assert get_config() is None
    assert calls["n"] == 0


def test_the_official_endpoint_is_the_default(monkeypatch):
    monkeypatch.delenv(remote_config.CONFIG_URL_ENV)
    assert remote_config.config_url() == remote_config.DEFAULT_CONFIG_URL
    assert remote_config.DEFAULT_CONFIG_URL.startswith("https://")

    requested = []

    def _fake(method, url, **kwargs):
        requested.append(url)
        return SimpleNamespace(status_code=200, json=lambda: FULL)

    monkeypatch.setattr(remote_config, "requests_request", _fake)
    assert get_config().is_effective is True
    assert requested == [remote_config.DEFAULT_CONFIG_URL]


def test_env_overrides_the_default(monkeypatch):
    monkeypatch.setenv(remote_config.CONFIG_URL_ENV, " https://staging.example.com/v1/config ")
    assert remote_config.config_url() == "https://staging.example.com/v1/config"


def test_never_refetches_once_it_has_a_config(monkeypatch):
    calls = _serve(monkeypatch, {**FULL, "ttl_seconds": 30})
    first = get_config()
    remote_config.set_config_for_test(
        RemoteConfig(**{**first.__dict__, "fetched_at": time.time() - 86400})
    )
    for _ in range(5):
        get_config()
        get_config(allow_refresh=False)
    time.sleep(0.2)  # 后台线程若被启动，这点时间足够它发出请求
    assert calls["n"] == 1


def _wait_for(predicate, timeout_s: float = 3.0) -> None:
    deadline = time.time() + timeout_s
    while not predicate():
        assert time.time() < deadline, "等待超时"
        time.sleep(0.01)


def test_hot_path_never_blocks_on_the_network_but_refreshes_in_background(monkeypatch):
    import threading

    calls = _serve(monkeypatch, FULL)
    fetch_threads: list[int] = []
    original = remote_config.requests_request

    def recording(method, url, **kwargs):
        fetch_threads.append(threading.get_ident())
        return original(method, url, **kwargs)

    monkeypatch.setattr(remote_config, "requests_request", recording)

    assert get_config(allow_refresh=False) is None, "调用线程上不等网络"
    _wait_for(lambda: get_config(allow_refresh=False) is not None)
    assert calls["n"] == 1
    assert fetch_threads and threading.get_ident() not in fetch_threads


def test_warm_up_fetches_in_the_background(monkeypatch):
    calls = _serve(monkeypatch, FULL)
    remote_config.warm_up_in_background()
    _wait_for(lambda: calls["n"] == 1)
    assert get_config(allow_refresh=False).is_effective is True


def test_concurrent_callers_share_one_fetch(monkeypatch):
    import threading

    served = {"n": 0}
    gate = threading.Event()

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return FULL

    def slow(method, url, **kwargs):
        served["n"] += 1
        gate.wait(2)
        return _Response()

    monkeypatch.setattr(remote_config, "requests_request", slow)
    results: list = []
    workers = [threading.Thread(target=lambda: results.append(get_config())) for _ in range(5)]
    for worker in workers:
        worker.start()
    time.sleep(0.1)
    gate.set()
    for worker in workers:
        worker.join(3)
    assert served["n"] == 1
    assert len(results) == 5 and all(r is not None and r.is_effective for r in results)


def test_keeps_the_config_when_the_service_breaks_later(monkeypatch):
    _serve(monkeypatch, FULL)
    get_config()
    _serve(monkeypatch, None, fail=OSError("connection refused"))
    kept = get_config()
    assert kept is not None and kept.gateway.base_url == "https://gw.example.com"


def test_never_fetched_and_service_is_down_means_off(monkeypatch):
    _serve(monkeypatch, None, fail=OSError("connection refused"))
    assert get_config() is None


def test_first_fetch_is_retried_after_the_cooldown(monkeypatch):
    calls = _serve(monkeypatch, None, fail=OSError("connection refused"))
    assert get_config() is None
    assert get_config() is None, "冷却期内不再重试"
    assert calls["n"] == 1

    remote_config._last_failure_at = time.time() - remote_config._retry_after_s - 1
    _serve(monkeypatch, FULL)
    assert get_config() is not None, "冷却期过了要再试一次"


def test_cooldown_backs_off_while_the_service_stays_down():
    base = remote_config._RETRY_AFTER_FAILURE_S
    jitter = remote_config._RETRY_JITTER
    cap = remote_config._RETRY_BACKOFF_MAX_S

    waits = []
    for _ in range(12):
        wait, base = remote_config._backoff(base)
        waits.append(wait)

    assert waits[0] <= remote_config._RETRY_AFTER_FAILURE_S * (1 + jitter), "第一次仍是 30 秒上下"
    # 只比封顶之前的几次：到了封顶值，抖动会让后一次可能略短于前一次
    assert all(waits[index] < waits[index + 1] for index in range(4)), "连续失败要越等越久"
    assert all(wait <= cap * (1 + jitter) for wait in waits), "封顶 10 分钟"
    assert waits[-1] >= cap * (1 - jitter), "一直失败下去会走到封顶值"
    assert base == cap, "基准也停在封顶值，不会无限涨"


def test_http_error_is_treated_as_a_failure(monkeypatch):
    _serve(monkeypatch, {"is_effective": True}, status=503)
    assert get_config() is None
