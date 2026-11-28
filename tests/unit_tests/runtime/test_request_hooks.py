# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hook injection retains request identity, policy failures and cancellation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.service import AgentRuntime


@pytest.mark.asyncio
async def test_runtime_dispatches_only_chat_requests_through_injected_port():
    async def enrich(request):
        request.params["authorized"] = True

    hooks = SimpleNamespace(before_chat=AsyncMock(side_effect=enrich))
    runtime = AgentRuntime(agent_manager=SimpleNamespace(), hooks=hooks)
    request = AgentRequest(
        request_id="hook", channel_id="web", session_id="session",
        req_method=ReqMethod.CHAT_SEND, params={},
    )
    await runtime._trigger_before_chat_request_hook(request)
    hooks.before_chat.assert_awaited_once_with(request)
    assert request.params["authorized"] is True
    request.req_method = None
    await runtime._trigger_before_chat_request_hook(request)
    assert hooks.before_chat.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("policy denied"), asyncio.CancelledError()])
async def test_runtime_does_not_swallow_injected_policy_failure(error):
    hooks = SimpleNamespace(before_chat=AsyncMock(side_effect=error))
    runtime = AgentRuntime(agent_manager=SimpleNamespace(), hooks=hooks)
    request = AgentRequest(
        request_id="denied", channel_id="web", session_id="session",
        req_method=ReqMethod.CHAT_SEND, params={},
    )
    with pytest.raises(type(error)) as raised:
        await runtime._trigger_before_chat_request_hook(request)
    assert raised.value is error
