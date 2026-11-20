# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""chat.cancel / chat.interrupt intent 契约的回归测试。

覆盖三件事：白名单纯函数；null/空串按既有惯例归一化为 cancel；
中立适配层 ``handle_chat_cancel`` 对未知 intent 显式拒绝而不是静默假成功。
WS 侧分支用行为级测试走真实 dispatch，防拒绝分支被静默回退。
"""

from __future__ import annotations

import asyncio
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
    assert response.payload["intent"] == "user_cancel"
    assert "unknown interrupt intent" in response.payload["message"]
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
    # 归一化后的 intent 回写进 request，下游读到的是规范取值
    assert ctx.request.params["intent"] == "cancel"


@pytest.mark.parametrize("params", [{"intent": "STOP"}, {"intent": " stop "}, {"intent": "Cancel"}])
@pytest.mark.asyncio
async def test_handle_chat_cancel_normalizes_intent_before_downstream(params: dict) -> None:
    runtime = _Runtime()
    ctx, _sink, _services = _ctx(params, runtime)

    await handle_chat_cancel(ctx)

    assert runtime.calls == [{"request_id": "req-1", "allow_create": False}]
    assert ctx.request.params["intent"] == "cancel"


@pytest.mark.asyncio
async def test_handle_chat_cancel_rejects_unknown_intent_before_targeted_cancel() -> None:
    runtime = _Runtime()
    ctx, sink, _services = _ctx({"intent": "user_cancel", "target_team_id": "expert"}, runtime)

    await handle_chat_cancel(ctx)

    assert runtime.calls == []
    response = sink.send_response.await_args.args[0]
    assert response.ok is False
    assert response.payload["success"] is False
    assert "unknown interrupt intent" in response.payload["error"]


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


@pytest.mark.asyncio
async def test_ws_cancel_branch_rejects_unknown_intent_before_any_cancel_action(monkeypatch) -> None:
    """行为级回归：WS 入口对未知 intent 的拒绝必须发生在任何会话/任务操作之前。

    防 three 种回退：拒绝分支的 return 被去掉、校验被挪到取消动作之后、
    断连判定取反写错——源码子串断言防不住这些，必须走真实 dispatch。
    """
    import jiuwenswarm.server.agent_ws_server as ws_module
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    server = object.__new__(AgentWebSocketServer)
    stream_tasks: dict = {}
    monkeypatch.setattr(server, "_session_stream_tasks", stream_tasks, raising=False)
    monkeypatch.setattr(server, "_is_client_disconnect_cancel_request", lambda request: False)
    handle_cancel = AsyncMock()
    monkeypatch.setattr(AgentWebSocketServer, "_handle_cancel", handle_cancel)

    sent = []

    async def fake_send_wire_payload(ws, payload):
        sent.append(payload)

    monkeypatch.setattr(ws_module, "send_wire_payload", fake_send_wire_payload)

    request = AgentRequest(
        request_id="req-ws-1",
        channel_id="web",
        session_id="session-ws",
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": "user_cancel"},
    )
    await server.dispatch_parsed_request(ws=object(), request=request, send_lock=asyncio.Lock())

    handle_cancel.assert_not_awaited()
    assert stream_tasks == {}
    assert len(sent) == 1
    wire = sent[0]
    assert wire["status"] == "failed"
    details = wire["body"]["details"]
    assert details["event_type"] == "chat.interrupt_result"
    assert details["success"] is False
    assert details["intent"] == "user_cancel"
    assert "unknown interrupt intent" in details["message"]
    assert request.params["intent"] == "user_cancel"  # 拒绝路径不回写 params


@pytest.mark.asyncio
async def test_client_disconnect_skips_intent_contract_and_proceeds_to_cancel() -> None:
    """断连合成请求跳过 intent 校验，确保 cleanup 路径可达。"""
    from e2a.constants import E2A_CANCEL_SOURCE_CLIENT_DISCONNECT, E2A_INTERNAL_CANCEL_SOURCE_KEY

    runtime = _Runtime()
    request = AgentRequest(
        request_id="req-disc",
        channel_id="web",
        session_id="session-1",
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": "user_cancel"},
        metadata={E2A_INTERNAL_CANCEL_SOURCE_KEY: E2A_CANCEL_SOURCE_CLIENT_DISCONNECT},
    )
    sink = AsyncMock()
    services = SimpleNamespace(
        execution_runtime=lambda: runtime,
        session_stream_tasks={},
    )
    ctx = SimpleNamespace(request=request, sink=sink, services=services)

    await handle_chat_cancel(ctx)

    # 断连请求不被 intent 校验拦截，进入正常取消路径
    assert runtime.calls == [{"request_id": "req-disc", "allow_create": False}]