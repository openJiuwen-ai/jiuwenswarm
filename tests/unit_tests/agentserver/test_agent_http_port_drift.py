# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AgentHTTPServer 端口预检与漂移策略。"""

from __future__ import annotations

import errno
import socket
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.server import agent_http_server as http_mod
from jiuwenswarm.server.agent_http_server import (
    AgentHTTPServer,
    http_port_drift_forbidden,
    is_port_available,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestIsPortAvailable:
    def test_free_port_is_available(self):
        assert is_port_available("127.0.0.1", _free_port()) is True

    def test_live_listener_is_occupied(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        try:
            assert is_port_available("127.0.0.1", port) is False
        finally:
            listener.close()

    def test_linux_reuseaddr_retry_treats_lingering_as_free(self, monkeypatch):
        """裸 bind EADDRINUSE 后，SO_REUSEADDR 复探成功 → 视为可用（TIME_WAIT）。"""

        class FakeSock:
            def bind(self, *_a, **_k):
                raise OSError(errno.EADDRINUSE, "Address already in use")

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        import socket as socket_mod

        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr(socket_mod, "socket", lambda *_a, **_k: FakeSock())
        monkeypatch.setattr(http_mod, "_reuseaddr_bind_probe", lambda host, port: True)
        assert is_port_available("0.0.0.0", 8766) is True

    def test_windows_skips_reuseaddr_retry(self, monkeypatch):
        class FakeSock:
            def bind(self, *_a, **_k):
                raise OSError(errno.EADDRINUSE, "Address already in use")

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        import socket as socket_mod

        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.setattr(socket_mod, "socket", lambda *_a, **_k: FakeSock())
        reused = {"called": False}

        def boom(*_a, **_k):
            reused["called"] = True
            return True

        monkeypatch.setattr(http_mod, "_reuseaddr_bind_probe", boom)
        assert is_port_available("127.0.0.1", 8766) is False
        assert reused["called"] is False

    def test_darwin_skips_reuseaddr_retry(self, monkeypatch):
        """macOS/BSD：SO_REUSEADDR 可叠活 listener，不得复探。"""

        class FakeSock:
            def bind(self, *_a, **_k):
                raise OSError(errno.EADDRINUSE, "Address already in use")

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        import socket as socket_mod

        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr(socket_mod, "socket", lambda *_a, **_k: FakeSock())
        reused = {"called": False}

        def boom(*_a, **_k):
            reused["called"] = True
            return True

        monkeypatch.setattr(http_mod, "_reuseaddr_bind_probe", boom)
        assert is_port_available("127.0.0.1", 8766) is False
        assert reused["called"] is False


class TestHttpPortDriftForbidden:
    def test_env_true_forbids(self, monkeypatch):
        monkeypatch.setenv("AGENT_HTTP_NO_PORT_DRIFT", "1")
        monkeypatch.setattr(http_mod, "is_enterprise", lambda: False)
        assert http_port_drift_forbidden() is True

    def test_env_false_allows_even_on_enterprise(self, monkeypatch):
        monkeypatch.setenv("AGENT_HTTP_NO_PORT_DRIFT", "0")
        monkeypatch.setattr(http_mod, "is_enterprise", lambda: True)
        assert http_port_drift_forbidden() is False

    def test_garbage_env_falls_back_to_enterprise_default(self, monkeypatch):
        """非法取值不得 fail-open 成允许漂移。"""
        monkeypatch.setenv("AGENT_HTTP_NO_PORT_DRIFT", "ture")
        monkeypatch.setattr(http_mod, "is_enterprise", lambda: True)
        assert http_port_drift_forbidden() is True

    def test_garbage_env_falls_back_to_personal_default(self, monkeypatch):
        monkeypatch.setenv("AGENT_HTTP_NO_PORT_DRIFT", "maybe")
        monkeypatch.setattr(http_mod, "is_enterprise", lambda: False)
        assert http_port_drift_forbidden() is False

    def test_enterprise_default_forbids(self, monkeypatch):
        monkeypatch.delenv("AGENT_HTTP_NO_PORT_DRIFT", raising=False)
        monkeypatch.setattr(http_mod, "is_enterprise", lambda: True)
        assert http_port_drift_forbidden() is True

    def test_personal_default_allows(self, monkeypatch):
        monkeypatch.delenv("AGENT_HTTP_NO_PORT_DRIFT", raising=False)
        monkeypatch.setattr(http_mod, "is_enterprise", lambda: False)
        assert http_port_drift_forbidden() is False


@pytest.mark.asyncio
async def test_enterprise_refuses_drift_when_port_busy(monkeypatch):
    monkeypatch.setattr(http_mod, "http_port_drift_forbidden", lambda: True)
    monkeypatch.setattr(http_mod, "is_port_available", lambda host, port: False)

    class FakeMTLS:
        enforced = False

        class mode:
            value = "off"

        def uvicorn_ssl_kwargs(self):
            return {}

    monkeypatch.setattr(
        "jiuwenswarm.common.security.link_mtls.LinkMTLSConfig.from_env",
        classmethod(lambda cls, **kwargs: FakeMTLS()),
    )

    server = AgentHTTPServer(MagicMock(), host="127.0.0.1", port=8766)
    assert await server.start() is False
    assert server.port == 8766
    assert server._server is None


@pytest.mark.asyncio
async def test_personal_drifts_to_plus_1000(monkeypatch):
    monkeypatch.setattr(http_mod, "http_port_drift_forbidden", lambda: False)

    def avail(host, port):
        return port != 8766

    monkeypatch.setattr(http_mod, "is_port_available", avail)

    class FakeMTLS:
        enforced = False

        class mode:
            value = "off"

        def uvicorn_ssl_kwargs(self):
            return {}

    monkeypatch.setattr(
        "jiuwenswarm.common.security.link_mtls.LinkMTLSConfig.from_env",
        classmethod(lambda cls, **kwargs: FakeMTLS()),
    )

    # 避免真启 uvicorn：预检选口后，让 started 立刻为 True 并短路 serve
    started = {"port": None}

    class FakeServer:
        def __init__(self, config):
            self.started = False
            self.should_exit = False
            started["port"] = config.port

        async def serve(self):
            self.started = True
            while not self.should_exit:
                import asyncio

                await asyncio.sleep(0.01)

    monkeypatch.setattr("uvicorn.Server", FakeServer)
    monkeypatch.setattr(
        AgentHTTPServer,
        "build_app",
        lambda self: MagicMock(),
    )

    server = AgentHTTPServer(MagicMock(), host="127.0.0.1", port=8766)
    assert await server.start() is True
    assert server.port == 9766
    assert started["port"] == 9766
    await server.stop(timeout=1)
