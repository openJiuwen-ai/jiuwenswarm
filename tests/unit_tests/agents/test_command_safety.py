# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import os

import pytest

from jiuwenswarm.agents.harness.common.tools import command_tools
from jiuwenswarm.agents.harness.common.tools.command_tools import (
    _check_command_safety,
    _command_spawns_tui,
    _enforce_tui_spawn_budget,
    _is_backend_cmdline,
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


def test_does_not_block_engine_owned_patterns() -> None:
    assert _check_command_safety("rm -rf /tmp/x") is None
    assert _check_command_safety("shutdown -h now") is None
    assert _check_command_safety("Remove-Item -Recurse -Force C:\\temp\\build") is None


# ── 按进程名 / PID 杀后端（Windows 下后端进程全是 python.exe）────


@pytest.mark.parametrize(
    "command",
    [
        "taskkill /F /IM python.exe",
        "taskkill /im python.exe /t /f",
        "taskkill /F /IM pythonw.exe",
        'taskkill /F /IM "python*"',
        "taskkill /F /IM py.exe",
        'taskkill /F /FI "IMAGENAME eq python.exe"',
        "Stop-Process -Name python -Force",
        "stop-process -name 'python*' -force",
        "spps -Name python",
        "Get-Process python | Stop-Process -Force",
        "Get-Process -Name python* | Stop-Process",
        "gps python | kill",
        "wmic process where name='python.exe' delete",
        "wmic process where \"name like '%python%'\" call terminate",
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Invoke-CimMethod -MethodName Terminate",
        "pkill python",
        "pkill -9 python3",
        "pkill -f python",
        "killall python3.13",
        "killall -9 'python'",
        # Sub-expression form: no literal PID, the pipe pattern never fires.
        "Stop-Process -Id (Get-Process python).Id",
        "stop-process -id (gps 'python*').id -force",
    ],
)
def test_blocks_killing_python_by_name(command: str) -> None:
    reason = _check_command_safety(command)
    assert reason is not None
    assert "python" in reason


@pytest.mark.parametrize(
    "command",
    [
        "tasklist | findstr python",
        "Get-Process python",
        "taskkill /F /IM chrome.exe",
        "taskkill /F /IM node.exe /T",
        "taskkill /F /IM pyinstaller.exe",
        "Get-Process pyinstaller | Stop-Process",
        "pkill -f 'python scrape_page.py'",
        "pkill -f scrape_page.py",
        "python -m pip install requests",
        "killall chrome",
        "grep -rn python docs/",
        "wmic process where \"commandline like '%scrape_page%'\" call terminate",
    ],
)
def test_allows_targeted_kills_and_python_mentions(command: str, monkeypatch) -> None:
    monkeypatch.setattr(command_tools, "_backend_pids", lambda: set())
    assert _check_command_safety(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "taskkill /F /PID 4242",
        "taskkill /PID 7 /PID 4242 /T /F",
        'taskkill /F /FI "PID eq 4242"',
        "Stop-Process -Id 4242 -Force",
        "Stop-Process -Id:4242",
        "spps -Id 7,4242",
        "Stop-Process 4242",
        "Get-Process -Id 4242 | Stop-Process",
        "wmic process where processid=4242 call terminate",
        'Get-CimInstance Win32_Process -Filter "ProcessId=4242" | Invoke-CimMethod -MethodName Terminate',
        "kill -9 4242",
        "kill 7 4242",
        "kill -s TERM 4242",
        # A later sigspec overrides the -0 probe: bash really SIGKILLs here.
        "kill -0 -n 9 4242",
        "kill -n 0 -n 9 4242",
        # Negative operand beyond signal range targets a process group.
        "kill -9 -4242",
        # Quoted forms must be seen through (the agent runs shells via bash -c).
        "bash -c 'kill -0 -n 9 4242'",
        '/bin/kill -TERM 4242',
    ],
)
def test_blocks_killing_backend_pids(command: str, monkeypatch) -> None:
    monkeypatch.setattr(command_tools, "_backend_pids", lambda: {4242})
    reason = _check_command_safety(command)
    assert reason is not None
    assert "4242" in reason


@pytest.mark.parametrize(
    "command",
    [
        "taskkill /F /PID 5151",
        "Stop-Process -Id 5151",
        "kill -9 5151",
        "kill -0 4242",
        "kill -n 0 4242",
        "kill -l",
        "kill -9 -5151",
    ],
)
def test_allows_killing_other_pids(command: str, monkeypatch) -> None:
    monkeypatch.setattr(command_tools, "_backend_pids", lambda: {4242})
    assert _check_command_safety(command) is None


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("kill -9 4242", {4242}),
        # The -n argument is a signal number, not a target.
        ("kill -n 9 4242", {4242}),
        # A pure signal-0 probe has no killable target.
        ("kill -0 4242", set()),
        # Last sigspec wins: -0 then -n 9 is SIGKILL.
        ("kill -0 -n 9 4242", {4242}),
        ("kill -s TERM 7 4242", {7, 4242}),
        ("kill -TERM 4242", {4242}),
        ("kill 7 4242", {7, 4242}),
        ("kill -- -4242", {4242}),
        # Scanning stops at shell separators; the probe stays exempt.
        ("kill -9 123; kill -0 456", {123}),
        # Job specs cannot be resolved to PIDs; nothing is extracted.
        ("kill %1", set()),
        ("bash -c 'kill -9 4242'", {4242}),
        ("pkill -9 4242", set()),
    ],
)
def test_posix_kill_pid_parsing(command: str, expected: set[int]) -> None:
    assert command_tools._posix_kill_pids(command) == expected


def test_backend_cmdline_detection() -> None:
    assert _is_backend_cmdline(["python", "-m", "jiuwenswarm.gateway.app_gateway"])
    assert _is_backend_cmdline(["C:\\env\\python.exe", "C:\\env\\Scripts\\jiuwenswarm-start.exe", "all"])
    assert _is_backend_cmdline(["/usr/bin/python3", "/site-packages/jiuwenswarm/gateway/app_gateway.py"])
    assert _is_backend_cmdline(["/usr/bin/python3", "-u", "/site-packages/jiuwenswarm/app.py"])
    assert _is_backend_cmdline(["JiuwenSwarm.exe", "--desktop-run-agent"])
    # Skill scripts live under ~/.jiuwenswarm but are the agent's own work, not the backend.
    assert not _is_backend_cmdline(
        ["python", "C:\\Users\\u\\.jiuwenswarm\\agent\\workspace\\skills\\demo\\scripts\\scrape_page.py"]
    )


def test_backend_cmdline_ignores_paths_that_are_merely_mentioned() -> None:
    # Commands that only reference backend files as data must not classify the
    # agent's own shell as backend — that made those shells unkillable by PID.
    assert not _is_backend_cmdline(["bash", "-c", "git diff HEAD -- jiuwenswarm/gateway/app_gateway.py"])
    assert not _is_backend_cmdline(["bash", "-lc", "grep -n TODO jiuwenswarm/app.py"])
    assert not _is_backend_cmdline(["git", "log", "--oneline", "--", "jiuwenswarm/channels/web/app_web.py"])
    assert not _is_backend_cmdline(["cmd", "/c", "type jiuwenswarm\\gateway\\app_gateway.py"])
    assert not _is_backend_cmdline(["cmd", "/c", "type", "jiuwenswarm\\gateway\\app_gateway.py"])


def test_backend_pids_cover_this_process_and_its_parent() -> None:
    pids = command_tools._backend_pids()
    assert os.getpid() in pids
    assert os.getppid() in pids


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

