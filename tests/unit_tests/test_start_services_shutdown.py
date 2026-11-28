# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Graceful shutdown ordering in the service launcher."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from jiuwenswarm import start_services


def test_terminate_processes_returns_when_children_already_exited(monkeypatch) -> None:
    monkeypatch.setattr(start_services, "_GRACEFUL_STOP_SECONDS", 0.0)
    proc = MagicMock()
    proc.poll.return_value = 0

    start_services._terminate_processes({"app": proc})

    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()


def test_terminate_processes_escalates_only_after_grace(monkeypatch) -> None:
    monkeypatch.setattr(start_services, "_GRACEFUL_STOP_SECONDS", 0.0)
    monkeypatch.setattr(start_services, "_TERMINATE_WAIT_SECONDS", 0.0)
    proc = MagicMock()
    # Grace check, terminate decision, then exit after SIGTERM.
    proc.poll.side_effect = [None, None, 0]

    start_services._terminate_processes({"app": proc})

    proc.terminate.assert_called_once()
    proc.kill.assert_not_called()


def test_terminate_processes_kills_when_sigterm_is_ignored(monkeypatch) -> None:
    monkeypatch.setattr(start_services, "_GRACEFUL_STOP_SECONDS", 0.0)
    monkeypatch.setattr(start_services, "_TERMINATE_WAIT_SECONDS", 0.0)
    proc = MagicMock()
    proc.poll.return_value = None

    start_services._terminate_processes({"app": proc})

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()


def test_terminate_processes_stops_batch_service_tree(monkeypatch) -> None:
    monkeypatch.setattr(start_services, "_GRACEFUL_STOP_SECONDS", 0.0)
    monkeypatch.setattr(start_services, "_TERMINATE_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(
        start_services, "_is_batch_service", lambda name: name == "web-dev"
    )
    killed: list[int] = []
    monkeypatch.setattr(start_services, "_kill_process_tree", killed.append)
    app = MagicMock()
    app.poll.return_value = 0
    web_dev = MagicMock()
    web_dev.poll.return_value = None

    start_services._terminate_processes({"app": app, "web-dev": web_dev})

    app.terminate.assert_not_called()
    web_dev.terminate.assert_not_called()
    assert killed == [web_dev.pid]


def test_start_process_isolates_batch_service_from_console_ctrl_c(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        start_services, "_is_batch_service", lambda name: name == "web-dev"
    )
    captured: dict = {}

    def _popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(start_services.subprocess, "Popen", _popen)

    start_services._start_process(
        "web-dev", ["cmd", "/c", "npm", "run", "dev"], Path(".")
    )

    assert captured["creationflags"] == getattr(
        start_services.subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
