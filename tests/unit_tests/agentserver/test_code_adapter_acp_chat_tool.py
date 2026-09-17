# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for CodeAdapter ACP tool registration."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.coding_memory_paths import resolve_project_coding_memory_dir
from jiuwenswarm.server.runtime.agent_adapter import interface_code
from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter


class _FakeResourceMgr:
    def __init__(self) -> None:
        self._tools: dict[str, object] = {}

    def get_tool(self, tool_id: str) -> object | None:
        return self._tools.get(tool_id)

    def add_tool(self, tool: object) -> None:
        self._tools[tool.card.id] = tool


def test_code_adapter_builds_acp_chat_when_profile_configured(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_code.get_config",
        lambda: {
            "acp_agents": {"codex": {"command": "npx", "args": []}},
            "modes": {"code": {"tools": ["acp_chat"]}},
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_code.Runner",
        SimpleNamespace(resource_mgr=_FakeResourceMgr()),
    )

    cards = JiuwenSwarmCodeAdapter().build_code_tool_cards("agent-id")

    assert [card.name for card in cards] == ["acp_chat"]


def test_code_adapter_skips_acp_chat_without_profiles(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_code.get_config",
        lambda: {
            "acp_agents": {},
            "modes": {"code": {"tools": ["acp_chat"]}},
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_code.Runner",
        SimpleNamespace(resource_mgr=_FakeResourceMgr()),
    )

    cards = JiuwenSwarmCodeAdapter().build_code_tool_cards("agent-id")

    assert cards == []


def test_code_adapter_builds_coding_memory_rail_without_embedding_config(monkeypatch, tmp_path):
    created: dict[str, object] = {}

    class _FakeCodingMemoryRail:
        def __init__(self, *, coding_memory_dir, embedding_config, language):
            created["coding_memory_dir"] = coding_memory_dir
            created["embedding_config"] = embedding_config
            created["language"] = language

    monkeypatch.setattr(interface_code, "CodingMemoryRail", _FakeCodingMemoryRail)

    project_dir = tmp_path / "project"
    agent_workspace_dir = tmp_path / "agent_workspace"

    rail = interface_code.create_coding_memory_rail(
        project_dir=str(project_dir),
        agent_workspace_dir=str(agent_workspace_dir),
        config={"preferred_language": "zh", "embed": {}},
    )

    assert isinstance(rail, _FakeCodingMemoryRail)
    assert created["coding_memory_dir"] == resolve_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace_dir,
        project_dir=project_dir,
    )
    assert created["embedding_config"].model_name == "text-embedding-v3"
    assert created["embedding_config"].base_url == ""
    assert created["embedding_config"].api_key is None


def test_coding_memory_tools_use_system_storage_without_workspace_node(monkeypatch, tmp_path):
    """The dedicated tool context resolves global storage without a Workspace node."""
    from openjiuwen.core.memory.lite.coding_memory_tool_ops import (
        validate_coding_memory_path,
    )

    coding_memory_dir = str(tmp_path / "agent_workspace" / "coding_memory" / "project")
    rail = interface_code.CodingMemoryRail(
        coding_memory_dir=coding_memory_dir,
        embedding_config=SimpleNamespace(model_name="test", base_url="", api_key=None),
        language="en",
    )
    project_workspace = SimpleNamespace(get_node_path=lambda _name: None)
    rail.workspace = project_workspace
    captured: dict[str, object] = {}

    def create_tools(ctx, **_kwargs):
        captured["context"] = ctx
        return []

    monkeypatch.setattr(
        "openjiuwen.harness.rails.memory.coding_memory_rail.create_coding_memory_tools",
        create_tools,
    )

    rail._register_coding_memory_tools(SimpleNamespace(ability_manager=SimpleNamespace()))

    tool_context = captured["context"]
    assert rail.workspace is project_workspace
    assert tool_context.coding_memory_dir == coding_memory_dir
    is_valid, resolved = validate_coding_memory_path("preference.md", tool_context.workspace)
    assert is_valid
    assert resolved == str(tmp_path / "agent_workspace" / "coding_memory" / "project" / "preference.md")


@pytest.mark.asyncio
async def test_coding_memory_initialization_does_not_block_and_is_deduplicated(monkeypatch):
    rail = interface_code.CodingMemoryRail(
        coding_memory_dir="/tmp/coding-memory",
        embedding_config=SimpleNamespace(model_name="test", base_url="", api_key=None),
        language="en",
    )
    release = asyncio.Event()
    calls = 0

    async def initialize(_ctx):
        nonlocal calls
        calls += 1
        await release.wait()

    monkeypatch.setattr(rail, "_init_coding_memory_manager", initialize)
    ctx = SimpleNamespace(
        inputs=SimpleNamespace(is_cron=lambda: False, is_heartbeat=lambda: False)
    )

    await asyncio.wait_for(rail.before_invoke(ctx), timeout=0.2)
    first_task = rail._manager_init_task
    assert first_task is not None
    await asyncio.sleep(0)
    await rail.before_invoke(ctx)

    assert rail._manager_init_task is first_task
    assert calls == 1

    release.set()
    await asyncio.wait_for(first_task, timeout=0.2)


@pytest.mark.asyncio
async def test_coding_memory_initialization_failure_degrades_without_retry(monkeypatch):
    rail = interface_code.CodingMemoryRail(
        coding_memory_dir="/tmp/coding-memory",
        embedding_config=SimpleNamespace(model_name="test", base_url="", api_key=None),
        language="en",
    )
    ctx = SimpleNamespace(
        agent=SimpleNamespace(card=SimpleNamespace(id="test-agent")),
        inputs=SimpleNamespace(is_cron=lambda: False, is_heartbeat=lambda: False),
    )
    initializer = AsyncMock(side_effect=RuntimeError("embedding unavailable"))
    monkeypatch.setattr(
        "openjiuwen.harness.rails.memory.coding_memory_rail.init_memory_manager_async",
        initializer,
    )

    await rail.before_invoke(ctx)
    first_task = rail._manager_init_task
    assert first_task is not None
    await first_task

    assert rail._manager_initialized
    assert initializer.await_count == 1

    await rail.before_invoke(ctx)
    assert rail._manager_init_task is first_task
    assert initializer.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_initialization_cannot_reset_reinitialized_state(monkeypatch):
    rail = interface_code.CodingMemoryRail(
        coding_memory_dir="/tmp/coding-memory",
        embedding_config=SimpleNamespace(model_name="test", base_url="", api_key=None),
        language="en",
    )
    ctx = SimpleNamespace(
        agent=SimpleNamespace(card=SimpleNamespace(id="test-agent")),
        inputs=SimpleNamespace(is_cron=lambda: False, is_heartbeat=lambda: False),
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def initialize(**_kwargs):
        started.set()
        await release.wait()
        return None

    monkeypatch.setattr(
        "openjiuwen.harness.rails.memory.coding_memory_rail.init_memory_manager_async",
        initialize,
    )

    await rail.before_invoke(ctx)
    old_task = rail._manager_init_task
    assert old_task is not None
    await asyncio.wait_for(started.wait(), timeout=0.2)

    rail.uninit(SimpleNamespace())
    await asyncio.gather(old_task, return_exceptions=True)
    assert rail._manager_init_task is None
    assert not rail._manager_initialized

    release.set()
    await rail.before_invoke(ctx)
    new_task = rail._manager_init_task
    assert new_task is not None
    assert new_task is not old_task
    await new_task
    assert rail._manager_initialized
