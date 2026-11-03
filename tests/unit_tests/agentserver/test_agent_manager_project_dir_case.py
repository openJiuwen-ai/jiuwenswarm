# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime paths retain their spelling while cache identities stay normalized."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.runtime import agent_manager as manager_module


@pytest.fixture
def facade(monkeypatch):
    agent = Mock()
    agent.create_instance = AsyncMock()
    agent.cleanup = AsyncMock()
    agent.prepare_session = AsyncMock()
    factory = Mock(return_value=agent)
    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.server.runtime.agent_adapter.interface",
        SimpleNamespace(JiuWenSwarm=factory),
    )
    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.server.runtime.agent_adapter.agent_adapters",
        SimpleNamespace(resolve_sdk_choice=lambda: "test"),
    )
    return agent, factory


@pytest.mark.asyncio
@pytest.mark.parametrize("relative", [False, True])
async def test_runtime_path_preserves_case_across_cache_lookup_and_recreation(
    monkeypatch, tmp_path, facade, relative,
):
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "Straße MiXeD Project"
    project.mkdir()
    declared = project.name if relative else str(project)
    agent, factory = facade
    manager = manager_module.AgentManager()

    assert await manager.get_agent("tui", project_dir=declared) is agent
    config = agent.create_instance.call_args.args[0]
    assert config["project_dir"] == str(project)
    assert await manager.get_agent("tui", project_dir=str(project).swapcase()) is agent
    assert manager.get_agent_nowait("tui", mode="agent", project_dir=str(project).swapcase()) is agent
    factory.assert_called_once()

    await manager.recreate_agent("tui")

    assert factory.call_count == 2
    assert agent.create_instance.call_args.args[0]["project_dir"] == str(project)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["project_dir", "workspace_dir", "metadata"])
async def test_auto_request_prepares_session_with_original_path_case(
    monkeypatch, tmp_path, facade, field,
):
    # Exercise Windows-like folding on every platform, including Linux CI.
    monkeypatch.setattr(os.path, "normcase", str.casefold)
    monkeypatch.setattr(
        manager_module, "get_config",
        lambda: {"permissions": {"enabled": True, "mode": "auto"}},
    )
    project = tmp_path / "Straße MiXeD Project"
    project.mkdir()
    params = {"mode": "agent"}
    metadata = {}
    if field == "metadata":
        metadata["project_dir"] = str(project)
    else:
        params[field] = str(project)
    request = SimpleNamespace(channel_id="web", session_id="", params=params, metadata=metadata)
    manager = manager_module.AgentManager()
    agent, _factory = facade
    get_agent = AsyncMock(return_value=agent)
    monkeypatch.setattr(manager, "get_agent", get_agent)

    assert await manager.get_agent_for_request(request) is agent

    assert get_agent.call_args.kwargs["project_dir"] == str(project.resolve())
    assert agent.prepare_session.call_args.kwargs["project_dir"] == str(project.resolve())
