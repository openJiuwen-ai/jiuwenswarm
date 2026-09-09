"""Permission rails respect Host-admitted non-Permission continuations."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openjiuwen.harness.rails.security.tool_security_rail import (
    PermissionInterruptRail,
)

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission import (
    before_tool,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.before_tool import (
    AutoPermissionBeforeToolMixin,
)
from jiuwenswarm.agents.harness.common.rails.permissions.permission_interrupt_rail import (
    JiuwenSwarmPermissionInterruptRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    ROOT_NON_PERMISSION_RESUME_ATTRIBUTE,
    RootNonPermissionResume,
)


def _marked_context() -> SimpleNamespace:
    ctx = SimpleNamespace(extra={})
    setattr(
        ctx,
        ROOT_NON_PERMISSION_RESUME_ATTRIBUTE,
        RootNonPermissionResume(
            "root-session",
            "call-1",
            "exit_plan_mode",
        ),
    )
    return ctx


@pytest.mark.asyncio
async def test_auto_permission_does_not_consume_nonpermission_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _marked_context()
    invocation = SimpleNamespace(ctx=ctx, tool_name="exit_plan_mode")
    resolve_user_input = MagicMock(
        return_value={"approved": True, "auto_confirm": True}
    )
    facts = MagicMock()
    clear_no_host = MagicMock()
    clear_send = MagicMock()
    clear_search = MagicMock()
    monkeypatch.setattr(
        before_tool, "_extract_invocation", lambda args, kwargs: invocation
    )
    monkeypatch.setattr(before_tool, "_resolve_user_input", resolve_user_input)
    monkeypatch.setattr(before_tool, "build_tool_decision_facts", facts)
    monkeypatch.setattr(before_tool, "clear_no_host_fallback", clear_no_host)
    monkeypatch.setattr(before_tool, "clear_send_file_execution_grant", clear_send)
    monkeypatch.setattr(before_tool, "clear_trusted_search_producer", clear_search)
    rail = SimpleNamespace(
        _call_base_rail=AsyncMock(),
    )

    result = await AutoPermissionBeforeToolMixin._before_tool_call_impl(rail)

    assert result is None
    resolve_user_input.assert_not_called()
    facts.assert_not_called()
    rail._call_base_rail.assert_not_awaited()
    clear_no_host.assert_called_once_with()
    clear_send.assert_called_once_with()
    clear_search.assert_called_once_with()


@pytest.mark.asyncio
async def test_base_permission_does_not_consume_nonpermission_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = AsyncMock()
    monkeypatch.setattr(PermissionInterruptRail, "before_tool_call", called)
    rail = object.__new__(JiuwenSwarmPermissionInterruptRail)

    await rail.before_tool_call(_marked_context())

    called.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmarked_initial_call_still_enters_base_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = AsyncMock()
    monkeypatch.setattr(PermissionInterruptRail, "before_tool_call", called)
    rail = object.__new__(JiuwenSwarmPermissionInterruptRail)
    ctx = SimpleNamespace(extra={})

    await rail.before_tool_call(ctx)

    called.assert_awaited_once_with(ctx)
