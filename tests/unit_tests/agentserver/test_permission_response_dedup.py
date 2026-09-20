# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for permission response deduplication at the facade."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jiuwenswarm.common.schema.agent import (
    AgentRequest,
    AgentResponse,
    AgentResponseChunk,
)
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.session.permission_response_ledger import (
    PermissionResponseLedger,
)


class _PermissionAdapter:
    def __init__(self) -> None:
        self.runtime_calls: list[str] = []

    async def handle_heartbeat(self, _request: AgentRequest) -> None:
        return None

    async def process_message_impl(
        self,
        request: AgentRequest,
        _inputs: dict[str, Any],
    ) -> AgentResponse:
        self.runtime_calls.append(request.params["request_id"])
        await asyncio.sleep(0)
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={"content": "", "files": []},
        )

    async def process_message_stream_impl(
        self,
        request: AgentRequest,
        _inputs: dict[str, Any],
    ):
        self.runtime_calls.append(request.params["request_id"])
        await asyncio.sleep(0)
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={"event_type": "chat.done"},
            is_complete=True,
        )


def _permission_request(
    continuation_id: str,
    *,
    request_id: str,
    req_method: ReqMethod = ReqMethod.CHAT_SEND,
    stream: bool = False,
    mode: str = "agent",
) -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        channel_id="web",
        session_id="session-1",
        req_method=req_method,
        params={
            "query": "",
            "request_id": continuation_id,
            "answers": [{"selected_options": ["approve"]}],
            "source": "permission_interrupt",
            "mode": mode,
        },
        is_stream=stream,
    )


@pytest.mark.parametrize("req_method", [ReqMethod.CHAT_SEND, ReqMethod.CHAT_RESUME])
def test_permission_response_methods_are_eligible(req_method: ReqMethod) -> None:
    request = _permission_request(
        "permission-1",
        request_id="transport-1",
        req_method=req_method,
    )

    assert interface_module._permission_response_key(request) == "permission-1"


def test_ask_user_response_is_not_treated_as_permission_response() -> None:
    request = _permission_request(
        "shared-tool-call-id",
        request_id="transport-ask-user",
    )
    request.params["source"] = "ask_user_interrupt"

    assert interface_module._permission_response_key(request) is None


def test_recent_permission_ledger_is_bounded() -> None:
    ledger = PermissionResponseLedger(max_recent_keys=2)

    for response_id in ("permission-1", "permission-2", "permission-3"):
        reservation = ledger.reserve("session-1", response_id)
        assert reservation is not None
        assert reservation.start() is True
        reservation.complete()

    assert ledger.reserve("session-1", "permission-1") is not None
    assert ledger.reserve("session-1", "permission-2") is None
    assert ledger.reserve("session-1", "permission-3") is None


def test_permission_reservation_state_is_read_only() -> None:
    ledger = PermissionResponseLedger()
    reservation = ledger.reserve("session-1", "permission-1")
    assert reservation is not None

    with pytest.raises(AttributeError):
        setattr(reservation, "key", ("session-1", "replacement"))
    with pytest.raises(AttributeError):
        setattr(reservation, "started", True)


def _regular_request(*, request_id: str) -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        channel_id="web",
        session_id="session-1",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "hello",
            "request_id": "regular-message",
            "mode": "agent",
        },
    )


def _build_swarm(monkeypatch: pytest.MonkeyPatch, adapter: _PermissionAdapter):
    swarm = interface_module.JiuWenSwarm()
    swarm._adapter = adapter
    swarm._sdk_name = "harness"

    def _build_inputs(request: AgentRequest):
        if request.params.get("mode") == "team":
            return (
                {
                    "query": swarm._build_interactive_input_from_answers(
                        request.params["request_id"],
                        request.params["answers"],
                        request.params["source"],
                    )
                },
                "local",
                "",
            )
        return {}, "local", ""

    monkeypatch.setattr(swarm, "_build_inputs", _build_inputs)
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    monkeypatch.setattr(interface_module, "_schedule_symphony_session_feedback", lambda *_args: None)
    return swarm


@pytest.mark.asyncio
async def test_unary_duplicate_permission_runs_runtime_once(monkeypatch) -> None:
    adapter = _PermissionAdapter()
    swarm = _build_swarm(monkeypatch, adapter)

    try:
        responses = await asyncio.gather(*(
            swarm.process_message(
                _permission_request("permission-1", request_id=f"web-{index}")
            )
            for index in range(3)
        ))

        assert adapter.runtime_calls == ["permission-1"]
        assert sum(
            response.payload.get("code") == "duplicate_permission_response"
            for response in responses
        ) == 2

        replay = await swarm.process_message(
            _permission_request("permission-1", request_id="web-replay")
        )
        assert replay.payload == {
            "code": "duplicate_permission_response",
            "deduplicated": True,
        }
        assert adapter.runtime_calls == ["permission-1"]
    finally:
        await swarm._session_manager.close_all_sessions()


@pytest.mark.asyncio
async def test_team_stream_duplicate_permission_runs_runtime_once(monkeypatch) -> None:
    adapter = _PermissionAdapter()
    swarm = _build_swarm(monkeypatch, adapter)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda _channel_id: object(),
    )

    async def collect(request: AgentRequest) -> list[AgentResponseChunk]:
        return [chunk async for chunk in swarm.process_message_stream(request)]

    try:
        results = await asyncio.gather(*(
            collect(
                _permission_request(
                    "permission-1",
                    request_id=f"stream-{index}",
                    stream=True,
                    mode="team",
                )
            )
            for index in range(3)
        ))

        assert adapter.runtime_calls == ["permission-1"]
        assert sum(
            chunks[-1].payload.get("code") == "duplicate_permission_response"
            for chunks in results
        ) == 2
    finally:
        await swarm._session_manager.close_all_sessions()


@pytest.mark.asyncio
async def test_permission_response_id_is_opaque(monkeypatch) -> None:
    adapter = _PermissionAdapter()
    swarm = _build_swarm(monkeypatch, adapter)

    try:
        responses = await asyncio.gather(
            swarm.process_message(
                _permission_request("permission-1", request_id="web-a")
            ),
            swarm.process_message(
                _permission_request(" permission-1 ", request_id="web-b")
            ),
        )

        assert sorted(adapter.runtime_calls) == [" permission-1 ", "permission-1"]
        assert all(
            response.payload.get("code") != "duplicate_permission_response"
            for response in responses
        )
    finally:
        await swarm._session_manager.close_all_sessions()


@pytest.mark.asyncio
async def test_regular_messages_are_not_deduplicated(monkeypatch) -> None:
    adapter = _PermissionAdapter()
    swarm = _build_swarm(monkeypatch, adapter)

    try:
        responses = await asyncio.gather(
            swarm.process_message(_regular_request(request_id="web-a")),
            swarm.process_message(_regular_request(request_id="web-b")),
        )

        assert adapter.runtime_calls == ["regular-message", "regular-message"]
        assert all(
            response.payload.get("code") != "duplicate_permission_response"
            for response in responses
        )
    finally:
        await swarm._session_manager.close_all_sessions()


@pytest.mark.asyncio
async def test_cancelled_queued_permission_releases_retry_without_stale_execution(
    monkeypatch,
) -> None:
    adapter = _PermissionAdapter()
    swarm = _build_swarm(monkeypatch, adapter)
    queued = asyncio.Event()
    never = asyncio.Event()
    stale_task = None
    submit_count = 0

    async def submit_and_wait(_session_id, task_func):
        nonlocal stale_task, submit_count
        submit_count += 1
        if submit_count == 1:
            stale_task = task_func
            queued.set()
            await never.wait()
        return await task_func()

    monkeypatch.setattr(swarm._session_manager, "submit_and_wait", submit_and_wait)

    first = asyncio.create_task(
        swarm.process_message(
            _permission_request("permission-1", request_id="web-cancelled")
        )
    )
    await asyncio.wait_for(queued.wait(), timeout=1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    retry = await swarm.process_message(
        _permission_request("permission-1", request_id="web-retry")
    )
    assert retry.payload.get("code") != "duplicate_permission_response"
    assert adapter.runtime_calls == ["permission-1"]

    assert stale_task is not None
    stale_result = await stale_task()
    assert stale_result.payload == {
        "code": "duplicate_permission_response",
        "deduplicated": True,
    }
    assert adapter.runtime_calls == ["permission-1"]


def test_abandon_does_not_remember_recent_key() -> None:
    ledger = PermissionResponseLedger()
    reservation = ledger.reserve("session-1", "permission-1")
    assert reservation is not None
    assert reservation.start() is True
    reservation.abandon()

    retry = ledger.reserve("session-1", "permission-1")
    assert retry is not None
    assert retry.start() is True
    retry.complete()
    assert ledger.reserve("session-1", "permission-1") is None


def test_team_landing_settle_abandons_when_resume_did_not_land() -> None:
    from jiuwenswarm.server.runtime.session.permission_response_ledger import (
        reset_team_permission_resume_landed,
        settle_permission_reservation,
    )

    reset_team_permission_resume_landed()
    ledger = PermissionResponseLedger()
    reservation = ledger.reserve("session-1", "permission-1")
    assert reservation is not None
    assert reservation.start() is True
    settle_permission_reservation(reservation, settle_by_team_landing=True)
    retry = ledger.reserve("session-1", "permission-1")
    assert retry is not None


def test_team_landing_settle_completes_when_resume_landed() -> None:
    from jiuwenswarm.server.runtime.session.permission_response_ledger import (
        mark_team_permission_resume_landed,
        reset_team_permission_resume_landed,
        settle_permission_reservation,
    )

    reset_team_permission_resume_landed()
    ledger = PermissionResponseLedger()
    reservation = ledger.reserve("session-1", "permission-1")
    assert reservation is not None
    assert reservation.start() is True
    mark_team_permission_resume_landed(True)
    settle_permission_reservation(reservation, settle_by_team_landing=True)
    assert ledger.reserve("session-1", "permission-1") is None


@pytest.mark.asyncio
async def test_team_stream_unlanded_permission_can_retry(monkeypatch) -> None:
    adapter = _PermissionAdapter()
    swarm = _build_swarm(monkeypatch, adapter)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda _channel_id: object(),
    )

    async def collect(request: AgentRequest) -> list[AgentResponseChunk]:
        return [chunk async for chunk in swarm.process_message_stream(request)]

    try:
        first = await collect(
            _permission_request(
                "permission-1",
                request_id="stream-unlanded",
                stream=True,
                mode="team",
            )
        )
        assert adapter.runtime_calls == ["permission-1"]
        assert first[-1].payload.get("code") != "duplicate_permission_response"

        retry = await collect(
            _permission_request(
                "permission-1",
                request_id="stream-unlanded-retry",
                stream=True,
                mode="team",
            )
        )
        assert adapter.runtime_calls == ["permission-1", "permission-1"]
        assert retry[-1].payload.get("code") != "duplicate_permission_response"
    finally:
        await swarm._session_manager.close_all_sessions()


@pytest.mark.asyncio
async def test_team_stream_landed_permission_is_deduplicated(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.session.permission_response_ledger import (
        mark_team_permission_resume_landed,
    )

    class _LandingAdapter(_PermissionAdapter):
        async def process_message_stream_impl(
            self,
            request: AgentRequest,
            _inputs: dict[str, Any],
        ):
            mark_team_permission_resume_landed(True)
            async for chunk in super().process_message_stream_impl(request, _inputs):
                yield chunk

    adapter = _LandingAdapter()
    swarm = _build_swarm(monkeypatch, adapter)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda _channel_id: object(),
    )

    async def collect(request: AgentRequest) -> list[AgentResponseChunk]:
        return [chunk async for chunk in swarm.process_message_stream(request)]

    try:
        first = await collect(
            _permission_request(
                "permission-1",
                request_id="stream-landed",
                stream=True,
                mode="team",
            )
        )
        assert adapter.runtime_calls == ["permission-1"]
        assert first[-1].payload.get("code") != "duplicate_permission_response"

        retry = await collect(
            _permission_request(
                "permission-1",
                request_id="stream-landed-retry",
                stream=True,
                mode="team",
            )
        )
        assert adapter.runtime_calls == ["permission-1"]
        assert retry[-1].payload == {
            "code": "duplicate_permission_response",
            "deduplicated": True,
        }
    finally:
        await swarm._session_manager.close_all_sessions()
