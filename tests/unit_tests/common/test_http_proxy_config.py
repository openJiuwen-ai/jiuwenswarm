# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jiuwenswarm.common.http_proxy_config import (
    read_no_proxy_list,
    read_proxy_url,
    resolve_requests_proxies,
    resolve_requests_verify,
    requests_get,
    should_bypass_proxy,
    ssl_verify_enabled,
)
import jiuwenswarm.common.http_proxy_config as proxy_cfg
from jiuwenswarm.common.local_env_config import (
    bind_task_env_overlay,
    reset_local_env_state_for_tests,
    reset_task_env_overlay,
    set_os_environ,
)


@pytest.fixture(autouse=True)
def _reset_env_state():
    saved_environ = dict(os.environ)
    reset_local_env_state_for_tests()
    # Session 复用引入了模块级缓存：必须在用例间重置，
    # 否则 monkeypatch 替换的 Session 替身不会生效（缓存跨用例存活）。
    proxy_cfg.reset_shared_session()
    yield
    proxy_cfg.reset_shared_session()
    reset_local_env_state_for_tests()
    os.environ.clear()
    os.environ.update(saved_environ)


@pytest.fixture(autouse=True)
def _clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "REQUESTS_CA_BUNDLE",
        "JIUWENCLAW_SSL_VERIFY",
        "JIUWENSWARM_SSL_VERIFY",
        "default__default__HTTP_PROXY",
        "default__default__HTTPS_PROXY",
        "default__default__NO_PROXY",
    ):
        monkeypatch.delenv(key, raising=False)


def test_read_proxy_url_prefers_overlay() -> None:
    token = bind_task_env_overlay({"HTTPS_PROXY": "http://overlay:3128"})
    try:
        assert read_proxy_url() == "http://overlay:3128"
    finally:
        reset_task_env_overlay(token)


def test_read_proxy_url_reads_namespaced_tip() -> None:
    set_os_environ(
        "HTTP_PROXY",
        "http://tenant:8080",
        service_id="default",
        agent_id="default",
    )
    assert read_proxy_url() == "http://tenant:8080"


def test_read_proxy_url_falls_back_to_bare_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP_PROXY may only be in process env when tip is empty."""
    monkeypatch.setenv("HTTP_PROXY", "http://spawn:8888")
    assert read_proxy_url() == "http://spawn:8888"
    assert resolve_requests_proxies("https://api.example.com") == {
        "http": "http://spawn:8888",
        "https": "http://spawn:8888",
    }


def test_tip_proxy_wins_over_bare_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://bare:9999")
    set_os_environ("HTTP_PROXY", "http://tip:7777")
    assert read_proxy_url() == "http://tip:7777"


def test_should_bypass_proxy_honors_overlay_no_proxy() -> None:
    token = bind_task_env_overlay(
        {
            "HTTPS_PROXY": "http://proxy:8080",
            "NO_PROXY": "example.com",
        }
    )
    try:
        assert should_bypass_proxy("https://example.com/path") is True
        assert should_bypass_proxy("https://other.com/path") is False
        assert resolve_requests_proxies("https://other.com") == {
            "http": "http://proxy:8080",
            "https": "http://proxy:8080",
        }
        assert resolve_requests_proxies("https://example.com") is None
    finally:
        reset_task_env_overlay(token)


def test_no_proxy_falls_back_to_bare_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://spawn:8888")
    monkeypatch.setenv("NO_PROXY", "internal.local")
    assert "internal.local" in read_no_proxy_list()
    assert should_bypass_proxy("https://internal.local/x") is True
    assert resolve_requests_proxies("https://internal.local/x") is None


def test_requests_request_prefers_overlay_over_bare_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://bare:9999")

    calls: list[dict] = []

    class _FakeSession:
        trust_env = True

        def request(self, method: str, url: str, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})

            class _Resp:
                status_code = 200

            return _Resp()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(proxy_cfg.requests, "Session", lambda: _FakeSession())

    token = bind_task_env_overlay({"HTTPS_PROXY": "http://overlay:3128"})
    try:
        requests_get("https://api.example.com/v1")
    finally:
        reset_task_env_overlay(token)

    assert calls
    assert calls[0]["proxies"] == {
        "http": "http://overlay:3128",
        "https": "http://overlay:3128",
    }


def test_requests_request_uses_spawn_proxy_when_tip_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://spawn:8888")

    calls: list[dict] = []

    class _FakeSession:
        trust_env = True

        def request(self, method: str, url: str, **kwargs):
            calls.append(kwargs)

            class _Resp:
                status_code = 200

            return _Resp()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(proxy_cfg.requests, "Session", lambda: _FakeSession())
    requests_get("https://api.example.com/v1")
    assert calls[0]["proxies"] == {
        "http": "http://spawn:8888",
        "https": "http://spawn:8888",
    }


def test_resolve_requests_verify_returns_ca_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ca = tmp_path / "cacert-bundle.pem"
    ca.write_text("dummy-ca\n", encoding="utf-8")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca))
    monkeypatch.delenv("JIUWENCLAW_SSL_VERIFY", raising=False)
    monkeypatch.delenv("JIUWENSWARM_SSL_VERIFY", raising=False)
    assert resolve_requests_verify() == str(ca)


def test_resolve_requests_verify_false_skips_ca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ca = tmp_path / "cacert-bundle.pem"
    ca.write_text("dummy-ca\n", encoding="utf-8")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca))
    monkeypatch.setenv("JIUWENCLAW_SSL_VERIFY", "false")
    assert ssl_verify_enabled() is False
    assert resolve_requests_verify() is False


def test_resolve_requests_verify_reads_ssl_flag_from_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENCLAW_SSL_VERIFY", "false")
    assert ssl_verify_enabled() is False
    assert resolve_requests_verify() is False


def test_prepare_requests_kwargs_keeps_caller_proxies() -> None:
    token = bind_task_env_overlay({"HTTPS_PROXY": "http://overlay:3128"})
    try:
        out = proxy_cfg.prepare_requests_kwargs(
            "https://api.example.com",
            {"proxies": {"http": "http://free-search:1", "https": "http://free-search:1"}},
        )
        assert out["proxies"]["http"] == "http://free-search:1"
    finally:
        reset_task_env_overlay(token)
