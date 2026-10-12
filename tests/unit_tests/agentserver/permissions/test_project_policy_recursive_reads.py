"""Recursive shell reads over a folder that contains read-denied paths are refused.

Found in the desktop re-test of the merged MRs: with ``deny_read: [secret/**]``
the filtered ``grep`` tool returned no matches, and the model fell back to

    cd <project> && ls -la && grep -rn --exclude-dir=.venv --exclude-dir=.git \
        "SANDBOX_MARKER" . ; echo "exit=$?"

whose output contained ``./secret/keys.txt:1:SANDBOX_MARKER_7f3a = ...``. The
path check saw ``.`` (the project root), which is not itself denied. The guard
now refuses a recursive read (search, copy, archive) whose root contains a
denied path.

Each case drives the real ``ShellPolicyGuardRail.before_tool_call`` with a real
``ToolCall`` whose ``arguments`` is JSON text, on a temp project.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.core.sys_operation.cwd import set_cwd

import jiuwenswarm.agents.harness.code.rails.shell_policy_guard_rail as guard_mod
from jiuwenswarm.agents.harness.code.rails.shell_policy_guard_rail import (
    ShellPolicyGuardRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions import project_policy as pp


def _make_project(base: Path, policy: dict) -> Path:
    root = base / "calc-project"
    (root / "secret").mkdir(parents=True)
    (root / "calc").mkdir()
    (root / "secret" / "keys.txt").write_text(
        "SANDBOX_MARKER_7f3a = not-a-real-secret\n", encoding="utf-8"
    )
    (root / "calc" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (root / "test_calc.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    (root / ".jiuwenswarm").mkdir()
    (root / ".jiuwenswarm" / "permissions.yaml").write_text(
        yaml.safe_dump(policy), encoding="utf-8"
    )
    return root


def _activate(root: Path, monkeypatch) -> None:
    pp._CACHE.clear()
    monkeypatch.setattr(pp, "current_project_root", lambda: str(root))
    monkeypatch.setattr(guard_mod, "current_project_root", lambda: str(root))
    set_cwd(str(root))


@pytest.fixture
def project(tmp_path, monkeypatch):
    base = tmp_path.resolve()
    root = _make_project(base, {"deny_read": ["secret/**"]})
    monkeypatch.chdir(base)
    _activate(root, monkeypatch)
    return root


async def _verdict(command: str) -> dict | None:
    tc = ToolCall(
        id="call-1",
        type="function",
        name="bash",
        arguments=json.dumps({"command": command}),
    )
    ctx = AgentCallbackContext(
        agent=None,
        inputs=ToolCallInputs(tool_call=tc, tool_name="bash", tool_args=tc.arguments),
        extra={},
    )
    assert isinstance(ctx.inputs.tool_args, str)  # the real shape
    await ShellPolicyGuardRail(permissions={}).before_tool_call(ctx)
    if not ctx.extra.get("_skip_tool_calls", {}).get("call-1"):
        return None
    content = ctx.inputs.tool_msg.content
    return json.loads(content) if content.startswith("{") else {"raw": content}


# -- the desktop command --------------------------------------------------------------
@pytest.mark.asyncio
async def test_desktop_fallback_command_is_refused(project):
    command = (
        f"cd {project} && ls -la && grep -rn --exclude-dir=.venv --exclude-dir=.git "
        '"SANDBOX_MARKER" . ; echo "exit=$?"'
    )
    verdict = await _verdict(command)
    assert verdict is not None, "recursive grep over the project root must be refused"
    text = json.dumps(verdict)
    assert "project_deny_paths" in text
    assert "grep tool" in text
    assert "not-a-real-secret" not in text


# -- recursive readers over a folder that contains denied paths -----------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "grep -rn SANDBOX .",
        "grep -R SANDBOX",
        "grep -Rin sandbox .",
        "grep -nri sandbox .",
        "grep --recursive SANDBOX .",
        "grep -d recurse SANDBOX .",
        "grep -drecurse SANDBOX .",
        "grep --directories=recurse SANDBOX .",
        "grep -r -e SANDBOX",
        "egrep -r SANDBOX .",
        "fgrep -R SANDBOX .",
        "grep -rn SANDBOX ..",
        "rg SANDBOX",
        "rg -n SANDBOX .",
        "rg -l sandbox -i",
        "rg -e SANDBOX",
        "ag SANDBOX",
        "ack SANDBOX",
        "git grep SANDBOX",
        "git -C . grep -n SANDBOX",
        "find . -type f -exec cat {} +",
        "find . -name '*.txt' -exec grep -H SANDBOX {} \\;",
        "find -L . -type f -execdir head -n1 {} +",
        "find . -type f | xargs cat",
        "find . -type f -print0 | xargs -0 grep SANDBOX",
        "rg --files | xargs cat",
        "git ls-files | xargs grep SANDBOX",
        "fd . -x cat",
        "fd -t f . -X cat",
        "tar czf /tmp/x.tgz .",
        "tar -czf /tmp/x.tgz .",
        "tar --create --file=/tmp/x.tar .",
        "zip -r /tmp/x.zip .",
        "zip -qr /tmp/x.zip .",
        "cp -r . /tmp/copy",
        "cp -a secret /tmp/s",
        "cp -R secret /tmp/s",
        "rsync -a . /tmp/r",
        "rsync -rv ./ /tmp/r",
        "ditto . /tmp/d",
        "mv secret /tmp/s",
        "bash -c 'grep -rn SANDBOX .'",
        "sudo grep -r SANDBOX .",
        "cd calc && grep -r SANDBOX ..",
        "cat */*.txt",
        "grep x */*",
    ],
)
async def test_recursive_reads_over_denied_paths_are_refused(project, command):
    assert await _verdict(command) is not None, command


# -- recursive reads that cannot reach denied paths, and ordinary commands -------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "grep -rn x calc",
        "grep -rn x calc/",
        "rg x calc",
        "rg -n x calc test_calc.py",
        "cd calc && grep -r x .",
        "find calc -name '*.py' -exec grep -l x {} +",
        "grep x test_calc.py",
        "grep -n x calc/__init__.py",
        "cat test_calc.py",
        "cat test_calc.py | rg test",
        "ls -la",
        "ls calc",
        "find . -name '*.py'",
        "rg --files",
        "git status",
        "git diff",
        "python -m pytest -q",
        "tar tzf /tmp/nonexistent.tgz",
        "cp test_calc.py /tmp/t.py",
    ],
)
async def test_other_commands_are_allowed(project, command):
    assert await _verdict(command) is None, command


@pytest.mark.asyncio
async def test_no_read_deny_means_no_recursive_refusal(tmp_path, monkeypatch):
    base = tmp_path.resolve()
    root = _make_project(base, {"deny_write": ["secret/**"]})
    monkeypatch.chdir(base)
    _activate(root, monkeypatch)
    assert await _verdict("grep -rn SANDBOX .") is None


# -- denied_below: what "contains a denied path" means --------------------------------
def _policy(root: Path, entries: list[str]):
    (root / ".jiuwenswarm").mkdir(exist_ok=True)
    (root / ".jiuwenswarm" / "permissions.yaml").write_text(
        yaml.safe_dump({"deny_read": entries}), encoding="utf-8"
    )
    pp._CACHE.clear()
    return pp.load_project_policy(str(root), config={})


def test_denied_area_below_counts_only_when_it_exists(tmp_path):
    root = tmp_path.resolve()
    policy = _policy(root, ["secret/**"])
    assert policy.denied_below(root, "read") is None  # nothing there yet
    (root / "secret").mkdir()
    assert policy.denied_below(root, "read") is not None
    assert policy.denied_below(root / "secret", "read") is not None  # the area itself


def test_root_level_glob_needs_a_matching_file(tmp_path):
    root = tmp_path.resolve()
    (root / "src").mkdir()
    policy = _policy(root, [".env*"])
    assert policy.denied_below(root, "read") is None
    (root / ".env.local").write_text("K=V\n", encoding="utf-8")
    assert policy.denied_below(root, "read") is not None
    assert policy.denied_below(root / "src", "read") is None


def test_double_star_glob_is_found_deep(tmp_path):
    root = tmp_path.resolve()
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    policy = _policy(root, ["**/*.pem"])
    assert policy.denied_below(root, "read") is None
    (deep / "server.pem").write_text("x", encoding="utf-8")
    assert policy.denied_below(root, "read") is not None
    assert policy.denied_below(root / "a", "read") is not None
    (root / "other").mkdir()
    assert policy.denied_below(root / "other", "read") is None


def test_walk_limit_fails_closed(tmp_path):
    root = tmp_path.resolve()
    for i in range(20):
        (root / f"f{i}.txt").write_text("x", encoding="utf-8")
    policy = _policy(root, ["**/*.pem"])
    assert policy.denied_below(root, "read", walk_limit=5) is not None
    assert policy.denied_below(root, "read") is None
