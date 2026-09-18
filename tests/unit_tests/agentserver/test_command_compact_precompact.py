# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the PreCompact hook in _handle_command_compact."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.hooks.executor import HookOutcome, HookResult


def _make_request() -> AgentRequest:
    return AgentRequest(
        request_id="req-1",
        session_id="sess-1",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_COMPACT,
        params={"mode": "agent"},
    )


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


def _find_result_payload(data: Any) -> dict[str, Any] | None:
    """Recursively find the response payload dict carrying a string "result"."""
    if isinstance(data, dict):
        if isinstance(data.get("result"), str):
            return data
        for v in data.values():
            found = _find_result_payload(v)
            if found is not None:
                return found
    elif isinstance(data, list):
        for v in data:
            found = _find_result_payload(v)
            if found is not None:
                return found
    return None


def _make_server(agent):
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = SimpleNamespace(get_agent=AsyncMock(return_value=agent))
    return server


class TestPreCompactManualTrigger:
    async def test_blocking_hook_skips_compaction(self) -> None:
        agent = SimpleNamespace(compress_context=AsyncMock())
        server = _make_server(agent)
        ws = _FakeWS()

        blocking = HookResult(outcome=HookOutcome.BLOCKING, error="not now")
        with patch(
            "jiuwenswarm.server.agent_ws_server.fire_pre_compact_hooks",
            new=AsyncMock(return_value=[blocking]),
        ) as fire:
            await server._handle_command_compact(ws, _make_request(), asyncio.Lock())

        fire.assert_awaited_once_with("manual", "sess-1")
        agent.compress_context.assert_not_awaited()
        assert len(ws.sent) == 1
        payload = _find_result_payload(json.loads(ws.sent[0]))
        assert payload is not None
        assert payload["result"] == "blocked"
        assert payload["reason"] == "not now"

    async def test_no_hooks_proceeds_with_compaction(self) -> None:
        agent = SimpleNamespace(
            compress_context=AsyncMock(return_value={"result": "noop", "stats": None}),
        )
        server = _make_server(agent)
        ws = _FakeWS()

        with patch(
            "jiuwenswarm.server.agent_ws_server.fire_pre_compact_hooks",
            new=AsyncMock(return_value=[]),
        ):
            await server._handle_command_compact(ws, _make_request(), asyncio.Lock())

        agent.compress_context.assert_awaited_once()
        payload = _find_result_payload(json.loads(ws.sent[0]))
        assert payload is not None
        assert payload["result"] == "noop"

    async def test_non_blocking_result_proceeds(self) -> None:
        agent = SimpleNamespace(
            compress_context=AsyncMock(return_value={"result": "noop", "stats": None}),
        )
        server = _make_server(agent)
        ws = _FakeWS()

        with patch(
            "jiuwenswarm.server.agent_ws_server.fire_pre_compact_hooks",
            new=AsyncMock(return_value=[HookResult()]),
        ):
            await server._handle_command_compact(ws, _make_request(), asyncio.Lock())

        agent.compress_context.assert_awaited_once()
