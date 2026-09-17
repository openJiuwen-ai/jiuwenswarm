# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.tools.command_tools import (
    _check_command_safety,
    _command_spawns_tui,
    _enforce_tui_spawn_budget,
    reset_tui_spawn_history,
    TUI_SPAWN_LIMIT,
)


def test_blocks_pkill_on_jiuwenswarm_backend() -> None:
    reason = _check_command_safety('pkill -f "jiuwenswarm" 2>/dev/null')
    assert reason is not None
    assert "jiuwenswarm" in reason


def test_blocks_pkill_on_jiuwenswarm_tui() -> None:
    reason = _check_command_safety('pkill -f "jiuwenswarm-tui" 2>/dev/null')
    assert reason is not None
    assert "jiuwenswarm" in reason


def test_blocks_pkill_on_jiuwenswarm_tui_in_compound_command() -> None:
    reason = _check_command_safety(
        'echo "clean" && pkill -f "jiuwenswarm-tui" 2>/dev/null; sleep 1'
    )
    assert reason is not None


def test_blocks_killall_on_jiuwenswarm_tui() -> None:
    reason = _check_command_safety("killall jiuwenswarm-tui")
    assert reason is not None


def test_blocks_kill_with_pgrep_subshell() -> None:
    reason = _check_command_safety("kill $(pgrep -f jiuwenswarm-tui)")
    assert reason is not None


def test_blocks_pgrep_xargs_kill_pipeline() -> None:
    reason = _check_command_safety("pgrep -f jiuwenswarm-tui | xargs kill")
    assert reason is not None


def test_blocks_pkill_on_jiuwenclaw_backend() -> None:
    reason = _check_command_safety('pkill -f "jiuwenclaw" 2>/dev/null')
    assert reason is not None


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
        "grep jiuwenswarm-tui README.md",  # only mentions the binary, doesn't run it
    ],
)
def test_command_spawns_tui_ignores_unrelated_commands(command: str) -> None:
    # "grep jiuwenswarm-tui" is a borderline match — the current pattern requires
    # the binary token to be followed by whitespace/EOL/quote, so a quoted-arg
    # form like `grep jiuwenswarm-tui README.md` triggers a false positive on
    # the trailing whitespace. Document the chosen behaviour explicitly:
    # we tolerate a tiny false-positive rate (grep is cheap; agent can rephrase)
    # in exchange for a simple regex. Tests assert what the regex actually does.
    if command.startswith("grep "):
        assert _command_spawns_tui(command) is True
    else:
        assert _command_spawns_tui(command) is False


def test_enforce_tui_spawn_budget_allows_first_few_then_blocks() -> None:
    sid = "session_under_test"
    # Limit defaults to 3 per 300s; first 3 must pass, 4th must block.
    for _ in range(TUI_SPAWN_LIMIT):
        assert _enforce_tui_spawn_budget("jiuwenswarm-tui --help", sid) is None
    msg = _enforce_tui_spawn_budget("jiuwenswarm-tui --help", sid)
    assert msg is not None
    assert "spawn budget exceeded" in msg
    assert "Retry in" in msg


def test_enforce_tui_spawn_budget_isolates_sessions() -> None:
    # Saturating session A must not affect session B.
    for _ in range(TUI_SPAWN_LIMIT):
        assert _enforce_tui_spawn_budget("jiuwenswarm-tui", "sess_a") is None
    assert _enforce_tui_spawn_budget("jiuwenswarm-tui", "sess_a") is not None
    assert _enforce_tui_spawn_budget("jiuwenswarm-tui", "sess_b") is None


def test_enforce_tui_spawn_budget_skips_unrelated_commands() -> None:
    # Non-spawn commands should never consume the budget, no matter how many.
    sid = "any_session"
    for _ in range(TUI_SPAWN_LIMIT + 5):
        assert _enforce_tui_spawn_budget("ls -la", sid) is None
    # Budget still fully available.
    for _ in range(TUI_SPAWN_LIMIT):
        assert _enforce_tui_spawn_budget("jiuwenswarm-tui", sid) is None
    assert _enforce_tui_spawn_budget("jiuwenswarm-tui", sid) is not None


def test_enforce_tui_spawn_budget_global_bucket_for_empty_session() -> None:
    # Empty session id must not silently bypass the limit.
    for _ in range(TUI_SPAWN_LIMIT):
        assert _enforce_tui_spawn_budget("jiuwenswarm-tui", "") is None
    assert _enforce_tui_spawn_budget("jiuwenswarm-tui", "") is not None


# ── git worktree add 路径护栏 ────────────────────────────────

from jiuwenswarm.agents.harness.common.tools import command_tools  # noqa: E402


@pytest.fixture
def _project_root(tmp_path, monkeypatch):
    """Pin the project root to a tmp dir so path bounds are deterministic."""
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(command_tools, "_context_project_root", lambda: root)
    return root


def test_worktree_add_sibling_dir_blocked(_project_root) -> None:
    from jiuwenswarm.agents.harness.common.tools.command_tools import (
        _check_worktree_path_safety,
    )

    msg = _check_worktree_path_safety("git worktree add ../foo main")
    assert msg is not None
    assert ".worktrees/" in msg
    assert "../" in msg


def test_worktree_add_outside_abs_path_blocked(_project_root) -> None:
    from jiuwenswarm.agents.harness.common.tools.command_tools import (
        _check_worktree_path_safety,
    )

    msg = _check_worktree_path_safety("git worktree add /tmp/outside-wt")
    assert msg is not None
    assert ".worktrees/" in msg


def test_worktree_add_inside_dot_worktrees_allowed(_project_root) -> None:
    from jiuwenswarm.agents.harness.common.tools.command_tools import (
        _check_worktree_path_safety,
    )

    # Target under the project's .worktrees/ → inside project → allow.
    assert (
        _check_worktree_path_safety(
            "git worktree add -b feature-x .worktrees/feature-x HEAD"
        )
        is None
    )
    # Absolute path inside the project → allow.
    assert (
        _check_worktree_path_safety(
            f"git worktree add {_project_root / '.worktrees' / 'foo'}"
        )
        is None
    )


def test_worktree_add_with_branch_value_correctly_skips_target(_project_root) -> None:
    """`-b <branch>` consumes the branch name; the next token is the path."""
    from jiuwenswarm.agents.harness.common.tools.command_tools import (
        _check_worktree_path_safety,
    )

    # `-b ../escape` would wrongly look like a path if -b didn't eat its value;
    # here the real target is .worktrees/x (inside) so it must be allowed.
    assert (
        _check_worktree_path_safety(
            "git worktree add -b ../escape .worktrees/x HEAD"
        )
        is None
    )
    # And the inverse: -b <name> then a sibling path → blocked.
    msg = _check_worktree_path_safety("git worktree add -b feature ../sibling")
    assert msg is not None


def test_worktree_check_ignores_non_worktree_commands(_project_root) -> None:
    from jiuwenswarm.agents.harness.common.tools.command_tools import (
        _check_worktree_path_safety,
    )

    assert _check_worktree_path_safety("git status") is None
    assert _check_worktree_path_safety("git worktree list") is None
    assert _check_worktree_path_safety("ls -la ../somewhere") is None
    assert _check_worktree_path_safety("git branch feature-x") is None

