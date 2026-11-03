# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.server import app_agentserver
from jiuwenswarm.server.lifecycle import Readiness


class _FakeFront:
    def __init__(self, host: str, port: int, **_kwargs) -> None:
        self.host = host
        self.port = port
        self.readiness = Readiness()
        self.events: list[str] = []

    async def start(self) -> None:
        self.readiness.mark_transport_ready()
        self.readiness.mark_control_ready()
        self.readiness.mark_runtime_warming()
        self.events.append("front_start")

    async def stop(self) -> None:
        self.events.append("front_stop")

    def attach_runtime_backend(self, backend: object) -> None:
        self.events.append("attach")
        _ = backend


@pytest.mark.asyncio
async def test_run_does_not_delete_agent_teams_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed_paths: list[Path] = []
    fake_front = _FakeFront("127.0.0.1", 18092)
    captured: dict[str, asyncio.Event] = {}
    real_event = asyncio.Event

    def _event_factory() -> asyncio.Event:
        event = real_event()
        captured["ev"] = event
        return event

    def _fake_rmtree(path, *args, **kwargs) -> None:
        _ = args, kwargs
        removed_paths.append(Path(path))

    class _NoopSupervisor:
        async def stop(self) -> None:
            return None

    async def _fake_supervisor(front, host, port):
        _ = host, port
        front.attach_runtime_backend(_NoopSupervisor())
        captured["ev"].set()
        return _NoopSupervisor()

    monkeypatch.setattr(app_agentserver.asyncio, "Event", _event_factory)
    monkeypatch.setattr("shutil.rmtree", _fake_rmtree)
    monkeypatch.setattr(
        "jiuwenswarm.server.front.server.AgentServerFront",
        lambda host, port, **kwargs: fake_front,
    )
    monkeypatch.setattr(app_agentserver, "_start_supervisor", _fake_supervisor)
    # Logging config would import openjiuwen; this case only cares about Front/Supervisor ordering.
    monkeypatch.setattr(app_agentserver, "_configure_openjiuwen_logging", lambda: None)

    await app_agentserver._run("127.0.0.1", 18092)

    assert fake_front.events[0] == "front_start"
    assert "attach" in fake_front.events
    assert "front_stop" in fake_front.events
    assert fake_front.events.index("front_start") < fake_front.events.index("attach")
    assert removed_paths == []


def test_configure_openjiuwen_logging_uses_writable_workspace_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default log path must resolve under $JIUWENSWARM_HOME/.jiuwenswarm/logs/."""
    captured: dict[str, Any] = {}
    levels: list[tuple[str, int]] = []

    class _FakeLogger:
        def __init__(self, name: str) -> None:
            self.name = name

        def set_level(self, level: int) -> None:
            levels.append((self.name, level))

    class _FakeLogManager:
        @staticmethod
        def get_all_loggers() -> dict[str, _FakeLogger]:
            return {"common": _FakeLogger("common")}

    def _fake_configure_log_config(config: dict[str, Any]) -> None:
        captured.update(config)

    home = tmp_path / "home"
    monkeypatch.setattr("jiuwenswarm.common.utils.get_root_dir", lambda: tmp_path)
    monkeypatch.setenv("JIUWENSWARM_HOME", str(home))
    monkeypatch.setattr(
        "openjiuwen.core.common.logging.log_config.configure_log_config",
        _fake_configure_log_config,
    )
    monkeypatch.setattr(
        "openjiuwen.core.common.logging.LogManager",
        _FakeLogManager,
    )

    app_agentserver._configure_openjiuwen_logging()

    assert captured["log_path"] == f"{home}/.jiuwenswarm/logs/"
    assert captured["log_file"] == "run/jiuwen.log"
    assert levels == [("common", logging.CRITICAL)]


def test_configure_openjiuwen_logging_prefers_workspace_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    logging_yaml = config_dir / "logging.yaml"
    logging_yaml.write_text("logging: {}\n", encoding="utf-8")
    called: dict[str, str] = {}

    def _fake_configure_log(path: str) -> None:
        called["path"] = path

    monkeypatch.setattr("jiuwenswarm.common.utils.get_root_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "openjiuwen.core.common.logging.log_config.configure_log",
        _fake_configure_log,
    )

    app_agentserver._configure_openjiuwen_logging()

    assert called["path"] == str(logging_yaml)


def test_configure_openjiuwen_logging_failure_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Front must still start when the log dir is unwritable (sandbox read-only-root fallback)."""

    def _boom(config: dict[str, Any]) -> None:
        _ = config
        raise RuntimeError("Read-only file system: '/logs'")

    monkeypatch.setattr("jiuwenswarm.common.utils.get_root_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "openjiuwen.core.common.logging.log_config.configure_log_config",
        _boom,
    )

    app_agentserver._configure_openjiuwen_logging()


def test_configure_openjiuwen_logging_falls_back_when_yaml_config_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When logging.yaml exists but configure_log raises, fall back to the workspace config."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "logging.yaml").write_text("logging: {}\n", encoding="utf-8")

    captured: dict[str, Any] = {}

    def _broken_configure_log(path: str) -> None:
        _ = path
        raise RuntimeError("invalid logging.yaml")

    def _capture_configure_log_config(config: dict[str, Any]) -> None:
        captured.update(config)

    class _FakeLogger:
        def set_level(self, level: int) -> None:
            _ = level

    class _FakeLogManager:
        @staticmethod
        def get_all_loggers() -> dict[str, _FakeLogger]:
            return {"common": _FakeLogger()}

    monkeypatch.setattr("jiuwenswarm.common.utils.get_root_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "openjiuwen.core.common.logging.log_config.configure_log",
        _broken_configure_log,
    )
    monkeypatch.setattr(
        "openjiuwen.core.common.logging.log_config.configure_log_config",
        _capture_configure_log_config,
    )
    monkeypatch.setattr(
        "openjiuwen.core.common.logging.LogManager",
        _FakeLogManager,
    )

    app_agentserver._configure_openjiuwen_logging()

    assert captured.get("log_file") == "run/jiuwen.log"
    assert captured.get("log_path", "").endswith("/.jiuwenswarm/logs/")


def test_configure_openjiuwen_logging_survives_log_manager_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LogManager.get_all_loggers() raising after config must not crash Front."""

    def _fake_configure_log_config(config: dict[str, Any]) -> None:
        _ = config

    class _BrokenLogManager:
        @staticmethod
        def get_all_loggers() -> dict[str, Any]:
            raise RuntimeError("LogManager not initialized")

    monkeypatch.setattr("jiuwenswarm.common.utils.get_root_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "openjiuwen.core.common.logging.log_config.configure_log_config",
        _fake_configure_log_config,
    )
    monkeypatch.setattr(
        "openjiuwen.core.common.logging.LogManager",
        _BrokenLogManager,
    )

    app_agentserver._configure_openjiuwen_logging()
