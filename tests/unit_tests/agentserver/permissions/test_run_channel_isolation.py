"""Run-scoped permission choices must not affect resident channels."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.run_permissions import RUN_PERMISSIONS, overlay_run_permissions
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
