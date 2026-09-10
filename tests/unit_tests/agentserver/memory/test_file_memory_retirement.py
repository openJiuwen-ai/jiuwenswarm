"""Workspace creation must not revive the retired file-memory backend."""

from pathlib import Path

import pytest
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.core.sys_operation import LocalWorkConfig, SysOperation, SysOperationCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.schema.deep_agent_spec import WorkspaceSpec
from openjiuwen.harness.workspace.workspace import Workspace

from jiuwenswarm.agents.harness.common import memory_rpc
from jiuwenswarm.common import utils
from jiuwenswarm.server.runtime.agent_adapter.interface_code import _build_coding_memory_directory_node


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["cn", "en"])
@pytest.mark.parametrize("serialized", [False, True], ids=["adapter", "team-spec"])
async def test_workspace_initialization_keeps_coding_memory_without_legacy_files(tmp_path, language, serialized):
    if serialized:
        workspace = WorkspaceSpec(root_path=str(tmp_path), language=language).build()
    else:
        workspace = Workspace(root_path=str(tmp_path), language=language)
    for node in ("USER.md", "memory", "daily_memory"):
        assert workspace.get_directory(node) is None
        assert workspace.get_node_path(node) is None
    # Code adapters explicitly add their project index in either language.
    workspace.set_directory(_build_coding_memory_directory_node("coding_memory", description="Project index"))
    assert workspace.get_directory("coding_memory") is not None
    operation = SysOperation(SysOperationCard(
        id="retired-memory-fs", work_config=LocalWorkConfig(sandbox_root=[str(tmp_path)]),
    ))
    card = AgentCard(id="retired-memory-test", name="retired-memory-test")
    agent = DeepAgent(card).configure(DeepAgentConfig(card=card, workspace=workspace, sys_operation=operation))
    await agent.init_workspace()
    assert (tmp_path / "coding_memory" / "MEMORY.md").is_file()
    assert (tmp_path / "HEARTBEAT.md").is_file()
    assert not (tmp_path / "USER.md").exists()
    assert not (tmp_path / "memory").exists()


def test_legacy_layout_migration_leaves_old_memory_in_place(tmp_path):
    source = tmp_path / "agent" / "memory"
    source.mkdir(parents=True)
    for name in ("USER.md", "MEMORY.md", "2026-09-10.md"):
        (source / name).write_text("old memory", encoding="utf-8")
    utils._migrate_legacy_workspace(tmp_path)
    assert not (tmp_path / "agent" / "workspace" / "memory").exists()
    assert all(path.read_text() == "old memory" for path in source.iterdir())


@pytest.mark.asyncio
async def test_memory_status_does_not_initialize_a_file_index(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_rpc, "get_config", lambda: {"memory": {"engine": "external"}})
    status = await memory_rpc.handle_memory_status(str(tmp_path), "agent", {"detailed": True})
    assert "index" not in status
    assert not (tmp_path / "memory").exists()
    assert not list(Path(tmp_path).rglob("*.db"))
