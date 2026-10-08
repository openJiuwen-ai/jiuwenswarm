# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Safety checks for request-scoped permissions and root tool selection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.runtime.run_permissions import RUN_PERMISSIONS, overlay_run_permissions
from jiuwenswarm.runtime.tool_allowlist import install_tool_allowlist
from jiuwenswarm.agents.harness.common.rails.permissions.permission_interrupt_rail import (
    JiuwenSwarmPermissionInterruptRail,
)


@pytest.mark.unit
def test_run_permissions_preserve_installed_deny_and_config_snapshot() -> None:
    installed = {
        "tools": {"locked": "deny", "read_file": "ask", "write_file": "allow"},
        "deny_tools": ["locked"],
        "ask_tools": ["read_file"],
        "allow_tools": ["write_file"],
        "file_guard": {"enabled": True},
    }
    token = RUN_PERMISSIONS.set({
        "locked": "allow", "read_file": "allow", "write_file": "ask",
        "run_shell": "deny",
    })
    try:
        effective = overlay_run_permissions(installed)
    finally:
        RUN_PERMISSIONS.reset(token)
    assert effective["tools"] == {
        "locked": "deny", "read_file": "allow", "write_file": "ask",
        "run_shell": "deny",
    }
    assert effective["file_guard"] == installed["file_guard"]
    assert installed["tools"]["read_file"] == "ask"
    assert "run_shell" not in installed["tools"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_root_tool_allowlist_filters_discovery_and_blocks_execution() -> None:
    class Manager:
        def __init__(self):
            self.calls = []

        def list(self):
            return [SimpleNamespace(name=name) for name in ("read_file", "write_file")]

        async def list_tool_info(self):
            return self.list()

        async def execute(self, _ctx, tool_call, _session):
            self.calls.append(tool_call.name)
            return tool_call.name

    manager = Manager()
    install_tool_allowlist(manager, ["read_file"])
    assert [card.name for card in manager.list()] == ["read_file"]
    assert [info.name for info in await manager.list_tool_info()] == ["read_file"]
    with pytest.raises(ValueError, match="allowlist denied"):
        await manager.execute(None, SimpleNamespace(name="write_file"), None)
    assert manager.calls == []
    assert await manager.execute(None, SimpleNamespace(name="read_file"), None) == "read_file"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_permission_rail_rejects_run_deny_before_host_approval() -> None:
    rail = object.__new__(JiuwenSwarmPermissionInterruptRail)
    rail.run_permission_levels = {"write_file": "deny"}
    rail._normalize_tool_name = lambda name: name
    rail.reject = lambda *, tool_result: tool_result

    result = await rail.resolve_interrupt(
        None, SimpleNamespace(name="write_file"), None
    )
    assert result == "[PERMISSION_DENIED] run policy"
