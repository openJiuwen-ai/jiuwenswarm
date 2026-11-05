"""Run-scoped permission choices must not affect resident channels."""

from __future__ import annotations

from types import SimpleNamespace
import asyncio
from contextvars import Context
import json

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions import root_permission_queue_rail
from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
    TOOL_PERMISSION_CHANNEL_ID, TOOL_PERMISSION_REQUEST_ID,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.run_permissions import RUN_PERMISSIONS, overlay_run_permissions
from jiuwenswarm.runtime.run_context import CURRENT_RUN_CONTEXT, RunContext
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


@pytest.mark.parametrize("channel_id", ["web", "tui", "acp"])
def test_resident_channel_ignores_run_permissions_field(channel_id: str) -> None:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    rail = SimpleNamespace(run_permission_levels={"existing": "ask"})
    adapter._permission_rail = rail
    adapter._is_session_scoped_adapter = False
    adapter._enable_auto_permission = False
    adapter._root_permission_queue = None
    adapter._sys_operation = None
    adapter._resolve_interrupt_session_id = lambda session_id: session_id
    request = AgentRequest(
        request_id="request", session_id="session", channel_id=channel_id,
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent", "run_permissions": {"tools": {"write_file": "deny"}}},
    )

    with adapter._bind_permission_request_context(request):
        assert RUN_PERMISSIONS.get() is None
        assert rail.run_permission_levels is None
        assert not hasattr(rail, "supports_permission_approval")
        installed = {"tools": {"write_file": "allow"}}
        assert overlay_run_permissions(installed) is installed
    assert rail.run_permission_levels == {"existing": "ask"}
    assert RUN_PERMISSIONS.get() is None


def test_process_cli_binds_and_restores_run_permissions() -> None:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    rail = SimpleNamespace(run_permission_levels=None)
    adapter._permission_rail = rail
    adapter._is_session_scoped_adapter = False
    adapter._enable_auto_permission = False
    adapter._root_permission_queue = None
    adapter._sys_operation = None
    adapter._resolve_interrupt_session_id = lambda session_id: session_id
    request = AgentRequest(
        request_id="request", session_id="session", channel_id="process_cli",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent", "run_permissions": {"tools": {"write_file": "deny"}}},
    )

    with adapter._bind_permission_request_context(request):
        assert RUN_PERMISSIONS.get() == {"write_file": "deny"}
        assert rail.run_permission_levels == {"write_file": "deny"}
        assert overlay_run_permissions({"tools": {"write_file": "allow"}})["tools"] == {
            "write_file": "deny"
        }
    assert rail.run_permission_levels is None
    assert RUN_PERMISSIONS.get() is None


@pytest.mark.parametrize("params,expected", [
    ({"supports_permission_approval": False, "supports_user_interaction": True}, False),
    ({"supports_user_interaction": False}, False),
    ({"supports_user_interaction": True}, True),
    ({"supports_permission_approval": True, "supports_user_interaction": True}, True),
])
def test_process_cli_binds_and_restores_permission_approval_capability(params, expected):
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._permission_rail = None
    adapter._is_session_scoped_adapter = False
    adapter._enable_auto_permission = False
    adapter._root_permission_queue = None
    adapter._sys_operation = None
    adapter._resolve_interrupt_session_id = lambda session_id: session_id
    request = AgentRequest(
        request_id="request", session_id="session", channel_id="process_cli", params=params,
    )
    with adapter._bind_permission_request_context(request):
        assert CURRENT_RUN_CONTEXT.get().supports_permission_approval is expected
    assert CURRENT_RUN_CONTEXT.get() is None


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("failure_point", ["root", "command", "body"])
def test_permission_context_failure_restores_session_state(
    monkeypatch: pytest.MonkeyPatch, wrapped: bool, failure_point: str,
) -> None:
    previous_levels = {"read_file": "ask"}
    approval_rail = SimpleNamespace(supports_permission_approval=True)
    rail = SimpleNamespace(base_rail=approval_rail) if wrapped else approval_rail
    rail.run_permission_levels = previous_levels
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._permission_rail = rail
    adapter._is_session_scoped_adapter = True
    adapter._enable_auto_permission = False
    adapter._root_permission_queue = None
    adapter._sys_operation = object() if failure_point == "command" else None
    adapter._sys_operation_card = SimpleNamespace(mode=None)
    adapter._resolve_interrupt_session_id = lambda session_id: session_id
    request = AgentRequest(
        request_id="request", session_id="session", channel_id="process_cli",
        params={"supports_permission_approval": False,
                "run_permissions": {"tools": {"write_file": "deny"}}},
    )

    def fail_binding(*_args, **_kwargs):
        raise RuntimeError("permission context failed")

    if failure_point != "body":
        target = "bind_root_permission_request" if failure_point == "root" else "bind_command_execution"
        monkeypatch.setattr(interface_deep, target, fail_binding)

    def exercise():
        previous_context = RunContext(supports_permission_approval=True)
        CURRENT_RUN_CONTEXT.set(previous_context)
        RUN_PERMISSIONS.set(previous_levels)
        TOOL_PERMISSION_CHANNEL_ID.set("previous-channel")
        TOOL_PERMISSION_REQUEST_ID.set("previous-request")
        with pytest.raises(RuntimeError, match="permission context failed"):
            with adapter._bind_permission_request_context(request):
                assert failure_point == "body"
                assert approval_rail.supports_permission_approval is False
                fail_binding()
        assert approval_rail.supports_permission_approval is True
        assert rail.run_permission_levels is previous_levels
        assert RUN_PERMISSIONS.get() is previous_levels
        assert CURRENT_RUN_CONTEXT.get() is previous_context
        assert TOOL_PERMISSION_CHANNEL_ID.get() == "previous-channel"
        assert TOOL_PERMISSION_REQUEST_ID.get() == "previous-request"
        with pytest.raises(root_permission_queue_rail.RootPermissionQueueError, match="unbound"):
            root_permission_queue_rail.current_root_permission_queue()

    # A failed setup must not contaminate the context used by following tests.
    Context().run(exercise)


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("supports_approval", [False, True])
async def test_permission_approval_policy_reaches_sdk_callbacks_without_request_context(
    tmp_path, wrapped, supports_approval,
):
    from openjiuwen.core.foundation.llm import ToolCall
    from openjiuwen.harness.security.permission_engine.host import ToolPermissionHost
    from openjiuwen.harness.security.permission_engine.models import PermissionConfirmResponse
    from jiuwenswarm.channels.process_cli.permissions import ProcessCliPermissionInterruptRail

    asked = []

    async def confirm(request):
        asked.append(request)
        return PermissionConfirmResponse(approved=True)

    rail = ProcessCliPermissionInterruptRail(
        config={"enabled": True, "package_builtin_rules": False, "defaults": {"*": "allow"},
                "file_guard": {"enabled": True, "defaults": {"read": "ask", "write": "ask", "exec": "ask"}}},
        tool_names=[], llm=None, model_name=None,
        host=ToolPermissionHost(resolve_workspace_dir=lambda: tmp_path, request_permission_confirmation=confirm),
    )
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._permission_rail = SimpleNamespace(base_rail=rail) if wrapped else rail
    adapter._is_session_scoped_adapter = False
    adapter._enable_auto_permission = False
    adapter._root_permission_queue = None
    adapter._sys_operation = None
    adapter._resolve_interrupt_session_id = lambda session_id: session_id
    request = AgentRequest(
        request_id="request", session_id="session", channel_id="process_cli",
        params={"supports_permission_approval": supports_approval},
    )
    call = ToolCall(id="call", type="function", name="bash", arguments=json.dumps({"command": "python writer.py"}))
    ctx = SimpleNamespace(session=None, extra={})
    with adapter._bind_permission_request_context(request):
        task = asyncio.create_task(rail.resolve_interrupt(ctx, call, None), context=Context())
        decision = await task
    assert type(decision).__name__ == ("ApproveResult" if supports_approval else "RejectResult")
    assert len(asked) == int(supports_approval)
    assert rail.supports_permission_approval is None
