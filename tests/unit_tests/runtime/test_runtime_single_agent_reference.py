# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jiuwenswarm.channels.process_cli.client import InProcessRuntimeClient
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.runtime.session import SessionWorkKind
from jiuwenswarm.runtime.session.model import SessionExecutionState

pytestmark = pytest.mark.unit


class _Manager:
    def __init__(self) -> None:
        self.cleanup_sessions: list[tuple[str, str]] = []

    async def create_session(self, *, channel_id: str, session_id: str | None) -> str:
        return session_id or "allocated"

    async def cancel_all_inflight_work(self, _reason: str) -> None:
        return None

    async def cleanup_session_runtime(self, *, channel_id: str, session_id: str) -> bool:
        self.cleanup_sessions.append((channel_id, session_id))
        return True

    async def cleanup(self) -> None:
        return None


class _Plan:
    def reset_session(self, _session_id: str) -> None:
        return None


async def _initialize() -> None:
    return None


def _runtime():
    return AgentRuntime(
        agent_manager=_Manager(),
        initializer=_initialize,
        plan_controller=_Plan(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "work_mode"),
    [("agent.work.normal", "work"), ("agent.code.normal", "code")],
)
async def test_work_and_code_normal_unary_use_session_registry(
    mode: str, work_mode: str
) -> None:
    runtime = _runtime()
    await runtime.start()
    session_id = await runtime.create_or_resume_session(
        channel_id="process", session_id=f"{work_mode}-session"
    )

    async def invoke_started(request: AgentRequest, **_kwargs: Any):
        return [request.request_id]

    runtime._invoke_started = invoke_started  # type: ignore[method-assign]
    request = AgentRequest(
        request_id=f"{work_mode}-request",
        channel_id="process",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": mode, "work_mode": work_mode},
    )
    assert await runtime.invoke(request) == [request.request_id]
    snapshot = runtime.session_coordinator.snapshot_session(session_id)
    assert snapshot is not None
    assert snapshot.executions[-1].state is SessionExecutionState.SUCCEEDED
    assert snapshot.executions[-1].work_kind is SessionWorkKind.CHAT_UNARY
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "work_mode"),
    [("agent.work.normal", "work"), ("agent.code.normal", "code")],
)
async def test_work_and_code_normal_stream_use_session_registry(
    mode: str, work_mode: str
) -> None:
    runtime = _runtime()
    await runtime.start()
    session_id = await runtime.create_or_resume_session(
        channel_id="process", session_id=f"{work_mode}-session"
    )

    async def stream_started(request: AgentRequest, **_kwargs: Any):
        yield request.request_id

    runtime._stream_started = stream_started  # type: ignore[method-assign]
    request = AgentRequest(
        request_id=f"{work_mode}-request",
        channel_id="process",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": mode, "work_mode": work_mode},
        is_stream=True,
    )
    assert [item async for item in runtime.stream(request)] == [request.request_id]
    snapshot = runtime.session_coordinator.snapshot_session(session_id)
    assert snapshot is not None
    assert snapshot.executions[-1].state is SessionExecutionState.SUCCEEDED
    assert snapshot.executions[-1].work_kind is SessionWorkKind.CHAT_STREAM
    await runtime.close()


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("set", SessionWorkKind.GOAL_STREAM),
        ("resume", SessionWorkKind.GOAL_STREAM),
        ("get", SessionWorkKind.GOAL_CONTROL),
        ("pause", SessionWorkKind.GOAL_CONTROL),
        ("clear", SessionWorkKind.GOAL_CONTROL),
    ],
)
def test_goal_commands_are_native_session_runtime_work(
    action: str, expected: SessionWorkKind
) -> None:
    request = AgentRequest(
        request_id=f"goal-{action}",
        channel_id="web",
        session_id="goal-session",
        req_method=ReqMethod.COMMAND_GOAL,
        is_stream=True,
        params={"action": action, "mode": "agent", "work_mode": "work"},
    )

    assert AgentRuntime.session_work_kind(request) is expected
    assert AgentRuntime.uses_session_runtime(request)


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"attach_goal": True}, SessionWorkKind.GOAL_ATTACH),
        ({"input_mode": "steer"}, SessionWorkKind.CONTROL_INPUT),
        ({"runtime_mode": "follow_up"}, SessionWorkKind.CONTROL_INPUT),
    ],
)
def test_goal_delivery_is_runtime_control_work(
    params: dict[str, object], expected: SessionWorkKind
) -> None:
    request = AgentRequest(
        request_id="goal-control",
        channel_id="web",
        session_id="goal-session",
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        params={"mode": "agent", "work_mode": "work", **params},
    )

    assert AgentRuntime.session_work_kind(request) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agent.work.plan", "team.work.normal"])
async def test_unadapted_modes_keep_their_existing_executor(mode: str) -> None:
    runtime = _runtime()
    await runtime.start()
    await runtime.create_or_resume_session(channel_id="process", session_id="session")

    request = AgentRequest(
        request_id="request",
        channel_id="process",
        session_id="session",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": mode, "work_mode": "work"},
    )
    async def invoke_started(actual: AgentRequest, **_kwargs: Any):
        return [actual.request_id]

    runtime._invoke_started = invoke_started  # type: ignore[method-assign]
    assert await runtime.invoke(request) == [request.request_id]
    snapshot = runtime.session_coordinator.snapshot_session("session")
    assert snapshot is not None
    assert snapshot.executions == ()
    await runtime.close()


@pytest.mark.asyncio
async def test_cancel_runs_semantic_interrupt_before_coordinator_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    await runtime.start()
    await runtime.create_or_resume_session(channel_id="process", session_id="session")
    order: list[str] = []
    started = asyncio.Event()

    async def work() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            order.append("execution-exited")

    running = asyncio.create_task(
        runtime.session_coordinator.run_unary(
            "session", "target", SessionWorkKind.CHAT_UNARY, work
        )
    )
    await started.wait()

    async def semantic_interrupt(*_args: Any, **_kwargs: Any) -> AgentResponse:
        order.append("semantic-interrupt")
        return AgentResponse(request_id="cancel", channel_id="process")

    monkeypatch.setattr(
        "jiuwenswarm.runtime.request.cancel_request", semantic_interrupt
    )
    response = await runtime.cancel_request(
        AgentRequest(
            request_id="cancel",
            channel_id="process",
            session_id="session",
            req_method=ReqMethod.CHAT_CANCEL,
            params={"target_request_id": "target"},
        )
    )
    assert response.ok is True
    with pytest.raises(asyncio.CancelledError):
        await running
    assert order == ["semantic-interrupt", "execution-exited"]
    await runtime.close()


def test_process_client_uses_the_standard_runtime() -> None:
    assert isinstance(InProcessRuntimeClient().runtime, AgentRuntime)


@pytest.mark.asyncio
async def test_closed_session_can_be_registered_as_a_new_generation() -> None:
    runtime = _runtime()
    await runtime.start()
    await runtime.register_session(session_id="session", channel_id="web")
    first = runtime.session_coordinator.snapshot_session("session")
    assert first is not None

    await runtime.session_coordinator.close_session("session")
    assert not runtime.owns_session("session")
    await runtime.register_session(session_id="session", channel_id="tui")

    second = runtime.session_coordinator.snapshot_session("session")
    assert second is not None
    assert second.generation == first.generation + 1
    assert second.channel_id == "tui"
    await runtime.close()


_SINGLE_AGENT_CHANNELS = (
    "web",
    "tui",
    "feishu",
    "feishu_enterprise:tenant-a",
    "xiaoyi",
    "wecom",
    "dingtalk",
    "telegram",
    "discord",
    "slack",
    "whatsapp",
    "wechat",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_id", _SINGLE_AGENT_CHANNELS)
@pytest.mark.parametrize(
    ("mode", "work_mode"),
    [("agent.work.normal", "work"), ("agent.code.normal", "code")],
)
async def test_single_agent_runtime_owns_every_user_channel(
    channel_id: str,
    mode: str,
    work_mode: str,
) -> None:
    runtime = _runtime()
    await runtime.start()

    async def invoke_started(request: AgentRequest, **_kwargs: Any):
        return [request.request_id]

    runtime._invoke_started = invoke_started  # type: ignore[method-assign]
    request = AgentRequest(
        request_id=f"{channel_id}-request",
        channel_id=channel_id,
        session_id=f"{channel_id.replace(':', '-')}-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": mode, "work_mode": work_mode},
    )

    assert await runtime.invoke(request) == [request.request_id]
    snapshot = runtime.session_coordinator.snapshot_session(request.session_id)
    assert snapshot is not None
    assert snapshot.channel_id == channel_id
    assert snapshot.executions[-1].state is SessionExecutionState.SUCCEEDED
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agent.work.plan", "team.work.normal"])
async def test_plan_and_team_work_keep_their_existing_owner(
    mode: str,
) -> None:
    runtime = _runtime()
    await runtime.start()

    async def invoke_started(request: AgentRequest, **_kwargs: Any):
        return [request.request_id]

    runtime._invoke_started = invoke_started  # type: ignore[method-assign]
    request = AgentRequest(
        request_id="unadapted-request",
        channel_id="web",
        session_id="unadapted-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": mode, "work_mode": "work"},
    )

    assert await runtime.invoke(request) == [request.request_id]
    assert runtime.session_coordinator.snapshot_session(request.session_id) is None
    await runtime.close()


@pytest.mark.asyncio
async def test_process_client_routes_plan_to_its_executor() -> None:
    runtime = _runtime()
    await runtime.start()
    await runtime.create_or_resume_session(channel_id="process_cli", session_id="session")
    client = InProcessRuntimeClient(runtime)
    request = AgentRequest(
        request_id="plan-request",
        channel_id="process_cli",
        session_id="session",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent.work.plan", "work_mode": "work"},
        is_stream=True,
    )
    async def stream_started(actual: AgentRequest, **_kwargs: Any):
        yield RuntimeEvent(
            request_id=actual.request_id,
            channel_id=actual.channel_id,
            session_id=actual.session_id,
            payload={"content": "plan"},
            is_complete=True,
        )

    runtime._stream_started = stream_started  # type: ignore[method-assign]
    assert len([item async for item in client.stream(request)]) == 1
    await client.close()
