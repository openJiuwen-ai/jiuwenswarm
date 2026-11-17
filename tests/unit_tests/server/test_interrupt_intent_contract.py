# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""chat.cancel / chat.interrupt intent 契约的回归测试。

覆盖三件事：白名单纯函数；null/空串按既有惯例归一化为 cancel；
中立适配层 ``handle_chat_cancel`` 对未知 intent 显式拒绝而不是静默假成功。
WS 侧分支依赖共享契约模块，也用源码级断言防回退到本地副本。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, ReqMethod
from jiuwenswarm.server.handlers.chat import handle_chat_cancel
from jiuwenswarm.server.interrupt_intent import (
    KNOWN_INTERRUPT_INTENTS,
    normalize_interrupt_intent,
    unknown_interrupt_intent_error,
)

_WS_SERVER_FILE = (
    Path(__file__).resolve().parents[3] / "jiuwenswarm" / "server" / "agent_ws_server.py"
)


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def cancel_request(self, request: AgentRequest, *, allow_create: bool = False) -> AgentResponse:
        self.calls.append({"request_id": request.request_id, "allow_create": allow_create})
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"success": True},
        )


def _ctx(params: dict, runtime: _Runtime | None = None) -> tuple[SimpleNamespace, AsyncMock, SimpleNamespace]:
    request = AgentRequest(
        request_id="req-1",
        channel_id="web",
        session_id="session-1",
        req_method=ReqMethod.CHAT_CANCEL,
        params=params,
    )
    sink = AsyncMock()
    services = SimpleNamespace(
        execution_runtime=lambda: runtime or _Runtime(),
        session_stream_tasks={},
    )
    return SimpleNamespace(request=request, sink=sink, services=services), sink, services


@pytest.mark.parametrize("intent", KNOWN_INTERRUPT_INTENTS)
def test_known_intent_passes_contract(intent: str) -> None:
    assert unknown_interrupt_intent_error(intent) is None


@pytest.mark.parametrize("intent", ["user_cancel", "halt", "pause_all", "cancel_now"])
def test_unknown_intent_reports_actual_and_expected_values(intent: str) -> None:
    error = unknown_interrupt_intent_error(intent)
    assert error is not None
    assert repr(intent) in error
    assert "expected one of: cancel, supplement, pause, resume, stop" in error


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, "cancel"),
        ({"intent": None}, "cancel"),
        ({"intent": ""}, "cancel"),
        ({"intent": "pause"}, "pause"),
        ({"intent": "stop"}, "cancel"),
        ({"intent": "STOP"}, "cancel"),
        ({"intent": " Cancel "}, "cancel"),
    ],
)
def test_normalize_intent_defaults_null_and_empty_to_cancel(params: dict, expected: str) -> None:
    assert normalize_interrupt_intent(params) == expected


def test_normalize_intent_handles_non_dict_params() -> None:
    assert normalize_interrupt_intent(None) == "cancel"
    assert normalize_interrupt_intent(["intent"]) == "cancel"


@pytest.mark.asyncio
async def test_handle_chat_cancel_rejects_unknown_intent_without_side_effects() -> None:
    runtime = _Runtime()
    ctx, sink, services = _ctx({"intent": "user_cancel"}, runtime)

    await handle_chat_cancel(ctx)

    assert runtime.calls == []
    assert services.session_stream_tasks == {}
    response = sink.send_response.await_args.args[0]
    assert response.ok is False
    assert response.payload["event_type"] == "chat.interrupt_result"
    assert response.payload["success"] is False
    assert "unknown interrupt intent" in response.payload["error"]
    assert "user_cancel" in response.payload["error"]


@pytest.mark.parametrize("params", [{}, {"intent": None}, {"intent": ""}])
@pytest.mark.asyncio
async def test_handle_chat_cancel_treats_missing_intent_as_cancel(params: dict) -> None:
    runtime = _Runtime()
    ctx, sink, _services = _ctx(params, runtime)

    await handle_chat_cancel(ctx)

    assert runtime.calls == [{"request_id": "req-1", "allow_create": False}]
    response = sink.send_response.await_args.args[0]
    assert response.ok is True


@pytest.mark.parametrize("intent", ["cancel", "stop"])
@pytest.mark.asyncio
async def test_handle_chat_cancel_keeps_valid_intent_path(intent: str) -> None:
    runtime = _Runtime()
    ctx, sink, _services = _ctx({"intent": intent}, runtime)

    await handle_chat_cancel(ctx)

    assert runtime.calls == [{"request_id": "req-1", "allow_create": False}]
    assert sink.send_response.await_args.args[0].ok is True


@pytest.mark.asyncio
async def test_handle_chat_cancel_treats_stop_as_cancel_for_stream_tasks() -> None:
    runtime = _Runtime()
    ctx, _sink, services = _ctx({"intent": "stop"}, runtime)
    stop_event = asyncio.Event()
    stream_task = asyncio.get_running_loop().create_task(asyncio.Event().wait())
    services.session_stream_tasks["session-1"] = {stream_task: stop_event}

    try:
        await handle_chat_cancel(ctx)
        assert stop_event.is_set()
        assert stream_task.cancelled()
    finally:
        stream_task.cancel()
        await asyncio.gather(stream_task, return_exceptions=True)


def test_ws_cancel_branch_reuses_the_shared_contract() -> None:
    source = _WS_SERVER_FILE.read_text(encoding="utf-8")
    assert "from jiuwenswarm.server.interrupt_intent import" in source
    assert "normalize_interrupt_intent(request.params)" in source
    assert "unknown_interrupt_intent_error(intent)" in source
    assert "def _unknown_interrupt_intent_error" not in source