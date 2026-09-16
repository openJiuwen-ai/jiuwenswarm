# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Receipts must distinguish rejection from a race after SDK submission."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_adapter.session_input import (
    SessionInputDeliveryUnknown, SessionInputGuard,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["closed", "changed_round", "rejected", "accepted"])
async def test_receipt_truthfulness_across_sdk_submission(race):
    instance = SimpleNamespace(active_round=object(), has_output_stream=lambda: True)
    guard = SessionInputGuard(instance)
    guard.accepting = race != "rejected"

    async def sdk_send(_request):
        if race == "closed":
            guard.accepting = False
        if race == "changed_round":
            instance.active_round = object()

    instance.send_input = AsyncMock(side_effect=sdk_send)

    async def permission_send(request, *, send):
        await send(request)
        # Existing adapter's bool is permission-transaction ownership, not ACK.
        return False

    adapter = SimpleNamespace(
        _instance=instance, _session_input_guard=guard,
        _stream_completion_state=lambda **kwargs: "completed",
        _prepare_root_input_dispatch=AsyncMock(return_value="prepared"),
        _permission_inputs_for_dispatch=lambda *args: {"query": "extra text"},
        _send_input_with_permission_resume_guard=permission_send,
        _permission_dispatch=SimpleNamespace(finalize=Mock()),
    )
    request = SimpleNamespace(params={"input_mode": "steer"}, request_id="input")
    if race == "accepted":
        assert await JiuWenSwarmDeepAdapter.deliver_active_session_input(
            adapter, request, {"query": "extra text"}
        )
    elif race == "rejected":
        with pytest.raises(RuntimeError, match="not sent"):
            await JiuWenSwarmDeepAdapter.deliver_active_session_input(adapter, request, {})
        instance.send_input.assert_not_awaited()
        adapter._prepare_root_input_dispatch.assert_not_awaited()
        return
    else:
        with pytest.raises(SessionInputDeliveryUnknown, match="do not retry automatically"):
            await JiuWenSwarmDeepAdapter.deliver_active_session_input(
                adapter, request, {"query": "extra text"}
            )
    instance.send_input.assert_awaited_once()
    adapter._permission_dispatch.finalize.assert_called_once_with("prepared")


@pytest.mark.asyncio
async def test_guard_install_failure_is_retryable_and_reload_replaces_registration(monkeypatch):
    instance = SimpleNamespace(
        ensure_initialized=AsyncMock(), register_rail=AsyncMock(side_effect=RuntimeError("rail failed")),
        unregister_rail=AsyncMock(),
    )
    adapter = JiuWenSwarmDeepAdapter()
    monkeypatch.setattr(adapter, "_instance", instance)
    with pytest.raises(RuntimeError, match="rail failed"):
        await adapter.install_session_input_guard()
    assert adapter._session_input_guard is None
    instance.register_rail.side_effect = None
    await adapter.install_session_input_guard()
    guard = adapter._session_input_guard
    await adapter.install_session_input_guard()
    assert instance.register_rail.await_count == 2
    await adapter.install_session_input_guard(reload=True)
    instance.unregister_rail.assert_awaited_once_with(guard)
    assert instance.register_rail.await_count == 3
