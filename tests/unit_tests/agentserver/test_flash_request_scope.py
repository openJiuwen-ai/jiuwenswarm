# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise stock request-scope contracts with the actual Flash adapter class."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_request_scoped_office_claw_mcp as existing

from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
    JiuwenSwarmFlashAdapter,
)

CASES = [
    "test_request_scoped_tool_allowlist_rejects_cross_request_id",
    "test_request_scoped_tool_allowlist_rejects_unbound_invoke",
    "test_request_scoped_tool_invoke_rebinds_from_live_allowlist",
    "test_request_scoped_tool_invoke_rebinds_from_expected_allowlist_kwarg",
    "test_request_scoped_tool_invoke_refuses_foreign_live_rebind_when_concurrent",
    "test_request_scoped_tool_invoke_refuses_cross_request_binding",
    "test_registers_exact_tool_names_and_cleans_them_up",
    "test_no_mcp_payload_replaces_stale_session_registration",
    "test_delayed_registration_cannot_overwrite_newer_request",
    "test_partial_connector_discovery_cannot_block_newer_request",
    "test_cleanup_does_not_remove_rebound_short_name",
    "test_foreign_short_name_conflict_fails_closed",
    "test_legacy_ability_manager_post_add_mismatch_fails_closed",
    "test_unary_request_always_cleans_up_request_mcp",
    "test_stream_request_cleans_up_when_consumer_finishes",
    "test_stream_request_binds_active_tool_allowlist",
    "test_cleanup_superseded_request_preserves_active_allowlist",
    "test_invoke_refuses_env_invocation_mismatch_against_active_allowlist",
    "test_invoke_refuses_office_claw_without_request_binding",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", CASES)
async def test_flash_reuses_request_lifecycle(name, monkeypatch, request):
    def flash_session():
        adapter = JiuwenSwarmFlashAdapter()
        adapter._instance = SimpleNamespace(ability_manager=existing._AbilityManager())
        adapter._active_office_claw_mcp = None
        adapter._active_office_claw_tool_instances = ()
        return adapter

    monkeypatch.setattr(existing, "JiuWenSwarmDeepAdapter", JiuwenSwarmFlashAdapter)
    monkeypatch.setattr(existing, "_bare_session_adapter", flash_session)
    case = getattr(existing, name)
    kwargs = {
        key: request.getfixturevalue(key) for key in inspect.signature(case).parameters
    }
    result = case(**kwargs)
    if inspect.isawaitable(result):
        await result


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Delegated stream closure does not synchronously release request-scoped MCP resources",
)
async def test_flash_stream_aclose_releases_registration():
    parent = JiuwenSwarmFlashAdapter()
    parent._is_session_scoped_adapter = False
    delegates = []
    original = parent._process_message_stream_impl

    def track_delegate(request, inputs):
        stream = original(request, inputs)
        delegates.append(stream)
        return stream

    parent._process_message_stream_impl = track_delegate
    registration = existing.OfficeClawMcpRegistration(
        "req-123", ("tool-id",), ("tool",)
    )
    child = JiuwenSwarmFlashAdapter()
    child.register_request_scoped_office_claw_mcp = AsyncMock(return_value=registration)
    child.cleanup_request_scoped_office_claw_mcp = AsyncMock()

    async def stream(request, inputs):
        yield existing.AgentResponseChunk(request_id="req-123", channel_id="officeclaw")
        raise AssertionError("consumer must stop after first chunk")

    child.process_message_stream_impl = stream
    parent._get_or_create_session_adapter = AsyncMock(return_value=child)
    parent._evict_idle_session_adapters = AsyncMock()
    iterator = parent.process_message_stream_impl(
        existing._request(existing._valid_config()), {}
    )
    try:
        await anext(iterator)
        child.register_request_scoped_office_claw_mcp.assert_awaited_once()
        await iterator.aclose()
        child.cleanup_request_scoped_office_claw_mcp.assert_awaited_once_with(
            registration
        )
    finally:
        # Clean the reproducer in its owning Task after observing the failure.
        for stream in delegates:
            await stream.aclose()
        await iterator.aclose()


@pytest.mark.asyncio
async def test_flash_cancel_during_child_stream_releases_mcp():
    parent, child = JiuwenSwarmFlashAdapter(), JiuwenSwarmFlashAdapter()
    parent._is_session_scoped_adapter = False
    registration = existing.OfficeClawMcpRegistration(
        "req-123", ("tool-id",), ("tool",)
    )
    child.register_request_scoped_office_claw_mcp = AsyncMock(return_value=registration)
    child.cleanup_request_scoped_office_claw_mcp = AsyncMock()
    entered = asyncio.Event()

    async def stream(request, inputs):
        entered.set()
        await asyncio.Event().wait()
        yield existing.AgentResponseChunk(request_id="req-123", channel_id="officeclaw")

    child.process_message_stream_impl = stream
    parent._get_or_create_session_adapter = AsyncMock(return_value=child)
    parent._evict_idle_session_adapters = AsyncMock()
    iterator = parent.process_message_stream_impl(
        existing._request(existing._valid_config()), {}
    )
    task = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    child.cleanup_request_scoped_office_claw_mcp.assert_awaited_once_with(registration)
