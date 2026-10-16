# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Finding 1 (CWE-306): refuse an unauthenticated non-loopback HTTP bind."""

from __future__ import annotations

import pytest
import uvicorn

from jiuwenbox.server import launcher


@pytest.fixture
def stub_uvicorn(monkeypatch):
    """Stub out ``uvicorn.run`` so ``main()`` never actually binds a socket.

    ``main()`` imports ``uvicorn`` lazily inside itself and also needs the
    real package for ``logging_config.patch_uvicorn_logging()``'s
    ``uvicorn.config`` submodule, so only ``run`` is replaced, not the whole
    module.
    """
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    return calls


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        launcher.ENV_LISTEN,
        launcher._ENV_API_TOKEN_NAME,
        launcher.ENV_ALLOW_INSECURE_NETWORK_BIND,
        launcher.ENV_UDS_PATH,
        launcher.ENV_SAVE_LOGS_DIR,
    ):
        monkeypatch.delenv(var, raising=False)


def test_is_loopback_host():
    assert launcher._is_loopback_host("127.0.0.1")
    assert launcher._is_loopback_host("localhost")
    assert launcher._is_loopback_host("::1")
    assert launcher._is_loopback_host("LOCALHOST")
    assert not launcher._is_loopback_host("0.0.0.0")
    assert not launcher._is_loopback_host("10.0.0.5")


def test_non_loopback_bind_without_token_is_refused(stub_uvicorn, caplog):
    """Finding 1 reproduction: the vulnerable default was silent success."""
    called = stub_uvicorn
    rc = launcher.main(["--listen", "http://0.0.0.0:8321"])
    assert rc == 2
    assert not called, "server must not start on an open, unauthenticated bind"
    assert "refusing to bind" in caplog.text


def test_non_loopback_bind_with_token_starts(monkeypatch, stub_uvicorn):
    called = stub_uvicorn
    rc = launcher.main(["--listen", "http://0.0.0.0:8321", "--api-token", "s3cret"])
    assert rc == 0
    assert called


def test_loopback_bind_without_token_starts(monkeypatch, stub_uvicorn):
    called = stub_uvicorn
    rc = launcher.main(["--listen", "http://127.0.0.1:8321"])
    assert rc == 0
    assert called


def test_allow_insecure_network_bind_flag_overrides(monkeypatch, stub_uvicorn):
    called = stub_uvicorn
    rc = launcher.main(["--listen", "http://0.0.0.0:8321", "--allow-insecure-network-bind"])
    assert rc == 0
    assert called


def test_allow_insecure_network_bind_env_overrides(monkeypatch, stub_uvicorn):
    called = stub_uvicorn
    monkeypatch.setenv(launcher.ENV_ALLOW_INSECURE_NETWORK_BIND, "1")
    rc = launcher.main(["--listen", "http://0.0.0.0:8321"])
    assert rc == 0
    assert called


def test_uds_listen_is_unaffected_by_the_token_check(monkeypatch, stub_uvicorn, tmp_path):
    called = stub_uvicorn
    sock_path = tmp_path / "jiuwenbox.sock"
    rc = launcher.main(["--listen", f"unix://{sock_path}"])
    assert rc == 0
    assert called
