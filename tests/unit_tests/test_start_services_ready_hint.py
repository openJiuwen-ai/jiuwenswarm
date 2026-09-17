# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Unit tests for jiuwenswarm-start ready / access-url hints (issue #1059)."""

from __future__ import annotations

import logging
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from jiuwenswarm.start_services import (
    _resolve_runtime_ports,
    _start_process,
    _wait_for_services_ready,
)


def _open_ports(*open_ports: int):
    """Patch socket so only the given ports accept connections."""
    open_set = set(open_ports)

    class _Sock:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def settimeout(self, _t):
            return None

        def connect(self, addr):
            _host, port = addr[0], addr[1]
            if port in open_set:
                return None
            raise OSError("refused")

    return patch("socket.socket", side_effect=lambda *a, **k: _Sock())


def test_resolve_runtime_ports_defaults(monkeypatch: pytest.MonkeyPatch):
    for key in ("AGENT_SERVER_PORT", "WEB_PORT", "GATEWAY_PORT", "FRONTEND_PORT"):
        monkeypatch.delenv(key, raising=False)
    ports = _resolve_runtime_ports()
    assert ports["frontend"] == 5173
    assert ports["gateway"] == 19001
    assert ports["web"] == 19000
    assert ports["agent_server"] == 18092


def test_resolve_runtime_ports_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FRONTEND_PORT", "6173")
    monkeypatch.setenv("GATEWAY_PORT", "29001")
    ports = _resolve_runtime_ports()
    assert ports["frontend"] == 6173
    assert ports["gateway"] == 29001


def test_resolve_runtime_ports_invalid_env_keeps_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FRONTEND_PORT", "not-a-port")
    monkeypatch.setenv("GATEWAY_PORT", "12.5")
    ports = _resolve_runtime_ports()
    assert ports["frontend"] == 5173
    assert ports["gateway"] == 19001


def test_resolve_runtime_ports_out_of_range_env_keeps_default(
    monkeypatch: pytest.MonkeyPatch,
):
    """PR #3936 review: reject ports outside 1–65535 to avoid OverflowError."""
    monkeypatch.setenv("FRONTEND_PORT", "-1")
    monkeypatch.setenv("AGENT_SERVER_PORT", "99999")
    monkeypatch.setenv("WEB_PORT", "0")
    ports = _resolve_runtime_ports()
    assert ports["frontend"] == 5173
    assert ports["agent_server"] == 18092
    assert ports["web"] == 19000


def test_start_process_passes_resolved_ports_to_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    from jiuwenswarm.dotenv_early import CLI_PORTS_ENV_FLAG

    captured: dict[str, object] = {}

    def fake_popen(cmd, *, cwd, env):
        captured.update({"cmd": cmd, "cwd": cwd, "env": env})
        return MagicMock()

    monkeypatch.setattr("jiuwenswarm.start_services.subprocess.Popen", fake_popen)
    monkeypatch.setenv("AGENT_SERVER_URL", "ws://127.0.0.1:18092")
    ports = {
        "agent_server": 19092,
        "web": 20000,
        "gateway": 20001,
        "frontend": 6173,
    }

    _start_process("web-dev", ["npm", "run", "dev"], tmp_path, ports=ports)

    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert child_env[CLI_PORTS_ENV_FLAG] == "1"
    assert child_env["AGENT_SERVER_PORT"] == "19092"
    assert child_env["AGENT_PORT"] == "19092"
    assert child_env["WEB_PORT"] == "20000"
    assert child_env["GATEWAY_PORT"] == "20001"
    assert child_env["FRONTEND_PORT"] == "6173"
    # Stale URL from parent shell must not override the remapped agent port.
    assert "AGENT_SERVER_URL" not in child_env


def test_wait_for_services_ready_prints_access_url_for_web_dev(caplog: pytest.LogCaptureFixture):
    frontend = MagicMock()
    frontend.poll.return_value = None
    ports = {
        "agent_server": 18092,
        "web": 19000,
        "gateway": 19001,
        "frontend": 5173,
    }
    processes = {"web-dev": frontend}

    with _open_ports(5173):
        with caplog.at_level(logging.INFO):
            _wait_for_services_ready(ports, processes, overall_timeout=2.0)

    joined = "\n".join(caplog.messages)
    assert "服务已启动，端口信息如下：" in joined
    assert "✓ Web UI" in joined
    assert "http://localhost:5173" in joined
    assert "Web UI ready (port 5173)" in joined


def test_wait_for_services_ready_prints_full_port_summary(caplog: pytest.LogCaptureFixture):
    app = MagicMock()
    app.poll.return_value = None
    frontend = MagicMock()
    frontend.poll.return_value = None
    ports = {
        "agent_server": 18092,
        "web": 19000,
        "gateway": 19001,
        "frontend": 5173,
    }
    processes = {"app": app, "web-dev": frontend}

    with _open_ports(5173, 18092, 19000, 19001):
        with caplog.at_level(logging.INFO):
            _wait_for_services_ready(ports, processes, overall_timeout=2.0)

    joined = "\n".join(caplog.messages)
    assert "服务已启动，端口信息如下：" in joined
    assert "✓ Web UI" in joined
    assert "✓ AgentServer WebSocket" in joined
    assert "✓ Gateway HTTP" in joined
    assert "✓ WebChannel WebSocket" in joined
    assert "http://localhost:5173" in joined
    assert "http://localhost:19001" in joined
    assert "ws://localhost:19000/ws" in joined
    assert "ws://localhost:18092" in joined


def test_port_open_tries_ipv6_when_ipv4_fails():
    """Vite on Windows may listen on ::1 only; probe must still succeed."""
    frontend = MagicMock()
    frontend.poll.return_value = None
    ports = {"frontend": 5173}
    processes = {"web-dev": frontend}

    calls: list[tuple[str, int]] = []

    class _Sock:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def settimeout(self, _t):
            return None

        def connect(self, addr):
            host, port = addr[0], addr[1]
            calls.append((host, port))
            if host == "127.0.0.1":
                raise OSError("refused")
            if host == "::1":
                return None
            raise OSError("unexpected")

    with patch("socket.socket", side_effect=lambda *a, **k: _Sock()):
        with patch("logging.Logger.info"):
            _wait_for_services_ready(ports, processes, overall_timeout=2.0)

    assert ("127.0.0.1", 5173) in calls
    assert ("::1", 5173) in calls


def test_port_open_falls_back_when_ipv6_unavailable(caplog: pytest.LogCaptureFixture):
    """PR #3936 P3: IPv6 socket creation failure must not block IPv4 success."""
    import socket

    frontend = MagicMock()
    frontend.poll.return_value = None
    ports = {"frontend": 5173}
    processes = {"web-dev": frontend}

    class _Sock:
        def __init__(self, family, *_a, **_k):
            if family == socket.AF_INET6:
                raise OSError("Address family not supported")
            self.family = family

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def settimeout(self, _t):
            return None

        def connect(self, addr):
            host, _port = addr[0], addr[1]
            if host == "127.0.0.1":
                return None
            raise OSError("unexpected")

    with patch("socket.socket", side_effect=lambda *a, **k: _Sock(*a, **k)):
        with caplog.at_level(logging.INFO):
            _wait_for_services_ready(ports, processes, overall_timeout=2.0)

    assert "✓ Web UI" in "\n".join(caplog.messages)


def test_web_ui_banner_prints_before_backends_ready(caplog: pytest.LogCaptureFixture):
    """CR-001: Web UI URL banner must appear as soon as frontend is reachable."""
    app = MagicMock()
    app.poll.return_value = None
    frontend = MagicMock()
    frontend.poll.return_value = None
    ports = {
        "agent_server": 18092,
        "web": 19000,
        "gateway": 19001,
        "frontend": 5173,
    }
    processes = {"app": app, "web-dev": frontend}

    open_set = {5173}

    class _Sock:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def settimeout(self, _t):
            return None

        def connect(self, addr):
            _host, port = addr[0], addr[1]
            if port in open_set:
                return None
            raise OSError("refused")

    def _sleep(_seconds: float) -> None:
        # After the first wait tick, backends come up (Web UI already bannered).
        open_set.update({18092, 19000, 19001})

    with patch("socket.socket", side_effect=lambda *a, **k: _Sock()):
        with patch("jiuwenswarm.start_services.time.sleep", side_effect=_sleep):
            with caplog.at_level(logging.INFO):
                _wait_for_services_ready(ports, processes, overall_timeout=3.0)

    msgs = list(caplog.messages)
    early_idx = next(i for i, m in enumerate(msgs) if "服务启动中，端口信息如下：" in m)
    assert "http://localhost:5173" in "\n".join(msgs[early_idx : early_idx + 8])
    # Final refresh should flip to fully started once backends catch up.
    assert any("服务已启动，端口信息如下：" in m for m in msgs[early_idx + 1 :])


def test_timeout_shows_ellipsis_and_starting_copy(caplog: pytest.LogCaptureFixture):
    """CR-003/CR-004: unpaid ports keep … and '启动中' wording."""
    app = MagicMock()
    app.poll.return_value = None
    ports = {
        "agent_server": 18092,
        "web": 19000,
        "gateway": 19001,
        "frontend": 5173,
    }
    processes = {"app": app}

    with _open_ports():
        with caplog.at_level(logging.INFO):
            _wait_for_services_ready(ports, processes, overall_timeout=0.5)

    joined = "\n".join(caplog.messages)
    assert "服务启动中，端口信息如下：" in joined
    assert "…" in joined
    assert "starting..." in joined
    assert "服务已启动，端口信息如下：" not in joined


def test_process_death_ends_wait_early(caplog: pytest.LogCaptureFixture):
    """CR-002: crashed subprocess must not burn the full overall_timeout."""
    app = MagicMock()
    # Target build sees alive; wait-loop death check sees exit.
    app.poll.side_effect = [None, 1] + [1] * 20
    frontend = MagicMock()
    frontend.poll.return_value = None
    ports = {
        "agent_server": 18092,
        "web": 19000,
        "gateway": 19001,
        "frontend": 5173,
    }
    processes = {"app": app, "web-dev": frontend}

    started = time.monotonic()
    with _open_ports():
        with caplog.at_level(logging.INFO):
            _wait_for_services_ready(ports, processes, overall_timeout=30.0)
    elapsed = time.monotonic() - started

    joined = "\n".join(caplog.messages)
    assert "required process exited during startup wait" in joined
    assert elapsed < 3.0
    assert "服务启动中，端口信息如下：" in joined


def test_cli_injected_ports_survive_stale_dotenv_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """Issue #2749: banner ports and Gateway bind ports must stay aligned.

    jiuwenswarm-start injects index-0 defaults into the child env and prints
    them in the access banner, but app/gateway historically re-loaded a stale
    .env (GATEWAY_PORT=20001 from a prior fallback) with override=True and
    rebound to 20001. The CLI_PORTS flag must keep the injected group.
    """
    from jiuwenswarm.dotenv_early import CLI_PORTS_ENV_FLAG, load_dotenv_runtime

    captured: dict[str, object] = {}

    def fake_popen(cmd, *, cwd, env):
        captured["env"] = env
        return MagicMock()

    monkeypatch.setattr("jiuwenswarm.start_services.subprocess.Popen", fake_popen)

    banner_ports = {
        "agent_server": 18092,
        "web": 19000,
        "gateway": 19001,
        "frontend": 5173,
    }
    _start_process("app", ["python", "-m", "jiuwenswarm.app"], tmp_path, ports=banner_ports)

    child_env = captured["env"]
    assert isinstance(child_env, dict)

    # Simulate the child process environment after spawn.
    monkeypatch.delenv("JIUWENSWARM_DESKTOP", raising=False)
    for key, value in child_env.items():
        monkeypatch.setenv(key, value)

    stale_env = tmp_path / ".env"
    stale_env.write_text(
        "AGENT_SERVER_PORT=19092\n"
        "WEB_PORT=20000\n"
        "GATEWAY_PORT=20001\n"
        "FRONTEND_PORT=6173\n",
        encoding="utf-8",
    )

    load_dotenv_runtime(stale_env, override=True)

    assert os.environ.get(CLI_PORTS_ENV_FLAG) == "1"
    assert os.environ["GATEWAY_PORT"] == "19001"
    assert os.environ["WEB_PORT"] == "19000"
    assert os.environ["AGENT_SERVER_PORT"] == "18092"
    assert os.environ["FRONTEND_PORT"] == "5173"
