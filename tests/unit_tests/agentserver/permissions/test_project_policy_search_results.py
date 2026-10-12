"""Base search and listing tools do not reveal read-denied project files.

Found in a desktop test of the merged MRs: with ``deny_read: [secret/**]`` the
model called agent-core's ``grep`` over the project root with
``output_mode: files_with_matches`` and ``-i``, and the result listed
``secret/keys.txt``. ``read_file`` on it was refused, but the search itself was
a content oracle, and ``content`` mode printed the denied line.

These tests drive the real ``grep`` / ``glob`` / ``list_files`` tools on a temp
project through the real guard rail: a ``ToolCall`` whose ``arguments`` is JSON
text, ``before_tool_call``, the tool, its own rendering, ``after_tool_call``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.core.sys_operation import OperationMode
from openjiuwen.core.sys_operation.config import LocalWorkConfig
from openjiuwen.core.sys_operation.cwd import set_cwd
from openjiuwen.core.sys_operation.sys_operation import SysOperation, SysOperationCard
from openjiuwen.harness.tools.base_tool import ToolOutput
from openjiuwen.harness.tools.filesystem import GlobTool, GrepTool, ListDirTool

import jiuwenswarm.agents.harness.code.rails.shell_policy_guard_rail as guard_mod
from jiuwenswarm.agents.harness.code.rails.shell_policy_guard_rail import (
    ShellPolicyGuardRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions import project_policy as pp

SECRET = "not-a-real-secret"
NOTICE = "left out of these results"


@pytest.fixture
def project(tmp_path, monkeypatch):
    base = tmp_path.resolve()
    root = base / "calc-project"
    (root / "secret").mkdir(parents=True)
    (root / "calc").mkdir()
    (root / "secret" / "keys.txt").write_text(
        f"SANDBOX_MARKER_7f3a = {SECRET}\n", encoding="utf-8"
    )
    (root / "calc" / "__init__.py").write_text("SANDBOX_OK = 1\n", encoding="utf-8")
    (root / "a-1-b.txt").write_text("SANDBOX dash name\n", encoding="utf-8")
    (root / ".jiuwenswarm").mkdir()
    (root / ".jiuwenswarm" / "permissions.yaml").write_text(
        yaml.safe_dump({"deny_read": ["secret/**"]}), encoding="utf-8"
    )
    pp._CACHE.clear()
    monkeypatch.setattr(pp, "current_project_root", lambda: str(root))
    monkeypatch.setattr(guard_mod, "current_project_root", lambda: str(root))
    # agent-core writes operation logs under the process cwd; keep them out of the project
    monkeypatch.chdir(base)
    set_cwd(str(root))
    return root


def _tools(root: Path) -> dict:
    op = SysOperation(
        SysOperationCard(
            id="op",
            mode=OperationMode.LOCAL,
            work_config=LocalWorkConfig(sandbox_root=[str(root.parent)]),
        )
    )
    return {"grep": GrepTool(op), "glob": GlobTool(op), "list_files": ListDirTool(op)}


async def _call(root: Path, name: str, args: dict) -> tuple[str, AgentCallbackContext]:
    tc = ToolCall(id="call-1", type="function", name=name, arguments=json.dumps(args))
    ctx = AgentCallbackContext(
        agent=None,
        inputs=ToolCallInputs(tool_call=tc, tool_name=name, tool_args=tc.arguments),
        extra={},
    )
    assert isinstance(ctx.inputs.tool_args, str)  # the real shape
    rail = ShellPolicyGuardRail(permissions={})
    await rail.before_tool_call(ctx)
    if ctx.extra.get("_skip_tool_calls", {}).get("call-1"):
        return "REFUSED " + ctx.inputs.tool_msg.content, ctx
    tool = _tools(root)[name]
    out = await tool.invoke(json.loads(ctx.inputs.tool_args))
    ctx.inputs.tool_result = out
    ctx.inputs.tool_msg = ToolMessage(
        content=tool.render_for_llm(out), tool_call_id="call-1"
    )
    await rail.after_tool_call(ctx)
    return ctx.inputs.tool_msg.content, ctx


def _leaks(text: str) -> bool:
    return SECRET in text or "keys.txt" in text


# -- the desktop case and the other grep modes ----------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"pattern": "sandbox_marker", "output_mode": "files_with_matches", "-i": True},
        {"pattern": "sandbox_marker", "output_mode": "content", "-i": True},
        {"pattern": "SANDBOX", "output_mode": "content", "-C": 1},
        {"pattern": "SANDBOX", "output_mode": "content", "-n": False},
        {"pattern": "SANDBOX", "output_mode": "count"},
        {"pattern": "SANDBOX", "glob": "*.txt"},
    ],
    ids=[
        "files_with_matches-i",
        "content-i",
        "content-context",
        "content-no-lineno",
        "count",
        "glob-arg",
    ],
)
@pytest.mark.parametrize("path_style", ["absolute", "relative", "absent"])
async def test_grep_over_the_project_hides_denied_files(
    project, args, path_style
) -> None:
    call_args = dict(args)
    if path_style == "absolute":
        call_args["path"] = str(project)
    elif path_style == "relative":
        call_args["path"] = "."
    text, ctx = await _call(project, "grep", call_args)
    assert not text.startswith("REFUSED")
    assert not _leaks(text), text
    assert not _leaks(json.dumps(ctx.inputs.tool_result.data)), (
        ctx.inputs.tool_result.data
    )
    assert NOTICE in text
    if args["pattern"] == "SANDBOX" and args.get("glob") != "*.txt":
        # allowed matches are still returned
        assert "calc/__init__.py" in text or "calc/__init__.py" in json.dumps(
            ctx.inputs.tool_result.data
        )


@pytest.mark.asyncio
async def test_grep_result_does_not_depend_on_denied_content(project) -> None:
    """The notice is fixed, so a guessed string cannot be tested against a denied file."""
    present, _ = await _call(
        project,
        "grep",
        {"pattern": SECRET, "path": str(project), "output_mode": "count"},
    )
    absent, _ = await _call(
        project,
        "grep",
        {"pattern": "zz-nowhere-zz", "path": str(project), "output_mode": "count"},
    )
    assert present == absent


@pytest.mark.asyncio
async def test_allowed_names_with_separators_are_kept(project) -> None:
    text, _ = await _call(
        project, "grep", {"pattern": "dash name", "path": str(project)}
    )
    assert "a-1-b.txt" in text and "dash name" in text


@pytest.mark.asyncio
async def test_symlink_into_denied_directory_is_hidden(project) -> None:
    os.symlink(project / "secret", project / "shortcut")
    text, _ = await _call(
        project,
        "grep",
        {"pattern": "SANDBOX_MARKER", "path": str(project), "output_mode": "content"},
    )
    assert not _leaks(text) and "shortcut" not in text


# -- searches aimed at the denied directory are refused up front -----------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("grep", {"pattern": "SANDBOX", "path": "secret"}),
        ("glob", {"pattern": "*.txt", "path": "secret"}),
        ("glob", {"pattern": "secret/*"}),
        ("list_files", {"path": "secret"}),
    ],
)
async def test_search_rooted_in_denied_directory_is_refused(
    project, name, args
) -> None:
    text, _ = await _call(project, name, args)
    assert text.startswith("REFUSED") and "project_deny_paths" in text


# -- glob and list_files ---------------------------------------------------------
@pytest.mark.asyncio
async def test_glob_over_the_project_hides_denied_files(project) -> None:
    text, ctx = await _call(
        project, "glob", {"pattern": "**/*.txt", "path": str(project)}
    )
    assert not _leaks(text) and "a-1-b.txt" in text and NOTICE in text
    data = ctx.inputs.tool_result.data
    assert not _leaks(json.dumps(data))
    assert data["numFiles"] == len(data["filenames"]) == len(data["matching_files"])


@pytest.mark.asyncio
async def test_list_files_hides_a_denied_file_but_keeps_its_directory(project) -> None:
    (project / ".jiuwenswarm" / "permissions.yaml").write_text(
        yaml.safe_dump({"deny_read": ["secret/**", "a-1-b.txt"]}), encoding="utf-8"
    )
    pp._CACHE.clear()
    text, _ = await _call(project, "list_files", {"path": str(project)})
    assert "secret/" in text and "calc/" in text
    assert "a-1-b.txt" not in text and NOTICE in text


# -- ripgrep output shape (no rg binary needed) ------------------------------------
@pytest.mark.asyncio
async def test_ripgrep_style_output_with_absolute_context_lines(project) -> None:
    """rg prints context lines as ``path-N-text`` with the path as given, and ``--`` separators."""
    secret = project / "secret" / "keys.txt"
    allowed = project / "calc" / "__init__.py"
    stdout = "\n".join(
        [
            f"{allowed}:1:SANDBOX_OK = 1",
            "--",
            f"{secret}-0-context line",
            f"{secret}:1:SANDBOX_MARKER_7f3a = {SECRET}",
            "--",
        ]
    )
    data = GrepTool(None)._build_structured_output(
        stdout=stdout,
        stderr="",
        exit_code=0,
        output_mode="content",
        head_limit=None,
        offset=0,
        base_path=str(project),
    )
    tc = ToolCall(
        id="call-1",
        type="function",
        name="grep",
        arguments=json.dumps({"pattern": "SANDBOX", "path": str(project), "-C": 1}),
    )
    out = ToolOutput(success=True, data=data)
    ctx = AgentCallbackContext(
        agent=None,
        inputs=ToolCallInputs(
            tool_call=tc,
            tool_name="grep",
            tool_args=tc.arguments,
            tool_result=out,
            tool_msg=ToolMessage(content=data["stdout"], tool_call_id="call-1"),
        ),
        extra={},
    )
    await ShellPolicyGuardRail(permissions={}).after_tool_call(ctx)
    text = ctx.inputs.tool_msg.content
    assert not _leaks(text) and "context line" not in text
    assert "calc/__init__.py:1:SANDBOX_OK = 1" in text
    assert not text.startswith("--") and "--\n--" not in text


# -- fail closed -------------------------------------------------------------------
@pytest.mark.asyncio
async def test_filter_error_withholds_the_results(project, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(guard_mod, "filter_search_result", boom)
    text, ctx = await _call(
        project, "grep", {"pattern": "SANDBOX", "path": str(project)}
    )
    assert "Results withheld" in text and not _leaks(text)
    assert ctx.inputs.tool_result.success is False


@pytest.mark.asyncio
async def test_no_read_denies_leaves_results_untouched(project) -> None:
    (project / ".jiuwenswarm" / "permissions.yaml").write_text(
        yaml.safe_dump({"deny_write": ["secret/**"]}), encoding="utf-8"
    )
    pp._CACHE.clear()
    text, _ = await _call(
        project, "grep", {"pattern": "SANDBOX_MARKER", "path": str(project)}
    )
    assert "keys.txt" in text and NOTICE not in text


# -- shell readers aimed at the denied directory -------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command", ["grep -rn SANDBOX secret", "rg SANDBOX secret", "ls secret"]
)
async def test_shell_reader_of_denied_directory_is_refused(project, command) -> None:
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
    await ShellPolicyGuardRail(permissions={}).before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool_calls", {}).get("call-1")
    assert "project_deny_paths" in ctx.inputs.tool_msg.content
