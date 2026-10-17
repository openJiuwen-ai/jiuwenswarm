# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for AgentMdPromptRail and project instruction file lookup."""

# pylint: disable=protected-access

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ToolCallInputs,
)
from openjiuwen.harness.prompts.prompt_attachment_manager import (
    PromptAttachmentManager,
)

from jiuwenswarm.agents.harness.code.rails.agent_md_prompt_rail import (
    SECTION_NAME,
    AgentMdPromptRail,
    ProjectInstructionFile,
    find_project_instruction_file,
)


class FakeSession:
    def __init__(self, session_id: str = "sess-agent-md") -> None:
        self._session_id = session_id

    def get_session_id(self) -> str:
        return self._session_id


def _ctx(
    session_id: str = "sess-agent-md",
    *,
    tool_name: str = "",
) -> AgentCallbackContext:
    return AgentCallbackContext(
        agent=None,
        session=FakeSession(session_id),
        inputs=ToolCallInputs(tool_name=tool_name),
    )


class FakeModelContext:
    """Duck-typed ModelContext capturing messages appended by the manager."""

    def __init__(self) -> None:
        self.messages: list = []

    def get_messages(self, with_history: bool = True) -> list:
        return list(self.messages)

    async def add_messages(self, message) -> None:
        self.messages.append(message)


class FakeAgent:
    def __init__(self, manager: PromptAttachmentManager | None) -> None:
        self.prompt_attachment_manager = manager


def _write(directory: Path, name: str, content: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(content, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# find_project_instruction_file
# ---------------------------------------------------------------------------


def test_find_prefers_nearest_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _write(root / "sub", "agent.md", "# sub")
    _write(root, "agent.md", "# root")
    hit = find_project_instruction_file(root / "sub")
    assert hit is not None
    assert hit.parent == root / "sub"


def test_find_walks_up_within_three_levels(tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    _write(root, "agent.md", "# root")
    hit = find_project_instruction_file(nested)
    assert hit is not None
    assert hit.parent == root


def test_find_gives_up_after_three_levels(tmp_path: Path) -> None:
    root = tmp_path / "root"
    deep = root / "l1" / "l2" / "l3"
    deep.mkdir(parents=True)
    # root 是第 4 层（l3 → l2 → l1 → root），超出三层上限。
    _write(root, "agent.md", "# too far")
    assert find_project_instruction_file(deep) is None


@pytest.mark.parametrize("name", ["AGENT.md", "Agent.md", "agent.md"])
def test_find_matches_case_insensitively(tmp_path: Path, name: str) -> None:
    _write(tmp_path, name, "# instructions")
    hit = find_project_instruction_file(tmp_path)
    assert hit is not None
    assert hit.name == name


def test_find_category_priority_follows_enum_order(tmp_path: Path) -> None:
    _write(tmp_path, "claude.md", "# claude")
    _write(tmp_path, "agent.md", "# agent")
    hit = find_project_instruction_file(tmp_path)
    assert hit is not None
    assert hit.name.lower() == ProjectInstructionFile.AGENT_MD.value

    claude_only = find_project_instruction_file(
        tmp_path, categories=(ProjectInstructionFile.CLAUDE_MD,)
    )
    assert claude_only is not None
    assert claude_only.name.lower() == ProjectInstructionFile.CLAUDE_MD.value


def test_find_skips_directory_with_instruction_name(tmp_path: Path) -> None:
    (tmp_path / "agent.md").mkdir()
    parent_file = _write(tmp_path.parent, "agent.md", "# parent")
    hit = find_project_instruction_file(tmp_path)
    assert hit == parent_file


def test_find_returns_none_without_any_candidate(tmp_path: Path) -> None:
    # 工作目录放在三层深的空目录里，确保向上扫描不会越过 tmp_path
    # 触及 pytest 共享临时根中其他用例创建的 agent.md。
    deep_empty = tmp_path / "a" / "b" / "c"
    deep_empty.mkdir(parents=True)
    assert find_project_instruction_file(deep_empty) is None
    assert find_project_instruction_file(deep_empty / "missing") is None


# ---------------------------------------------------------------------------
# AgentMdPromptRail
# ---------------------------------------------------------------------------


def _make_rail(tmp_path: Path, *, max_chars: int = 12_000) -> tuple[AgentMdPromptRail, PromptAttachmentManager, FakeContext]:
    manager = PromptAttachmentManager(language="en")
    rail = AgentMdPromptRail(max_chars=max_chars)
    rail.init(FakeAgent(manager))
    rail.set_runtime_paths(cwd=str(tmp_path))
    ctx = _ctx()
    return rail, manager, ctx


@pytest.mark.asyncio
async def test_rail_upserts_instruction_attachment(tmp_path: Path) -> None:
    _write(tmp_path, "agent.md", "# build rules")
    rail, manager, ctx = _make_rail(tmp_path)

    await rail.before_model_call(ctx)

    items = await manager.collect_for_session(ctx.session.get_session_id())
    assert len(items) == 1
    assert items[0].section == SECTION_NAME
    assert "# build rules" in (items[0].content or "")
    assert "agent.md" in (items[0].content or "")


@pytest.mark.asyncio
async def test_rail_clears_section_when_file_missing(tmp_path: Path) -> None:
    empty_cwd = tmp_path / "a" / "b"
    empty_cwd.mkdir(parents=True)
    rail, manager, ctx = _make_rail(empty_cwd)

    await rail.before_model_call(ctx)

    assert await manager.collect_for_session(ctx.session.get_session_id()) == []


@pytest.mark.asyncio
async def test_rail_skips_empty_instruction_file(tmp_path: Path) -> None:
    _write(tmp_path, "agent.md", "   \n")
    rail, manager, ctx = _make_rail(tmp_path)

    await rail.before_model_call(ctx)

    assert await manager.collect_for_session(ctx.session.get_session_id()) == []


@pytest.mark.asyncio
async def test_rail_truncates_oversized_content(tmp_path: Path) -> None:
    _write(tmp_path, "agent.md", "x" * 500)
    rail, manager, ctx = _make_rail(tmp_path, max_chars=100)

    await rail.before_model_call(ctx)

    items = await manager.collect_for_session(ctx.session.get_session_id())
    assert len(items) == 1
    content = items[0].content or ""
    assert "[Project instructions truncated" in content


@pytest.mark.asyncio
async def test_history_snapshot_once_then_delta(tmp_path: Path) -> None:
    """首个生效上下文写全量快照，内容未变不追加，变化只写增量。"""
    target = _write(tmp_path, "agent.md", "# v1")
    rail, manager, ctx = _make_rail(tmp_path)
    model_context = FakeModelContext()
    session_id = ctx.session.get_session_id()

    await rail.before_model_call(ctx)
    await manager.sync_to_context(model_context, session_id)
    assert len(model_context.messages) == 1
    assert "# v1" in model_context.messages[0].content

    # 内容未变：再次 sync 不应产生新消息。
    await rail.before_model_call(ctx)
    await manager.sync_to_context(model_context, session_id)
    assert len(model_context.messages) == 1

    # 写类工具后重读文件，内容变化 → 只追加 delta。
    target.write_text("# v2", encoding="utf-8")
    await rail.after_tool_call(_ctx(tool_name="edit_file"))
    await rail.before_model_call(ctx)
    await manager.sync_to_context(model_context, session_id)
    assert len(model_context.messages) == 2
    assert "# v2" in model_context.messages[1].content


@pytest.mark.asyncio
async def test_history_resends_snapshot_after_context_loss(tmp_path: Path) -> None:
    """上下文压缩丢掉快照后，下一轮自动重发全量。"""
    _write(tmp_path, "agent.md", "# rules")
    rail, manager, ctx = _make_rail(tmp_path)
    model_context = FakeModelContext()
    session_id = ctx.session.get_session_id()

    await rail.before_model_call(ctx)
    await manager.sync_to_context(model_context, session_id)
    assert len(model_context.messages) == 1

    # 模拟上下文压缩：历史被清空。
    model_context.messages.clear()

    await rail.before_model_call(ctx)
    await manager.sync_to_context(model_context, session_id)
    assert len(model_context.messages) == 1
    assert "# rules" in model_context.messages[0].content


@pytest.mark.asyncio
async def test_rail_follows_cwd_switch(tmp_path: Path) -> None:
    first = tmp_path / "first"
    _write(first, "agent.md", "# first")
    rail, manager, ctx = _make_rail(first)
    session_id = ctx.session.get_session_id()

    await rail.before_model_call(ctx)
    items = await manager.collect_for_session(session_id)
    assert items and "# first" in (items[0].content or "")

    # 切到没有指令文件的目录 → section 被清除。
    # 目录深度保证三层扫描止于 tmp_path，不会命中其他用例的文件。
    empty_cwd = tmp_path / "s1" / "s2" / "s3"
    empty_cwd.mkdir(parents=True)
    rail.set_runtime_paths(cwd=str(empty_cwd))
    await rail.before_model_call(ctx)
    assert await manager.collect_for_session(session_id) == []


@pytest.mark.asyncio
async def test_rail_without_manager_is_inert(tmp_path: Path) -> None:
    _write(tmp_path, "agent.md", "# rules")
    rail = AgentMdPromptRail()
    rail.init(FakeAgent(None))
    rail.set_runtime_paths(cwd=str(tmp_path))

    await rail.before_model_call(_ctx())

    assert rail._attachment_manager is None


# ---------------------------------------------------------------------------
# bash workdir 目录级注入
# ---------------------------------------------------------------------------


def _bash_ctx(
    session_id: str,
    workdir: Path | None,
    *,
    tool_result: object = "cmd output",
    tool_msg_content: str = "cmd output",
) -> AgentCallbackContext:
    ctx = _ctx(session_id, tool_name="bash")
    ctx.inputs.tool_args = {"workdir": str(workdir)} if workdir else {}
    ctx.inputs.tool_result = tool_result
    ctx.inputs.tool_msg = SimpleNamespace(content=tool_msg_content)
    return ctx


@pytest.mark.asyncio
async def test_bash_cwd_appends_instructions_to_tool_result(tmp_path: Path) -> None:
    tools_dir = tmp_path / "tools"
    _write(tools_dir, "agent.md", "# AGENTMD::TOOLS")
    rail = AgentMdPromptRail()
    rail.init(FakeAgent(None))
    rail.set_runtime_paths(cwd=str(tmp_path / "session"))

    ctx = _bash_ctx("sess-bash", tools_dir)
    await rail.after_tool_call(ctx)

    content = ctx.inputs.tool_msg.content
    assert content.startswith("cmd output")
    assert "<system-reminder>" in content
    assert "AGENTMD::TOOLS" in content
    assert "Project instructions (from" in content
    assert content.endswith("</system-reminder>")
    assert ctx.inputs.tool_result.endswith("</system-reminder>")


@pytest.mark.asyncio
async def test_bash_cwd_appends_once_per_directory(tmp_path: Path) -> None:
    tools_dir = tmp_path / "tools"
    _write(tools_dir, "agent.md", "# AGENTMD::TOOLS")
    rail = AgentMdPromptRail()
    rail.init(FakeAgent(None))
    rail.set_runtime_paths(cwd=str(tmp_path / "session"))

    first = _bash_ctx("sess-bash", tools_dir, tool_msg_content="out1")
    await rail.after_tool_call(first)
    once = first.inputs.tool_msg.content

    second = _bash_ctx("sess-bash", tools_dir, tool_msg_content="out2")
    await rail.after_tool_call(second)
    assert second.inputs.tool_msg.content == "out2"


@pytest.mark.asyncio
async def test_bash_cwd_same_as_session_cwd_skips(tmp_path: Path) -> None:
    _write(tmp_path, "agent.md", "# rules")
    rail = AgentMdPromptRail()
    rail.init(FakeAgent(None))
    rail.set_runtime_paths(cwd=str(tmp_path))

    ctx = _bash_ctx("sess-bash", tmp_path)
    await rail.after_tool_call(ctx)

    assert ctx.inputs.tool_msg.content == "cmd output"


@pytest.mark.asyncio
async def test_bash_cwd_without_own_agent_md_skips_even_if_parent_has_one(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "agent.md", "# parent")
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    rail = AgentMdPromptRail()
    rail.init(FakeAgent(None))
    rail.set_runtime_paths(cwd=str(tmp_path / "session"))

    ctx = _bash_ctx("sess-bash", plain_dir)
    await rail.after_tool_call(ctx)

    assert ctx.inputs.tool_msg.content == "cmd output"


@pytest.mark.asyncio
async def test_bash_cwd_skips_when_file_is_session_attachment(tmp_path: Path) -> None:
    """会话 attachment 已注入同一文件时，bash 追加去重。"""
    _write(tmp_path, "agent.md", "# root rules")
    session_cwd = tmp_path / "a" / "b"
    session_cwd.mkdir(parents=True)
    rail, manager, ctx = _make_rail(session_cwd)
    session_id = ctx.session.get_session_id()

    await rail.before_model_call(ctx)
    items = await manager.collect_for_session(session_id)
    assert items and "# root rules" in (items[0].content or "")

    bash_ctx = _bash_ctx(session_id, tmp_path, tool_msg_content="out")
    await rail.after_tool_call(bash_ctx)
    assert bash_ctx.inputs.tool_msg.content == "out"


@pytest.mark.asyncio
async def test_bash_non_string_tool_result_only_touches_tool_msg(
    tmp_path: Path,
) -> None:
    tools_dir = tmp_path / "tools"
    _write(tools_dir, "agent.md", "# AGENTMD::TOOLS")
    rail = AgentMdPromptRail()
    rail.init(FakeAgent(None))
    rail.set_runtime_paths(cwd=str(tmp_path / "session"))

    structured_result = SimpleNamespace(success=True, data={"content": "cmd output"})
    ctx = _bash_ctx("sess-bash", tools_dir, tool_result=structured_result)
    await rail.after_tool_call(ctx)

    assert "AGENTMD::TOOLS" in ctx.inputs.tool_msg.content
    # 非字符串 tool_result（ToolOutput 形态）保持原样，只改 tool_msg。
    assert ctx.inputs.tool_result.data == {"content": "cmd output"}


@pytest.mark.asyncio
async def test_bash_without_workdir_skips(tmp_path: Path) -> None:
    rail = AgentMdPromptRail()
    rail.init(FakeAgent(None))
    rail.set_runtime_paths(cwd=str(tmp_path))

    ctx = _bash_ctx("sess-bash", None)
    await rail.after_tool_call(ctx)

    assert ctx.inputs.tool_msg.content == "cmd output"
