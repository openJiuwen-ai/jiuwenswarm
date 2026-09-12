# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Behavior characterization for the AgentServer ``session.fork`` adapter."""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import (
    AgentRuntime,
    SessionForkInput,
    SessionForkResult,
    SessionProvisionCommitTiming,
    SessionProvisionError,
    SessionProvisionState,
)
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


def _request(
    *,
    target_session_id: str | None = "fork-target",
    source_session_id: str = "fork-source",
    channel_id: str = "tui",
) -> AgentRequest:
    params = {
        "source_session_id": source_session_id,
        "title": "Forked session",
    }
    if target_session_id is not None:
        params["target_session_id"] = target_session_id
    return AgentRequest(
        request_id="fork-request",
        channel_id=channel_id,
        session_id="fork-source",
        req_method=ReqMethod.SESSION_FORK,
        params=params,
        metadata={"trace_id": "fork-characterization"},
    )


def _fork_result(target_session_id: str = "fork-target") -> SessionForkResult:
    return SessionForkResult(
        channel_id="tui",
        session_id=target_session_id,
        source_session_id="fork-source",
        title="Forked session",
    )


def _server(runtime: Any) -> AgentWebSocketServer:
    server = object.__new__(AgentWebSocketServer)
    server._runtime = runtime
    server._agent_manager = runtime.agent_manager
    return server


def _parse_sent_response(ws: Any) -> Any:
    ws.send.assert_awaited_once()
    return parse_agent_server_wire_unary(json.loads(ws.send.await_args.args[0]))


def _runtime_adapter(
    *,
    trace: list[str],
    result: SessionForkResult | None = None,
    prepare_error: BaseException | None = None,
    commit_error: BaseException | None = None,
    abort_error: BaseException | None = None,
) -> tuple[Any, object]:
    prepared = SimpleNamespace(state=SessionProvisionState.PREPARED)
    manager = object()

    async def start() -> None:
        trace.append("runtime.start")

    async def prepare(provision_input: SessionForkInput) -> object:
        trace.append("runtime.prepare")
        if prepare_error is not None:
            raise prepare_error
        return prepared

    async def commit(
        lease: object,
        *,
        timing: SessionProvisionCommitTiming,
    ) -> SessionForkResult:
        assert lease is prepared
        assert timing is SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY
        trace.append("runtime.commit")
        if commit_error is not None:
            raise commit_error
        prepared.state = SessionProvisionState.COMMITTED
        return result or _fork_result()

    async def abort(lease: object) -> None:
        assert lease is prepared
        trace.append("runtime.abort")
        if abort_error is not None:
            raise abort_error
        prepared.state = SessionProvisionState.ABORTED

    runtime = SimpleNamespace(
        agent_manager=manager,
        start=AsyncMock(side_effect=start),
        prepare_session_fork=AsyncMock(side_effect=prepare),
        commit_session_provision=AsyncMock(side_effect=commit),
        abort_session_provision=AsyncMock(side_effect=abort),
    )
    return runtime, prepared


@pytest.mark.asyncio
async def test_session_fork_server_preserves_runtime_commit_and_wire_order() -> None:
    trace: list[str] = []
    runtime, prepared = _runtime_adapter(trace=trace)
    ws = SimpleNamespace(
        send=AsyncMock(side_effect=lambda _wire: trace.append("response.send"))
    )

    await _server(runtime)._handle_session_fork(ws, _request(), asyncio.Lock())

    assert trace == [
        "runtime.start",
        "runtime.prepare",
        "runtime.commit",
        "response.send",
    ]
    runtime.prepare_session_fork.assert_awaited_once_with(
        SessionForkInput(
            channel_id="tui",
            source_session_id="fork-source",
            target_session_id="fork-target",
            title="Forked session",
        )
    )
    runtime.commit_session_provision.assert_awaited_once_with(
        prepared,
        timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
    )
    runtime.abort_session_provision.assert_not_awaited()
    response = _parse_sent_response(ws)
    assert response.ok is True
    assert response.payload == {
        "session_id": "fork-target",
        "source_session_id": "fork-source",
        "title": "Forked session",
    }
    assert response.metadata is None


@pytest.mark.asyncio
async def test_session_fork_crosses_real_runtime_boundary_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common import session_ops_service

    trace: list[str] = []
    card = object()
    deep_agent = SimpleNamespace(card=card)

    async def initialize() -> None:
        trace.append("runtime.start")

    async def ensure_instance() -> Any:
        trace.append("agent.ensure")
        return deep_agent

    def get_agent_nowait(channel_id: str) -> Any:
        assert channel_id == "tui"
        trace.append("agent.lookup")
        return SimpleNamespace(ensure_instance=ensure_instance)

    def fork_session(**kwargs: Any) -> dict[str, str]:
        assert kwargs == {
            "source_session_id": "fork-source",
            "target_session_id": "fork-target",
            "title": "Forked session",
            "channel_id": "tui",
        }
        trace.append("fork.filesystem")
        return {
            "session_id": "fork-target",
            "source_session_id": "fork-source",
            "title": "Forked session",
        }

    async def copy_session_context(
        selected_agent: Any,
        source_session_id: str,
        target_session_id: str,
    ) -> bool:
        assert selected_agent is deep_agent
        assert (source_session_id, target_session_id) == (
            "fork-source",
            "fork-target",
        )
        trace.append("fork.context")
        return True

    async def copy_session_state(**kwargs: Any) -> bool:
        assert kwargs == {
            "source_session_id": "fork-source",
            "target_session_id": "fork-target",
            "card": card,
            "deep_agent": deep_agent,
        }
        trace.append("fork.state")
        return True

    manager = SimpleNamespace(
        create_session=AsyncMock(
            side_effect=AssertionError("explicit fork must not allocate a target")
        ),
        get_agent_nowait=get_agent_nowait,
        cancel_all_inflight_work=AsyncMock(),
        cleanup=AsyncMock(),
    )
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=initialize,
        plan_controller=SimpleNamespace(reset_session=lambda _session_id: None),
    )
    monkeypatch.setattr(session_ops_service, "fork_session", fork_session)
    monkeypatch.setattr(
        session_ops_service,
        "copy_session_context",
        copy_session_context,
    )
    monkeypatch.setattr(
        session_ops_service,
        "copy_session_state",
        copy_session_state,
    )
    ws = SimpleNamespace(
        send=AsyncMock(side_effect=lambda _wire: trace.append("response.send"))
    )

    try:
        await _server(runtime)._handle_session_fork(
            ws,
            _request(),
            asyncio.Lock(),
        )

        assert trace == [
            "runtime.start",
            "fork.filesystem",
            "agent.lookup",
            "agent.ensure",
            "fork.context",
            "fork.state",
            "response.send",
        ]
        manager.create_session.assert_not_awaited()
        response = _parse_sent_response(ws)
        assert response.ok is True
        assert response.payload == {
            "session_id": "fork-target",
            "source_session_id": "fork-source",
            "title": "Forked session",
        }
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_session_fork_server_leaves_auto_target_to_runtime() -> None:
    trace: list[str] = []
    runtime, prepared = _runtime_adapter(
        trace=trace,
        result=_fork_result("allocated-target"),
    )
    ws = SimpleNamespace(send=AsyncMock())

    await _server(runtime)._handle_session_fork(
        ws,
        _request(target_session_id=None),
        asyncio.Lock(),
    )

    runtime.prepare_session_fork.assert_awaited_once_with(
        SessionForkInput(
            channel_id="tui",
            source_session_id="fork-source",
            target_session_id=None,
            title="Forked session",
        )
    )
    response = _parse_sent_response(ws)
    assert response.payload["session_id"] == "allocated-target"


@pytest.mark.asyncio
async def test_session_fork_server_preserves_channel_id_without_trimming() -> None:
    trace: list[str] = []
    runtime, _ = _runtime_adapter(trace=trace)
    ws = SimpleNamespace(send=AsyncMock())

    await _server(runtime)._handle_session_fork(
        ws,
        _request(channel_id=" tui "),
        asyncio.Lock(),
    )

    provision_input = runtime.prepare_session_fork.await_args.args[0]
    assert provision_input.channel_id == " tui "


@pytest.mark.asyncio
async def test_session_fork_missing_source_stays_before_runtime_start() -> None:
    trace: list[str] = []
    runtime, _ = _runtime_adapter(trace=trace)
    ws = SimpleNamespace(send=AsyncMock())

    await _server(runtime)._handle_session_fork(
        ws,
        _request(source_session_id="  "),
        asyncio.Lock(),
    )

    runtime.start.assert_not_awaited()
    runtime.prepare_session_fork.assert_not_awaited()
    response = _parse_sent_response(ws)
    assert response.ok is False
    assert response.payload == {
        "error": "source_session_id is required",
        "code": "BAD_REQUEST",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_payload"),
    [
        (
            SessionProvisionError(
                "source session not found",
                code="NOT_FOUND",
            ),
            {"error": "source session not found", "code": "NOT_FOUND"},
        ),
        (
            SessionProvisionError("fork rejected"),
            {"error": "fork rejected"},
        ),
        (
            RuntimeError("fork exploded"),
            {"error": "fork exploded"},
        ),
    ],
)
async def test_session_fork_runtime_errors_keep_legacy_wire(
    error: BaseException,
    expected_payload: dict[str, str],
) -> None:
    trace: list[str] = []
    runtime, _ = _runtime_adapter(trace=trace, prepare_error=error)
    ws = SimpleNamespace(send=AsyncMock())

    await _server(runtime)._handle_session_fork(ws, _request(), asyncio.Lock())

    response = _parse_sent_response(ws)
    assert response.ok is False
    assert response.payload == expected_payload
    assert response.metadata is None


@pytest.mark.asyncio
async def test_session_fork_commit_error_aborts_uncommitted_lease() -> None:
    trace: list[str] = []
    runtime, prepared = _runtime_adapter(
        trace=trace,
        commit_error=RuntimeError("commit failed"),
    )
    ws = SimpleNamespace(send=AsyncMock())

    await _server(runtime)._handle_session_fork(ws, _request(), asyncio.Lock())

    response = _parse_sent_response(ws)
    assert response.ok is False
    assert response.payload == {"error": "commit failed"}
    runtime.abort_session_provision.assert_awaited_once_with(prepared)
    assert prepared.state is SessionProvisionState.ABORTED


@pytest.mark.asyncio
async def test_session_fork_abort_error_does_not_replace_commit_error() -> None:
    trace: list[str] = []
    runtime, prepared = _runtime_adapter(
        trace=trace,
        commit_error=RuntimeError("commit failed"),
        abort_error=RuntimeError("abort failed"),
    )
    ws = SimpleNamespace(send=AsyncMock())

    await _server(runtime)._handle_session_fork(ws, _request(), asyncio.Lock())

    response = _parse_sent_response(ws)
    assert response.ok is False
    assert response.payload == {"error": "commit failed"}
    runtime.abort_session_provision.assert_awaited_once_with(prepared)
    assert prepared.state is SessionProvisionState.PREPARED


@pytest.mark.asyncio
async def test_session_fork_abort_error_does_not_replace_cancellation() -> None:
    trace: list[str] = []
    runtime, prepared = _runtime_adapter(
        trace=trace,
        commit_error=asyncio.CancelledError(),
        abort_error=RuntimeError("abort failed"),
    )
    ws = SimpleNamespace(send=AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await _server(runtime)._handle_session_fork(
            ws,
            _request(),
            asyncio.Lock(),
        )

    ws.send.assert_not_awaited()
    runtime.abort_session_provision.assert_awaited_once_with(prepared)
    assert prepared.state is SessionProvisionState.PREPARED


@pytest.mark.asyncio
async def test_session_fork_abort_cancellation_preserves_primary_cancellation() -> None:
    trace: list[str] = []
    commit_cancelled = asyncio.CancelledError("commit cancelled")
    runtime, prepared = _runtime_adapter(
        trace=trace,
        commit_error=commit_cancelled,
        abort_error=asyncio.CancelledError("abort cancelled"),
    )
    ws = SimpleNamespace(send=AsyncMock())

    with pytest.raises(asyncio.CancelledError) as caught:
        await _server(runtime)._handle_session_fork(
            ws,
            _request(),
            asyncio.Lock(),
        )

    assert caught.value is commit_cancelled
    ws.send.assert_not_awaited()
    runtime.abort_session_provision.assert_awaited_once_with(prepared)
    assert prepared.state is SessionProvisionState.PREPARED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_code"),
    [
        ("source session not found", "NOT_FOUND"),
        ("target session already exists", "ALREADY_EXISTS"),
        ("runtime is not ready", "BAD_REQUEST"),
    ],
)
async def test_session_fork_runtime_start_value_error_keeps_legacy_code(
    message: str,
    expected_code: str,
) -> None:
    trace: list[str] = []
    runtime, _ = _runtime_adapter(trace=trace)
    runtime.start.side_effect = ValueError(message)
    ws = SimpleNamespace(send=AsyncMock())

    await _server(runtime)._handle_session_fork(ws, _request(), asyncio.Lock())

    runtime.prepare_session_fork.assert_not_awaited()
    response = _parse_sent_response(ws)
    assert response.payload == {"error": message, "code": expected_code}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_stage", ["prepare", "commit"])
async def test_session_fork_cancellation_propagates_without_wire(
    cancel_stage: str,
) -> None:
    trace: list[str] = []
    runtime, prepared = _runtime_adapter(
        trace=trace,
        prepare_error=(asyncio.CancelledError() if cancel_stage == "prepare" else None),
        commit_error=(asyncio.CancelledError() if cancel_stage == "commit" else None),
    )
    ws = SimpleNamespace(send=AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await _server(runtime)._handle_session_fork(
            ws,
            _request(),
            asyncio.Lock(),
        )

    ws.send.assert_not_awaited()
    if cancel_stage == "prepare":
        runtime.abort_session_provision.assert_not_awaited()
    else:
        runtime.abort_session_provision.assert_awaited_once_with(prepared)
        assert prepared.state is SessionProvisionState.ABORTED


@pytest.mark.asyncio
async def test_session_fork_send_failure_does_not_abort_committed_result() -> None:
    trace: list[str] = []
    runtime, _ = _runtime_adapter(trace=trace)
    ws = SimpleNamespace(send=AsyncMock(side_effect=[OSError("send failed"), None]))

    await _server(runtime)._handle_session_fork(ws, _request(), asyncio.Lock())

    runtime.commit_session_provision.assert_awaited_once()
    runtime.abort_session_provision.assert_not_awaited()
    assert ws.send.await_count == 2
    error_response = parse_agent_server_wire_unary(
        json.loads(ws.send.await_args_list[1].args[0])
    )
    assert error_response.ok is False
    assert error_response.payload == {"error": "send failed"}


def test_session_fork_handler_keeps_transport_and_runtime_boundary() -> None:
    source = inspect.getsource(AgentWebSocketServer._handle_session_fork)

    for forbidden in (
        "self._agent_manager",
        "create_or_resume_session",
        "session_ops_service",
        "fork_session",
        "copy_session_context",
        "copy_session_state",
        "AgentCard",
    ):
        assert forbidden not in source
    assert "prepare_session_fork" in source
    assert "commit_session_provision" in source
    assert "send_lock" in source
    assert "send_wire_payload" in source
