# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ContextDietTrimRail (after_tool_call rewrite of tool messages)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm.schema.message import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs

from jiuwenswarm.agents.harness.code.context_diet import TrimConfig
from jiuwenswarm.agents.harness.code.rails import ContextDietTrimRail
from jiuwenswarm.agents.harness.code.rails.context_diet_trim_rail import _session_id


def _long_shell(n: int = 400) -> str:
    body = "\n".join(f"line {i}" if i != 200 else "Traceback (most recent call last):" for i in range(n))
    return "Command: pytest -q\nStdout: " + body + "\n\nStderr: (empty)\nExit Code: 1"


def _ctx(tool_name: str, content, *, tool_args=None, call_id: str = "call_1", session_id: str = "sess-1"):
    tool_msg = ToolMessage(content=content, tool_call_id=call_id, name=tool_name) if content is not None else None
    return AgentCallbackContext(
        agent=SimpleNamespace(),
        session=SimpleNamespace(session_id=session_id),
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id=call_id, name=tool_name),
            tool_name=tool_name,
            tool_args=tool_args or {},
            tool_result={"raw": True},
            tool_msg=tool_msg,
        ),
    )


@pytest.mark.asyncio
async def test_rail_rewrites_long_bash_message_and_spills_full_output(tmp_path):
    rail = ContextDietTrimRail(TrimConfig(), spill_root=tmp_path)
    rail.init(SimpleNamespace())
    ctx = _ctx("bash", _long_shell(), tool_args={"command": "pytest -q"})
    original = ctx.inputs.tool_msg

    await rail.after_tool_call(ctx)

    msg = ctx.inputs.tool_msg
    assert msg is not original and isinstance(msg, ToolMessage)
    assert msg.tool_call_id == "call_1" and msg.name == "bash"
    assert len(msg.content) < len(original.content) // 3
    assert "[context-diet]" in msg.content and "Traceback" in msg.content
    assert msg.content.splitlines()[-1] == "Exit Code: 1"
    meta = msg.metadata["context_diet"]
    assert meta["kind"] == "shell" and meta["omitted_lines"] > 250
    spill = tmp_path / "sess-1" / "bash_call_1.txt"
    assert spill.read_text() == original.content
    assert meta["spill_path"] == str(spill)
    # the structured result other rails consume is untouched
    assert ctx.inputs.tool_result == {"raw": True}
    assert rail.stats["rewrites"] == 1 and rail.stats["saved_chars"] > 0


@pytest.mark.asyncio
async def test_rail_leaves_short_and_unmanaged_results_alone(tmp_path):
    rail = ContextDietTrimRail(TrimConfig(), spill_root=tmp_path)
    for name, content in (("bash", "Command: ls\nStdout: a\n\nStderr: (empty)\nExit Code: 0"), ("edit_file", "x" * 50000)):
        ctx = _ctx(name, content)
        before = ctx.inputs.tool_msg
        await rail.after_tool_call(ctx)
        assert ctx.inputs.tool_msg is before
    assert not any(tmp_path.iterdir())
    assert rail.stats["rewrites"] == 0


@pytest.mark.asyncio
async def test_rail_ignores_missing_or_structured_tool_message(tmp_path):
    rail = ContextDietTrimRail(TrimConfig(), spill_root=tmp_path)
    ctx = _ctx("bash", None)
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_msg is None
    ctx2 = _ctx("bash", [{"type": "text", "text": "x" * 20000}])
    before = ctx2.inputs.tool_msg
    await rail.after_tool_call(ctx2)
    assert ctx2.inputs.tool_msg is before


@pytest.mark.asyncio
async def test_rail_disabled_config_changes_nothing(tmp_path):
    rail = ContextDietTrimRail(TrimConfig(enabled=False), spill_root=tmp_path)
    ctx = _ctx("bash", _long_shell())
    before = ctx.inputs.tool_msg
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_msg is before


@pytest.mark.asyncio
async def test_rail_trims_without_spill_dir(tmp_path, monkeypatch):
    rail = ContextDietTrimRail(TrimConfig(spill_dir=str(tmp_path / "missing" / "\0bad")))
    rail._spill_root = None
    # An unwritable spill directory must not stop the trim.
    monkeypatch.setattr(rail, "_resolve_spill_root", lambda: None)
    ctx = _ctx("bash", _long_shell())
    await rail.after_tool_call(ctx)
    assert "not persisted" in ctx.inputs.tool_msg.content
    assert ctx.inputs.tool_msg.metadata["context_diet"]["spill_path"] is None


@pytest.mark.asyncio
async def test_rail_read_file_window_when_enabled(tmp_path):
    rail = ContextDietTrimRail(TrimConfig(read_file_max_lines=100), spill_root=tmp_path)
    content = "\n".join(f"{i:>6}\t# l{i}" for i in range(1, 501))
    ctx = _ctx("read_file", content, tool_args={"file_path": "big.py"})
    await rail.after_tool_call(ctx)
    lines = ctx.inputs.tool_msg.content.splitlines()
    assert len(lines) == 101 and "offset=100" in lines[-1]
    assert not any(tmp_path.iterdir())  # file reads are never spilled


def test_session_id_resolution():
    assert _session_id(SimpleNamespace(session=SimpleNamespace(session_id="abc"))) == "abc"
    assert _session_id(SimpleNamespace(session=SimpleNamespace(get_session_id=lambda: "xyz"))) == "xyz"
    assert _session_id(SimpleNamespace(session=None)) == "unknown"


def test_default_spill_root_is_the_agent_workspace(monkeypatch, tmp_path):
    """Without a seeded cwd ContextVar the default is <agent workspace>/context_diet."""
    from jiuwenswarm.common import utils

    monkeypatch.setattr(utils, "get_agent_workspace_dir", lambda: tmp_path / "agent" / "workspace")
    rail = ContextDietTrimRail(TrimConfig())
    assert rail._resolve_spill_root() == tmp_path / "agent" / "workspace" / "context_diet"
    path = rail._spill("web_1/2", "call/9", "bash", "payload")
    assert path.endswith("web_1_2/bash_call_9.txt")
    assert (tmp_path / "agent" / "workspace" / "context_diet" / "web_1_2" / "bash_call_9.txt").read_text() == "payload"


def test_default_spill_root_follows_the_agent_workspace_contextvar(monkeypatch, tmp_path):
    """A seeded per-agent workspace wins; the project dir / cwd are never used."""
    import contextvars

    from openjiuwen.core.sys_operation.cwd import init_cwd

    from jiuwenswarm.agents.harness.code.rails.context_diet_trim_rail import (
        default_spill_root,
    )
    from jiuwenswarm.common import utils

    monkeypatch.setattr(utils, "get_agent_workspace_dir", lambda: tmp_path / "shared-ws")
    project = tmp_path / "repo"
    agent_ws = tmp_path / "data" / "agent" / "workspace"
    project.mkdir()
    agent_ws.mkdir(parents=True)

    def seeded_private_workspace():
        init_cwd(str(project), project_root=str(project), workspace=str(agent_ws))
        return default_spill_root()

    def seeded_workspace_equals_project():
        init_cwd(str(project), project_root=str(project), workspace=str(project))
        return default_spill_root()

    assert contextvars.copy_context().run(seeded_private_workspace) == agent_ws.resolve() / "context_diet"
    # workspace == project: never write into the checkout, fall back to the shared agent workspace
    assert contextvars.copy_context().run(seeded_workspace_equals_project) == tmp_path / "shared-ws" / "context_diet"


def test_spill_path_passes_the_code_mode_sandbox(tmp_path):
    """read_file / grep on the quoted spill path must not raise 'outside sandbox'."""
    import contextvars
    from types import SimpleNamespace

    from openjiuwen.core.sys_operation.cwd import (
        get_cwd,
        get_project_root,
        get_workspace,
        init_cwd,
    )
    from openjiuwen.core.sys_operation.local.fs_operation import FsOperation

    from jiuwenswarm.agents.harness.code.rails.context_diet_trim_rail import (
        default_spill_root,
    )

    project = tmp_path / "repo"
    agent_ws = tmp_path / "data" / "agent" / "workspace"
    project.mkdir()
    agent_ws.mkdir(parents=True)

    def run():
        init_cwd(str(project), project_root=str(project), workspace=str(agent_ws))
        rail = ContextDietTrimRail(TrimConfig(), spill_root=default_spill_root())
        spill = rail._spill("sess", "call_1", "bash", "full output")
        assert spill is not None
        fs = FsOperation.__new__(FsOperation)
        fs._run_config = SimpleNamespace(restrict_to_sandbox=True, sandbox_root=None)
        # the exact check the SDK performs for read_file / grep under restrict_to_sandbox
        resolved = fs._resolve_path(spill)
        assert resolved.read_text() == "full output"
        roots = [get_workspace(), get_project_root(), get_cwd()]
        assert any(FsOperation._is_within(resolved, __import__("pathlib").Path(r).resolve()) for r in roots)
        assert not FsOperation._is_within(resolved, project.resolve())  # and not inside the repo

    contextvars.copy_context().run(run)


def test_spill_cap_deletes_oldest_files_first(tmp_path):
    import os
    import time

    rail = ContextDietTrimRail(TrimConfig(spill_max_bytes=2500), spill_root=tmp_path)
    paths = []
    for i in range(4):
        p = rail._spill("sess", f"call_{i}", "bash", "x" * 1000)
        paths.append(p)
        os.utime(p, (time.time() - 100 + i, time.time() - 100 + i))
    remaining = sorted(q.name for q in (tmp_path / "sess").iterdir())
    # 4 KB written, cap 2.5 KB -> the two oldest are gone, the newest is always kept
    assert remaining == ["bash_call_2.txt", "bash_call_3.txt"]
    assert sum(q.stat().st_size for q in (tmp_path / "sess").iterdir()) <= 2500

    # 0 disables the cap
    rail0 = ContextDietTrimRail(TrimConfig(spill_max_bytes=0), spill_root=tmp_path / "nocap")
    for i in range(4):
        rail0._spill("sess", f"call_{i}", "bash", "x" * 1000)
    assert len(list((tmp_path / "nocap" / "sess").iterdir())) == 4

    # a single file above the cap is still kept
    rail1 = ContextDietTrimRail(TrimConfig(spill_max_bytes=10), spill_root=tmp_path / "tiny")
    assert rail1._spill("sess", "c", "bash", "y" * 500) is not None
    assert (tmp_path / "tiny" / "sess" / "bash_c.txt").exists()


@pytest.mark.asyncio
async def test_rail_clips_long_single_lines_and_spills_full_output(tmp_path):
    rail = ContextDietTrimRail(TrimConfig(bash_max_chars=2000), spill_root=tmp_path)
    minified = "Command: cat bundle.js\nStdout: " + "a" * 5000 + "\n\nStderr: (empty)\nExit Code: 0"
    ctx = _ctx("bash", minified, tool_args={"command": "cat bundle.js"})
    original = ctx.inputs.tool_msg
    await rail.after_tool_call(ctx)
    msg = ctx.inputs.tool_msg
    assert msg is not original
    assert "chars clipped" in msg.content and "long line(s) clipped" in msg.content
    assert len(msg.content) < 2600
    spill = tmp_path / "sess-1" / "bash_call_1.txt"
    assert spill.read_text() == original.content
    assert str(spill) in msg.content
    assert msg.metadata["context_diet"]["spill_path"] == str(spill)
    assert msg.content.splitlines()[-2] == "Exit Code: 0" or "Exit Code: 0" in msg.content
