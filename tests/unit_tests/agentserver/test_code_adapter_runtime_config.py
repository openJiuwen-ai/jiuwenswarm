# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for code-adapter per-request runtime configuration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import (
    BrowserTaskPromptRail,
)
from jiuwenswarm.agents.harness.common.tools import user_todo_tool
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)


@pytest.mark.asyncio
async def test_runtime_config_updates_browser_prompt_and_permission_rails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Per-request setup supports current browser and permission rail APIs."""
    adapter = JiuwenSwarmCodeAdapter()
    adapter._instance = SimpleNamespace(
        ability_manager=SimpleNamespace(add=MagicMock()),
    )
    adapter._project_dir = str(tmp_path)
    adapter._workspace_dir = str(tmp_path)
    adapter._agent_workspace_dir = str(tmp_path)
    adapter._runtime_prompt_rail = None
    adapter._subagent_rail = BrowserTaskPromptRail()
    adapter._project_memory_rail = None
    adapter._permission_rail = MagicMock()

    update_rails = AsyncMock()
    monkeypatch.setattr(adapter, "_seed_runtime_cwd", MagicMock())
    monkeypatch.setattr(adapter, "_resolve_output_language", lambda: "cn")
    monkeypatch.setattr(adapter, "_write_runtime_state", MagicMock())
    monkeypatch.setattr(adapter, "_update_rails_for_mode", update_rails)
    monkeypatch.setattr(adapter, "_set_user_interaction_enabled", AsyncMock())
    monkeypatch.setattr(adapter, "_update_tools_for_mode", AsyncMock())
    monkeypatch.setattr(adapter, "_update_session_tools", AsyncMock())
    monkeypatch.setattr(adapter, "_refresh_acp_runtime_tools", MagicMock())
    monkeypatch.setattr(adapter, "_update_prompt_for_mode", MagicMock())
    monkeypatch.setattr(adapter, "_register_shared_tool", MagicMock())
    monkeypatch.setattr(user_todo_tool, "set_global_workspace_dir", MagicMock())
    monkeypatch.setattr(user_todo_tool, "set_global_channel_id", MagicMock())
    monkeypatch.setattr(user_todo_tool, "get_decorated_tools", lambda: [])

    await adapter._update_runtime_config(
        adapter._RuntimeConfig(
            session_id="tui_session_1",
            request_id="request_1",
            mode="code",
            channel_id="tui",
            project_dir=str(tmp_path),
        )
    )

    update_rails.assert_awaited_once_with("code")
    adapter._permission_rail.set_trusted_dirs.assert_called_once_with(
        [str(tmp_path.resolve())]
    )
