"""Base permission rail callback argument forwarding."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ToolCallInputs,
)
from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.interrupt.state import ToolInterruptEntry, ToolInterruptionState
from openjiuwen.harness.persistent_agent.schema.session_record import decode_interruption, encode_interruption

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import (
    ToolInvocation,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.runtime_bridge import (
    AutoPermissionRuntimeBridgeMixin,
)


class _Bridge(AutoPermissionRuntimeBridgeMixin):
    def __init__(self, base_rail: object) -> None:
        self.base_rail = base_rail


def _invocation() -> ToolInvocation:
    tool_call = SimpleNamespace(id="tool-call-1", name="bash", arguments={})
    ctx = AgentCallbackContext(
        agent=SimpleNamespace(ability_manager=SimpleNamespace(get=lambda _name: None)),
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name="bash",
            tool_args={},
        ),
        session=SimpleNamespace(session_id="session-1"),
        extra={},
    )
    return ToolInvocation(ctx=ctx, tool_call=tool_call, tool_name="bash", tool_args={})


def test_auto_manual_marker_is_trusted_request_metadata_only() -> None:
    base = SimpleNamespace(_get_auto_confirm_key=lambda _tool_call: "bash")
    request = _Bridge(base)._build_runtime_interrupt_request(
        _invocation(),
        {
            "status": "interrupt",
            "metadata": {"auto_permission_manual": True},
        },
    )

    assert request.metadata["auto_permission_manual"] is True
    assert request.auto_confirm_key == ""
    assert getattr(request, "auto_permission_manual", None) is None


def test_manual_approval_interrupt_round_trips_through_persistent_session_state() -> None:
    invocation = _invocation()
    request = _Bridge(SimpleNamespace(_get_auto_confirm_key=lambda _tool_call: "bash"))._build_runtime_interrupt_request(
        invocation,
        {
            "status": "interrupt",
            "metadata": {
                "auto_permission_manual": True,
                "final_reviewer_status": "manual",
            },
        },
    )
    tool_call = ToolCall(id="tool-call-1", type="function", name="bash", arguments="{}")
    state = ToolInterruptionState(
        ai_message=AssistantMessage(content="Checking access", tool_calls=[tool_call]),
        iteration=1,
        original_query="Read a file",
        interrupted_tools={
            "tool-call-1": ToolInterruptEntry(
                tool_call=tool_call,
                interrupt_requests={"tool-call-1:0": request},
            ),
        },
        auto_confirm_mapping={"tool-call-1:0": "bash"},
    )

    restored = decode_interruption(encode_interruption(state))
    restored_request = restored.interrupted_tools["tool-call-1"].interrupt_requests["tool-call-1:0"]

    assert type(request) is InterruptRequest
    assert type(restored_request) is InterruptRequest
    assert restored_request.metadata["auto_permission_manual"] is True
    assert restored_request.metadata["final_reviewer_status"] == "manual"


def test_native_manual_interrupt_keeps_core_auto_confirm_key() -> None:
    base = SimpleNamespace(_get_auto_confirm_key=lambda _tool_call: "bash")
    request = _Bridge(base)._build_runtime_interrupt_request(
        _invocation(),
        {"status": "interrupt", "metadata": {}},
    )

    assert request.auto_confirm_key == "bash"


@pytest.mark.asyncio
async def test_before_forwards_single_parameter_bound_method() -> None:
    calls: list[object] = []

    class BaseRail:
        async def before_tool_call(self, ctx: object) -> str:
            calls.append(ctx)
            return "single"

    invocation = _invocation()
    result = await _Bridge(BaseRail())._call_base_rail(
        (invocation.ctx, invocation.tool_call), {}, invocation
    )

    assert result == "single"
    assert calls == [invocation.ctx]


@pytest.mark.asyncio
async def test_before_supplies_required_tool_call_after_runtime_context() -> None:
    calls: list[tuple[object, object]] = []

    class BaseRail:
        async def before_tool_call(self, ctx: object, tool_call: object) -> str:
            calls.append((ctx, tool_call))
            return "paired"

    invocation = _invocation()
    result = await _Bridge(BaseRail())._call_base_rail(
        (invocation.ctx,), {}, invocation
    )

    assert result == "paired"
    assert calls == [(invocation.ctx, invocation.tool_call)]


@pytest.mark.asyncio
async def test_before_preserves_explicit_keyword_tool_call() -> None:
    calls: list[tuple[object, object]] = []

    class BaseRail:
        async def before_tool_call(self, ctx: object, tool_call: object) -> None:
            calls.append((ctx, tool_call))

    invocation = _invocation()
    explicit_tool_call = object()
    await _Bridge(BaseRail())._call_base_rail(
        (invocation.ctx,), {"tool_call": explicit_tool_call}, invocation
    )

    assert calls == [(invocation.ctx, explicit_tool_call)]


@pytest.mark.asyncio
async def test_before_does_not_supplement_non_runtime_context() -> None:
    class BaseRail:
        async def before_tool_call(self, ctx: object, tool_call: object) -> None:
            raise AssertionError("callback must not be invoked with invented arguments")

    invocation = ToolInvocation(
        ctx=SimpleNamespace(),
        tool_call=object(),
        tool_name="bash",
        tool_args={},
    )

    with pytest.raises(TypeError):
        await _Bridge(BaseRail())._call_base_rail((invocation.ctx,), {}, invocation)


@pytest.mark.asyncio
async def test_after_supplies_required_tool_call_after_runtime_context() -> None:
    calls: list[tuple[object, object]] = []

    class BaseRail:
        async def after_tool_call(self, ctx: object, tool_call: object) -> str:
            calls.append((ctx, tool_call))
            return "after"

    invocation = _invocation()
    result = await _Bridge(BaseRail())._call_base_after_rail(
        (invocation.ctx,), {}, invocation
    )

    assert result == "after"
    assert calls == [(invocation.ctx, invocation.tool_call)]
