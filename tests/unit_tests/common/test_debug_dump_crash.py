# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the crash exit handler that pairs with the process supervisor."""

from __future__ import annotations

import sys

import pytest

from jiuwenswarm.common import debug_dump
from jiuwenswarm.common.process_supervision import CRASH_EXIT_CODE


@pytest.fixture
def crash_env(monkeypatch, tmp_path):
    """Install the handler with a redirected logs dir and a captured hard exit."""
    import faulthandler

    exits: list[int] = []
    monkeypatch.setattr(debug_dump, "get_logs_dir", lambda: tmp_path)
    monkeypatch.setattr(debug_dump, "_crash_exit", exits.append)
    monkeypatch.setattr(faulthandler, "enable", lambda **_kwargs: None)
    # Register the current hook first so monkeypatch restores it on teardown.
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    debug_dump.install_crash_exit_handler("agentserver")
    return tmp_path, exits


def test_unhandled_exception_exits_with_the_crash_code(crash_env) -> None:
    _, exits = crash_env

    try:
        raise ValueError("boom")
    except ValueError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)

    # Exit 70, not 1: the supervisor can now tell this crash from taskkill /F
    # on Windows and respawn instead of tearing the service down.
    assert exits == [CRASH_EXIT_CODE]


def test_crash_handler_leaves_a_faulthandler_file(crash_env) -> None:
    tmp_path, _ = crash_env
    # Native crashes (access violations) kill Python hooks entirely; the
    # faulthandler dump file is the only stack trace that survives them.
    assert (tmp_path / "faulthandler-agentserver.log").exists()


def test_faulthandler_falls_back_to_stderr(monkeypatch) -> None:
    import faulthandler

    enabled: list[dict] = []

    def broken_logs_dir():
        raise OSError("logs dir unavailable")

    monkeypatch.setattr(debug_dump, "get_logs_dir", broken_logs_dir)
    monkeypatch.setattr(faulthandler, "enable", lambda **kwargs: enabled.append(kwargs))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)

    debug_dump.install_crash_exit_handler("gateway")

    assert enabled == [{}]  # plain stderr fallback, no file kwarg
