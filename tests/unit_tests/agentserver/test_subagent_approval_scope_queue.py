"""Same-scope hosted approvals must queue instead of CapacityError."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.openjiuwen_subagent_approval_queue_patch import (
    apply_subagent_approval_queue_patch,
)
from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import (
    SubagentApprovalKind,
    SubagentApprovalRegistry,
    get_subagent_approval_registry,
)


@pytest.fixture
def registry():
    apply_subagent_approval_queue_patch()
    SubagentApprovalRegistry.reset_instance_for_tests()
    apply_subagent_approval_queue_patch()
    try:
        yield get_subagent_approval_registry()
    finally:
        SubagentApprovalRegistry.reset_instance_for_tests()


@pytest.mark.asyncio
async def test_same_scope_second_request_waits_instead_of_capacity_error(registry) -> None:
    shown = asyncio.Event()
    send_order: list[str] = []

    async def sender(request) -> None:
        send_order.append(request.tool_call_id)
        if request.tool_call_id == "call-1":
            shown.set()

    first = asyncio.create_task(
        registry.request(
            kind=SubagentApprovalKind.TOOL_PERMISSION,
            session_id="sess-ppt",
            agent_scope_id="scope-page-1",
            tool_call_id="call-1",
            payload={"tool_name": "read_file"},
            sender=sender,
            timeout=5,
        )
    )
    await asyncio.wait_for(shown.wait(), timeout=2)
    second = asyncio.create_task(
        registry.request(
            kind=SubagentApprovalKind.TOOL_PERMISSION,
            session_id="sess-ppt",
            agent_scope_id="scope-page-1",
            tool_call_id="call-2",
            payload={"tool_name": "read_file"},
            sender=sender,
            timeout=5,
        )
    )
    await asyncio.sleep(0.05)
    assert not second.done()
    pending = registry.pending_requests()
    assert len(pending) == 1
    assert pending[0].tool_call_id == "call-1"

    assert registry.resolve(
        session_id="sess-ppt",
        approval_id=pending[0].approval_id,
        kind=SubagentApprovalKind.TOOL_PERMISSION,
        answer="allow-1",
    )
    assert await first == "allow-1"

    for _ in range(40):
        pending = registry.pending_requests()
        if pending:
            break
        await asyncio.sleep(0.05)
    assert pending
    assert pending[0].tool_call_id == "call-2"
    assert registry.resolve(
        session_id="sess-ppt",
        approval_id=pending[0].approval_id,
        kind=SubagentApprovalKind.TOOL_PERMISSION,
        answer="allow-2",
    )
    assert await second == "allow-2"
    assert send_order == ["call-1", "call-2"]
