# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Behavior contract for ``session.switch`` before Runtime migration."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from weakref import WeakValueDictionary

import pytest

from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.agent_ws_server import AdapterRegistry, AgentWebSocketServer


class RecordingWebSocket:
    def __init__(self, trace: list[str] | None = None) -> None:
        self.trace = trace if trace is not None else []
        self.sent: list[dict[str, Any]] = []

    async def send(self, payload: str) -> None:
        self.trace.append("response.send")
        self.sent.append(json.loads(payload))


class SessionSwitchServer(AgentWebSocketServer):
    async def handle_session_switch_for_test(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        await self._handle_session_switch(ws, request, send_lock)

    async def handle_message_for_test(
        self,
        ws: Any,
        raw: str,
        send_lock: asyncio.Lock,
    ) -> None:
        await self._handle_message(ws, raw, send_lock)

    async def _handle_gateway_cron_callback(self, *_args: Any) -> bool:
        return False

    async def _dispatch_gateway_adapter_request(self, *_args: Any) -> bool:
        return False

    async def _trigger_before_chat_request_hook(self, _request: AgentRequest) -> None:
        return None


@pytest.fixture(autouse=True)
def isolated_switch_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent_ws_server_module,
        "_session_switch_locks",
        WeakValueDictionary(),
    )


def make_server() -> SessionSwitchServer:
    server = SessionSwitchServer.__new__(SessionSwitchServer)
    server._adapter_registry = AdapterRegistry()
    return server


def switch_request(
    *,
    request_id: str = "switch-request",
    channel_id: str | None = "web",
    session_id: str = "request-session",
    params: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        channel_id=channel_id,
        session_id=session_id,
        req_method=ReqMethod.SESSION_SWITCH,
        params={} if params is None else params,
        metadata=metadata,
    )


def switch_wire(
    *,
    request_id: str,
    session_id: str,
    params: dict[str, Any],
    metadata: dict[str, Any] | None,
) -> str:
    envelope = e2a_from_agent_fields(
        request_id=request_id,
        channel_id="tui",
        session_id=session_id,
        req_method=ReqMethod.SESSION_SWITCH,
        params=params,
        is_stream=False,
        timestamp=0.0,
        metadata=metadata,
    )
    return json.dumps(envelope.to_dict(), ensure_ascii=False)


@pytest.mark.asyncio
async def test_success_preserves_complete_wire_metadata_and_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = make_server()
    trace: list[str] = []
    ws = RecordingWebSocket(trace)
    prepare_calls: list[dict[str, Any]] = []
    kvc_calls: list[dict[str, Any]] = []
    context = object()
    dispatch_signals = object()

    async def prepare(**kwargs: Any) -> tuple[bool, str, Any, None, Any]:
        trace.append("switch.prepare")
        prepare_calls.append(kwargs)
        return False, "code.normal", context, None, dispatch_signals

    async def dispatch_kvc(**kwargs: Any) -> None:
        trace.append("switch.kvc")
        kvc_calls.append(kwargs)

    monkeypatch.setattr(server, "_prepare_session_switch_owner", prepare)
    monkeypatch.setattr(server, "_dispatch_session_switch_kvc", dispatch_kvc)
    metadata = {"trace_id": "switch-success", "nested": {"value": 1}}
    request = switch_request(
        request_id="switch-success",
        session_id="request-session",
        params={
            "session_id": "target-session",
            "previous_session_id": "previous-session",
            "mode": "code.normal",
            "view_id": "view-7",
        },
        metadata=metadata,
    )

    await server.handle_session_switch_for_test(ws, request, asyncio.Lock())

    assert trace == ["switch.prepare", "switch.kvc", "response.send"]
    assert prepare_calls == [
        {
            "channel_id": "web",
            "target_session_id": "target-session",
            "previous_session_id": "previous-session",
            "params": request.params,
            "reason": "session.switch: ",
        }
    ]
    assert kvc_calls == [
        {
            "channel_id": "web",
            "target_session_id": "target-session",
            "previous_session_id": "previous-session",
            "context": context,
            "dispatch_signals": dispatch_signals,
            "view_id": "view-7",
        }
    ]
    assert len(ws.sent) == 1
    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.request_id == "switch-success"
    assert response.channel_id == "web"
    assert response.ok is True
    assert response.payload == {
        "session_id": "target-session",
        "mode": "code.normal",
        "switched": True,
    }
    assert response.metadata == metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "params",
        "request_session_id",
        "channel_id",
        "expected_target",
        "expected_channel",
    ),
    [
        (
            {"session_id": "params-session", "previous_session_id": "old"},
            "request-session",
            "web",
            "params-session",
            "web",
        ),
        (
            {"previous_session_id": "old"},
            "request-session",
            None,
            "request-session",
            "default",
        ),
    ],
)
async def test_target_precedence_fallback_and_default_channel(
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
    request_session_id: str,
    channel_id: str | None,
    expected_target: str,
    expected_channel: str,
) -> None:
    server = make_server()
    calls: list[dict[str, Any]] = []

    async def prepare(**kwargs: Any) -> tuple[bool, str, None, None, None]:
        calls.append(kwargs)
        return False, "agent.plan", None, None, None

    monkeypatch.setattr(server, "_prepare_session_switch_owner", prepare)
    ws = RecordingWebSocket()
    request = switch_request(
        channel_id=channel_id,
        session_id=request_session_id,
        params=params,
    )

    await server.handle_session_switch_for_test(ws, request, asyncio.Lock())

    assert calls[0]["target_session_id"] == expected_target
    assert calls[0]["channel_id"] == expected_channel
    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.payload == {
        "session_id": expected_target,
        "mode": "agent.plan",
        "switched": True,
    }


@pytest.mark.asyncio
async def test_missing_kvc_context_skips_dispatch_and_still_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = make_server()

    async def prepare(**_kwargs: Any) -> tuple[bool, str, None, None, Any]:
        return False, "agent.plan", None, None, object()

    async def unexpected_dispatch(**_kwargs: Any) -> None:
        raise AssertionError("KVC dispatch must be skipped without a context")

    monkeypatch.setattr(server, "_prepare_session_switch_owner", prepare)
    monkeypatch.setattr(server, "_dispatch_session_switch_kvc", unexpected_dispatch)
    ws = RecordingWebSocket()

    await server.handle_session_switch_for_test(
        ws,
        switch_request(params={"session_id": "target-session"}),
        asyncio.Lock(),
    )

    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.ok is True
    assert response.payload == {
        "session_id": "target-session",
        "mode": "agent.plan",
        "switched": True,
    }


@pytest.mark.asyncio
async def test_default_view_id_remains_scoped_to_server_websocket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = make_server()
    ws = RecordingWebSocket()
    dispatched: list[dict[str, Any]] = []

    async def prepare(**_kwargs: Any) -> tuple[bool, str, Any, None, Any]:
        return False, "agent.plan", object(), None, object()

    async def dispatch_kvc(**kwargs: Any) -> None:
        dispatched.append(kwargs)

    monkeypatch.setattr(server, "_prepare_session_switch_owner", prepare)
    monkeypatch.setattr(server, "_dispatch_session_switch_kvc", dispatch_kvc)

    await server.handle_session_switch_for_test(
        ws,
        switch_request(params={"session_id": "target-session"}),
        asyncio.Lock(),
    )

    assert dispatched[0]["view_id"] == f"ws:{id(ws)}"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["prepare", "kvc"])
async def test_business_failure_keeps_legacy_error_wire_through_full_message_path(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    server = make_server()
    error_message = f"{failure_stage} failed"

    async def prepare(**_kwargs: Any) -> tuple[bool, str, Any, None, Any]:
        if failure_stage == "prepare":
            raise RuntimeError(error_message)
        return False, "agent.plan", object(), None, object()

    async def dispatch_kvc(**_kwargs: Any) -> None:
        raise RuntimeError(error_message)

    monkeypatch.setattr(server, "_prepare_session_switch_owner", prepare)
    monkeypatch.setattr(server, "_dispatch_session_switch_kvc", dispatch_kvc)
    metadata = {"trace_id": f"switch-{failure_stage}-failure"}
    ws = RecordingWebSocket()

    await server.handle_message_for_test(
        ws,
        switch_wire(
            request_id=f"switch-{failure_stage}-failure",
            session_id="request-session",
            params={"session_id": "target-session"},
            metadata=metadata,
        ),
        asyncio.Lock(),
    )

    assert len(ws.sent) == 1
    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.request_id == f"switch-{failure_stage}-failure"
    assert response.channel_id == "tui"
    assert response.ok is False
    assert response.payload == {"error": error_message}
    assert response.metadata == metadata


@pytest.mark.asyncio
async def test_cancelled_owner_releases_switch_lock_for_successor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = make_server()
    ws = RecordingWebSocket()
    owner_entered = asyncio.Event()
    successor_entered = asyncio.Event()
    never_release = asyncio.Event()

    async def prepare(**kwargs: Any) -> tuple[bool, str, None, None, None]:
        if kwargs["target_session_id"] == "owner-session":
            owner_entered.set()
            await never_release.wait()
        successor_entered.set()
        return False, "agent.plan", None, None, None

    monkeypatch.setattr(server, "_prepare_session_switch_owner", prepare)
    owner: asyncio.Task[None] | None = None
    successor: asyncio.Task[None] | None = None
    try:
        owner = asyncio.create_task(
            server.handle_session_switch_for_test(
                ws,
                switch_request(
                    request_id="switch-owner",
                    params={"session_id": "owner-session"},
                ),
                asyncio.Lock(),
            )
        )
        await asyncio.wait_for(owner_entered.wait(), timeout=1.0)
        lock_key = f"{id(ws)}:web"
        owner_lock = agent_ws_server_module._session_switch_locks[lock_key]
        assert owner_lock.locked()

        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert not owner_lock.locked()

        successor = asyncio.create_task(
            server.handle_session_switch_for_test(
                ws,
                switch_request(
                    request_id="switch-successor",
                    params={"session_id": "successor-session"},
                ),
                asyncio.Lock(),
            )
        )
        await asyncio.wait_for(successor_entered.wait(), timeout=1.0)
        await successor
        assert len(ws.sent) == 1
        assert parse_agent_server_wire_unary(ws.sent[0]).payload == {
            "session_id": "successor-session",
            "mode": "agent.plan",
            "switched": True,
        }
    finally:
        never_release.set()
        tasks = [task for task in (owner, successor) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_different_websockets_same_channel_can_prepare_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = make_server()
    first_ws = RecordingWebSocket()
    second_ws = RecordingWebSocket()
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def prepare(**kwargs: Any) -> tuple[bool, str, None, None, None]:
        if kwargs["target_session_id"] == "first-session":
            first_entered.set()
            await release_first.wait()
        else:
            second_entered.set()
        return False, "agent.plan", None, None, None

    monkeypatch.setattr(server, "_prepare_session_switch_owner", prepare)
    first: asyncio.Task[None] | None = None
    second: asyncio.Task[None] | None = None
    try:
        first = asyncio.create_task(
            server.handle_session_switch_for_test(
                first_ws,
                switch_request(
                    request_id="switch-first",
                    params={"session_id": "first-session"},
                ),
                asyncio.Lock(),
            )
        )
        await asyncio.wait_for(first_entered.wait(), timeout=1.0)

        second = asyncio.create_task(
            server.handle_session_switch_for_test(
                second_ws,
                switch_request(
                    request_id="switch-second",
                    params={"session_id": "second-session"},
                ),
                asyncio.Lock(),
            )
        )
        await asyncio.wait_for(second_entered.wait(), timeout=1.0)
        assert not first.done()

        release_first.set()
        await asyncio.gather(first, second)
        assert len(first_ws.sent) == 1
        assert len(second_ws.sent) == 1
    finally:
        release_first.set()
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
