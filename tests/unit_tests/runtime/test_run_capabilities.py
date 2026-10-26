# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Safety checks for request-scoped permissions and root tool selection."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from jiuwenswarm.runtime.run_permissions import RUN_PERMISSIONS, overlay_run_permissions
from jiuwenswarm.runtime.run_context import CURRENT_RUN_CONTEXT, RunContext
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


@pytest.mark.parametrize("levels,expected", [
    (None, {"get_current_goal": "deny", "mcp_exec_command": "deny", "locked": "deny"}),
    ({}, {"get_current_goal": "allow", "locked": "deny"}),
    ({"get_current_goal": "ask", "locked": "allow"}, {"get_current_goal": "ask", "locked": "deny"}),
])
def test_run_permissions_fallback_preserves_precedence_and_canonical_names(levels, expected):
    installed = {"tools": {"get_current_goal": "allow", "locked": "deny"},
                 "deny_tools": ["locked"], "file_guard": {"enabled": True}}
    original = deepcopy(installed)
    context = RunContext(request_id="request", tool_permissions=(
        ("get_current_goal", "deny"), ("exec_command", "deny"), ("locked", "allow"),
    ))
    context_token = CURRENT_RUN_CONTEXT.set(context)
    run_token = RUN_PERMISSIONS.set(levels)
    try:
        effective = overlay_run_permissions(installed)
        assert effective["tools"] == expected
        assert effective["file_guard"] == original["file_guard"]
        assert installed == original
        assert CURRENT_RUN_CONTEXT.get() is context and RUN_PERMISSIONS.get() is levels
    finally:
        RUN_PERMISSIONS.reset(run_token)
        CURRENT_RUN_CONTEXT.reset(context_token)


@pytest.mark.parametrize("tool_permissions", [None, ()])
def test_empty_run_context_does_not_add_policy(tool_permissions):
    installed = {"tools": {"get_current_goal": "ask"}}
    context_token = CURRENT_RUN_CONTEXT.set(RunContext(tool_permissions=tool_permissions))
    run_token = RUN_PERMISSIONS.set(None)
    try:
        assert overlay_run_permissions(installed) is installed
    finally:
        RUN_PERMISSIONS.reset(run_token)
        CURRENT_RUN_CONTEXT.reset(context_token)


@pytest.mark.parametrize("tool_permissions", [
    (("exec_command", "deny"), ("mcp_exec_command", "allow")), ((" ", "deny"),),
])
def test_ambiguous_run_context_permissions_fail_closed(tool_permissions):
    context_token = CURRENT_RUN_CONTEXT.set(RunContext(tool_permissions=tool_permissions))
    run_token = RUN_PERMISSIONS.set(None)
    try:
        with pytest.raises(ValueError, match="ambiguous"):
            overlay_run_permissions({"tools": {}})
    finally:
        RUN_PERMISSIONS.reset(run_token)
        CURRENT_RUN_CONTEXT.reset(context_token)


@pytest.mark.asyncio
async def test_existing_workers_keep_run_permissions_isolated():
    installed = {"tools": {"get_current_goal": "ask"}}
    release = asyncio.Event()
    tasks = []
    parent_context = CURRENT_RUN_CONTEXT.get()
    parent_levels = RUN_PERMISSIONS.get()

    async def worker():
        await release.wait()
        assert RUN_PERMISSIONS.get() is None
        return overlay_run_permissions(installed)["tools"]["get_current_goal"]

    for level in ("deny", "allow"):
        context_token = CURRENT_RUN_CONTEXT.set(RunContext(
            request_id=level, tool_permissions=(("get_current_goal", level),),
        ))
        run_token = RUN_PERMISSIONS.set(None)
        try:
            tasks.append(asyncio.create_task(worker()))
        finally:
            RUN_PERMISSIONS.reset(run_token)
            CURRENT_RUN_CONTEXT.reset(context_token)
    # This later request binding cannot change either pre-existing task's context.
    run_token = RUN_PERMISSIONS.set({"get_current_goal": "ask"})
    try:
        release.set()
        assert await asyncio.gather(*tasks) == ["deny", "allow"]
        assert installed == {"tools": {"get_current_goal": "ask"}}
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        RUN_PERMISSIONS.reset(run_token)
    assert CURRENT_RUN_CONTEXT.get() is parent_context
    assert RUN_PERMISSIONS.get() is parent_levels
