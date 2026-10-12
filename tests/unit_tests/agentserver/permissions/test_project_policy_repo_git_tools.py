"""Project deny-paths cover the repo-aware search tools and the git tools.

Found by the integration audit of all open MRs: with ``deny_read: [secret/**]``
``read_file`` and ``grep`` were refused, but ``code_search`` / ``find_symbol`` /
``symbol_outline`` (repo tools MR) and ``git_prepare`` / ``git_publish`` (git
workflow MR) were outside the guard. The guard covers them by tool name, so the
coverage is harmless when those tools are not registered.

The contexts are built the way agent-core builds them: a real ``ToolCall``
whose ``arguments`` is JSON *text*, passed through unchanged as ``tool_args``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs

import jiuwenswarm.agents.harness.code.rails.shell_policy_guard_rail as guard_mod
from jiuwenswarm.agents.harness.code.rails.shell_policy_guard_rail import (
    FailClosedShellPolicyGuardRail,
    ShellPolicyGuardRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions import project_policy as pp


def _ctx(name: str, args: dict) -> AgentCallbackContext:
    tc = ToolCall(id="call-1", type="function", name=name, arguments=json.dumps(args))
    return AgentCallbackContext(
        agent=None,
        inputs=ToolCallInputs(tool_call=tc, tool_name=tc.name, tool_args=tc.arguments),
        extra={},
    )


def _refusal(ctx: AgentCallbackContext) -> dict | None:
    if not ctx.extra.get("_skip_tool_calls", {}).get("call-1"):
        return None
    return json.loads(ctx.inputs.tool_msg.content)


async def _run(name: str, args: dict) -> dict | None:
    ctx = _ctx(name, args)
    assert isinstance(ctx.inputs.tool_args, str)  # the real shape
    await ShellPolicyGuardRail(permissions={}).before_tool_call(ctx)
    return _refusal(ctx)


def _git(root: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=root, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "proj"
    (root / "secret").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "secret" / "keys.py").write_text("TOPSECRET = 1\n", encoding="utf-8")
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    d = root / ".jiuwenswarm"
    d.mkdir()
    (d / "permissions.yaml").write_text(
        yaml.safe_dump({"deny_read": ["secret/**"]}), encoding="utf-8"
    )
    pp._CACHE.clear()
    monkeypatch.setattr(pp, "current_project_root", lambda: str(root))
    monkeypatch.setattr(guard_mod, "current_project_root", lambda: str(root))
    return root


@pytest.fixture
def repo(project):
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.email", "t@example.com")
    _git(project, "config", "user.name", "t")
    _git(project, "add", "src/app.py", "secret/keys.py")
    _git(project, "commit", "-qm", "init")
    return project


# -- repo-aware search tools --------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [
        ("code_search", {"pattern": "TOPSECRET", "path": "secret"}),
        ("code_search", {"pattern": "TOPSECRET", "path": "secret/keys.py"}),
        ("code_search", {"pattern": "TOPSECRET", "glob": "secret/**"}),
        ("code_search", {"pattern": "TOPSECRET", "glob": ["*.md", "secret/*.py"]}),
        ("code_search", {"pattern": "TOPSECRET", "path": ".", "glob": "secret/**"}),
        ("find_symbol", {"name": "TOPSECRET", "file_path": "secret/keys.py"}),
        ("find_symbol", {"name": "TOPSECRET", "path": "secret"}),
        ("symbol_outline", {"file_path": "secret/keys.py"}),
        ("symbol_outline", {"file_path": "src/../secret/keys.py"}),
    ],
)
async def test_repo_tools_rooted_in_denied_paths_are_refused(project, name, args):
    refusal = await _run(name, args)
    assert refusal is not None and refusal["policy"] == "project_deny_paths", refusal
    assert refusal["access"] == "read"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [
        ("code_search", {"pattern": "x", "path": "src"}),
        ("code_search", {"pattern": "x", "glob": ["*.py", "!secret/**"]}),
        (
            "code_search",
            {"pattern": "x"},
        ),  # whole repository: result filtering is the tool's job
        ("find_symbol", {"name": "x"}),
        ("symbol_outline", {"file_path": "src/app.py"}),
    ],
)
async def test_repo_tools_outside_denied_paths_run(project, name, args):
    assert await _run(name, args) is None


# -- git tools ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_git_prepare_refuses_committing_a_denied_path(repo):
    (repo / "secret" / "keys.py").write_text("TOPSECRET = 2\n", encoding="utf-8")
    refusal = await _run(
        "git_prepare",
        {"action": "commit", "message": "m", "paths": ["src/app.py", "secret/keys.py"]},
    )
    assert refusal is not None and refusal["policy"] == "project_deny_paths"
    assert "secret/keys.py" in refusal["path"]


@pytest.mark.asyncio
async def test_git_prepare_all_tracked_refuses_when_a_denied_file_changed(repo):
    (repo / "secret" / "keys.py").write_text("TOPSECRET = 2\n", encoding="utf-8")
    refusal = await _run(
        "git_prepare", {"action": "commit", "message": "m", "all_tracked": True}
    )
    assert refusal is not None and refusal["policy"] == "project_deny_paths"


@pytest.mark.asyncio
async def test_git_prepare_all_tracked_refuses_a_staged_denied_file(repo):
    (repo / "secret" / "keys.py").write_text("TOPSECRET = 3\n", encoding="utf-8")
    _git(repo, "add", "secret/keys.py")
    refusal = await _run(
        "git_prepare", {"action": "commit", "message": "m", "all_tracked": True}
    )
    assert refusal is not None and refusal["policy"] == "project_deny_paths"


@pytest.mark.asyncio
async def test_git_prepare_allowed_cases(repo):
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    assert (
        await _run(
            "git_prepare", {"action": "commit", "message": "m", "paths": ["src/app.py"]}
        )
        is None
    )
    assert (
        await _run(
            "git_prepare", {"action": "commit", "message": "m", "all_tracked": True}
        )
        is None
    )
    # status / branch never commit anything
    assert await _run("git_prepare", {"action": "status"}) is None
    assert await _run("git_prepare", {"action": "branch", "name": "feat"}) is None


@pytest.mark.asyncio
async def test_git_publish_refuses_outgoing_commits_touching_denied_paths(
    repo, tmp_path
):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")
    # Only src changes outgoing: allowed.
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "src")
    assert await _run("git_publish", {"remote": "origin"}) is None
    # An outgoing commit touching the denied file: refused.
    (repo / "secret" / "keys.py").write_text("TOPSECRET = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "secret")
    refusal = await _run("git_publish", {"remote": "origin"})
    assert refusal is not None and refusal["policy"] == "project_deny_paths"


@pytest.mark.asyncio
async def test_git_tools_outside_a_repository_are_left_to_the_tool(project):
    assert (
        await _run(
            "git_prepare", {"action": "commit", "message": "m", "all_tracked": True}
        )
        is None
    )
    assert await _run("git_publish", {}) is None


@pytest.mark.asyncio
async def test_no_policy_file_means_no_git_subprocess(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "bare"
    root.mkdir()
    pp._CACHE.clear()
    monkeypatch.setattr(pp, "current_project_root", lambda: str(root))
    monkeypatch.setattr(guard_mod, "current_project_root", lambda: str(root))

    def boom(*_a, **_kw):
        raise AssertionError("git must not run without project deny paths")

    monkeypatch.setattr(ShellPolicyGuardRail, "_git_names", staticmethod(boom))
    assert await _run("git_publish", {}) is None
    assert await _run("code_search", {"pattern": "x", "path": "secret"}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    ["code_search", "find_symbol", "symbol_outline", "git_prepare", "git_publish"],
)
async def test_fail_closed_guard_covers_the_new_tools(name):
    ctx = _ctx(name, {"file_path": "src/app.py"})
    await FailClosedShellPolicyGuardRail(reason="test").before_tool_call(ctx)
    refusal = _refusal(ctx)
    assert refusal is not None and refusal["policy"] == "policy_guard_unavailable"
