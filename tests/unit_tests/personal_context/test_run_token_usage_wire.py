# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Preserve optional run usage on the existing PCS query wire path."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.personal_context import ws_handler as module


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [
    ReqMethod.PERSONAL_CONTEXT_RUNTIME_STATUS, ReqMethod.PERSONAL_CONTEXT_FETCH_GET_RUN_STATUS,
])
@pytest.mark.parametrize("usage", [
    None,
    {"total_tokens": 0, "has_estimate": False, "incomplete": False},
    {"total_tokens": None, "has_estimate": False, "incomplete": True},
])
async def test_existing_wire_queries_preserve_optional_run_usage(method, usage):
    send = AsyncMock()
    record = {
        "service_id": "notes", "run_id": "a" * 32, "run_state": "succeeded",
        "capacity_maintenance": {"status": "over_limit", "removed_count": 2},
    }
    if usage is not None:
        record["token_usage"] = usage
    if method == ReqMethod.PERSONAL_CONTEXT_RUNTIME_STATUS:
        params = {}
        expected = {"fetch_run_progress": {"notes": record}}
        host = SimpleNamespace(error_language="zh", get_status=AsyncMock(return_value=expected))
    else:
        params = {"service_id": "notes", "run_id": record["run_id"]}
        expected = record
        host = SimpleNamespace(error_language="zh", get_fetch_run_status=AsyncMock(return_value=expected))
    request = AgentRequest(request_id="pcs-usage", channel_id="web", req_method=method, params=params)

    await module.handle_personal_context_request(host, SimpleNamespace(send=send), request, asyncio.Lock())

    if method == ReqMethod.PERSONAL_CONTEXT_RUNTIME_STATUS:
        host.get_status.assert_awaited_once_with()
    else:
        host.get_fetch_run_status.assert_awaited_once_with("notes", run_id=record["run_id"])
    send.assert_awaited_once()
    response = parse_agent_server_wire_unary(json.loads(send.call_args.args[0]))
    assert response.ok and response.payload == expected
