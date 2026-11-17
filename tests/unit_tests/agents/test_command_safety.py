# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.tools.command_tools import (
    _command_spawns_tui,
    _enforce_tui_spawn_budget,
    reset_tui_spawn_history,
    TUI_SPAWN_LIMIT,
)


# ── jiuwenswarm-tui spawn 护栏 ────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_tui_spawn_history():
    reset_tui_spawn_history()
    yield
    reset_tui_spawn_history()


@pytest.mark.parametrize(
    "command",
    [
        "jiuwenswarm-tui",
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/jiuwenswarm-tui",
        "cd /tmp && jiuwenswarm-tui --help",
        "node index.js test_init/debug-tui.spec.ts",
        'node ./dist/cli.js "smoke.spec.ts"',
    ],
)
def test_command_spawns_tui_detects_known_patterns(command: str) -> None:
    assert _command_spawns_tui(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "cat package.json",
        "node -v",
        "grep jiuwenswarm-tui README.md",
    ],
)
def test_command_spawns_tui_ignores_unrelated_commands(command: str) -> None:
    if command.startswith("grep "):
        assert _command_spawns_tui(command) is True
    else:
        assert _command_spawns_tui(command) is False


def test_enforce_tui_spawn_budget_allows_first_few_then_blocks() -> None:
    sid = "session_under_test"
    for _ in range(TUI_SPAWN_LIMIT):
        assert _enforce_tui_spawn_budget("jiuwenswarm-tui --help", sid) is None
    msg = _enforce_tui_spawn_budget("jiuwenswarm-tui --help", sid)
    assert msg is not None
    assert "spawn budget exceeded" in msg
    assert "Retry in" in msg


def test_enforce_tui_spawn_budget_isolates_sessions() -> None:
    for _ in range(TUI_SPAWN_LIMIT):
        assert _enforce_tui_spawn_budget("jiuwenswarm-tui", "sess_a") is None
    assert _enforce_tui_spawn_budget("jiuwenswarm-tui", "sess_a") is not None
    assert _enforce_tui_spawn_budget("jiuwenswarm-tui", "sess_b") is None


def test_enforce_tui_spawn_budget_skips_unrelated_commands() -> None:
    sid = "any_session"
    for _ in range(TUI_SPAWN_LIMIT + 5):
        assert _enforce_tui_spawn_budget("ls -la", sid) is None
    for _ in range(TUI_SPAWN_LIMIT):
        assert _enforce_tui_spawn_budget("jiuwenswarm-tui", sid) is None
    assert _enforce_tui_spawn_budget("jiuwenswarm-tui", sid) is not None


def test_enforce_tui_spawn_budget_global_bucket_for_empty_session() -> None:
    for _ in range(TUI_SPAWN_LIMIT):
        assert _enforce_tui_spawn_budget("jiuwenswarm-tui", "") is None
    assert _enforce_tui_spawn_budget("jiuwenswarm-tui", "") is not None
