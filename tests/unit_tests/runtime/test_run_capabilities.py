# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Safety checks for request-scoped permissions and root tool selection."""

from __future__ import annotations

import asyncio
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
    token = RUN_PERMISSIONS.set(
        {
            "locked": "allow",
            "read_file": "allow",
            "write_file": "ask",
            "run_shell": "deny",
        }
    )
    try:
        effective = overlay_run_permissions(installed)
    finally:
        RUN_PERMISSIONS.reset(token)
    assert effective["tools"] == {
        "locked": "deny",
        "read_file": "allow",
        "write_file": "ask",
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
    assert (
        await manager.execute(None, SimpleNamespace(name="read_file"), None)
        == "read_file"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_allowlist_blocks_late_registered_tools_and_batch_execution():
    class Manager:
        def __init__(self):
            self.names = ["read_file"]
            self.calls = []

        def list(self):
            return [SimpleNamespace(name=name) for name in self.names]

        async def list_tool_info(self):
            return self.list()

        async def execute(self, _ctx, calls, _session):
            self.calls.append(calls)
            return calls

    restricted = Manager()
    ordinary = Manager()
    install_tool_allowlist(restricted, [])
    restricted.names.extend(["write_file", "host_lookup", "mcp_tool"])
    assert restricted.list() == []
    assert await restricted.list_tool_info() == []
    for calls in (
        SimpleNamespace(name="write_file"),
        [SimpleNamespace(name="read_file"), SimpleNamespace(name="mcp_tool")],
    ):
        with pytest.raises(ValueError, match="allowlist denied"):
            await restricted.execute(None, calls, None)
    assert restricted.calls == []
    assert ordinary.list()[0].name == "read_file"


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


@pytest.mark.unit
@pytest.mark.parametrize("current,fallback,expected", [
    (None, {"get_current_goal": "deny", "locked": "allow"}, "deny"),
    ({}, {"get_current_goal": "deny"}, "allow"),
    ({"get_current_goal": "ask"}, {"get_current_goal": "deny"}, "ask"),
    (None, None, "allow"),
])
@pytest.mark.asyncio
async def test_auto_callback_restores_adapter_permissions(current, fallback, expected):
    from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.before_tool import (
        AutoPermissionBeforeToolMixin,
    )

    installed = {"tools": {"get_current_goal": "allow", "locked": "deny"}}

    async def evaluate():
        assert RUN_PERMISSIONS.get() == (fallback if current is None else current)
        return overlay_run_permissions(installed)

    rail = SimpleNamespace(base_rail=SimpleNamespace(), run_permission_levels=fallback, _before_tool_call_impl=evaluate)
    token = RUN_PERMISSIONS.set(current)
    try:
        effective = await AutoPermissionBeforeToolMixin.before_tool_call(rail)
        assert effective["tools"] == {"get_current_goal": expected, "locked": "deny"}
        assert installed["tools"] == {"get_current_goal": "allow", "locked": "deny"}
        assert RUN_PERMISSIONS.get() is current
        assert rail.run_permission_levels is fallback
    finally:
        RUN_PERMISSIONS.reset(token)


@pytest.mark.unit
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
@pytest.mark.asyncio
async def test_auto_callback_cleans_request_permissions_on_failure(error):
    from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.before_tool import (
        AutoPermissionBeforeToolMixin,
    )

    async def fail():
        assert RUN_PERMISSIONS.get() == {"get_current_goal": "deny"}
        raise error()

    rail = SimpleNamespace(base_rail=SimpleNamespace(), run_permission_levels={"get_current_goal": "deny"}, _before_tool_call_impl=fail)
    token = RUN_PERMISSIONS.set(None)
    try:
        with pytest.raises(error):
            await AutoPermissionBeforeToolMixin.before_tool_call(rail)
        assert RUN_PERMISSIONS.get() is None
    finally:
        RUN_PERMISSIONS.reset(token)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_auto_callback_keeps_existing_workers_and_next_requests_isolated():
    from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.before_tool import (
        AutoPermissionBeforeToolMixin,
    )

    release = asyncio.Event()

    async def evaluate():
        await release.wait()
        return overlay_run_permissions({"tools": {"get_current_goal": "ask"}})["tools"]["get_current_goal"]

    rails = [SimpleNamespace(base_rail=SimpleNamespace(), run_permission_levels={"get_current_goal": level}, _before_tool_call_impl=evaluate)
             for level in ("deny", "allow")]
    parent = RUN_PERMISSIONS.set(None)
    tasks = [asyncio.create_task(AutoPermissionBeforeToolMixin.before_tool_call(rail)) for rail in rails]
    token = RUN_PERMISSIONS.set({"get_current_goal": "ask"})
    try:
        release.set()
        assert await asyncio.gather(*tasks) == ["deny", "allow"]
        assert RUN_PERMISSIONS.get() == {"get_current_goal": "ask"}
        rails[0].run_permission_levels = None
        RUN_PERMISSIONS.reset(token)
        token = RUN_PERMISSIONS.set(None)
        assert await AutoPermissionBeforeToolMixin.before_tool_call(rails[0]) == "ask"
        assert RUN_PERMISSIONS.get() is None
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        RUN_PERMISSIONS.reset(token)
        RUN_PERMISSIONS.reset(parent)
