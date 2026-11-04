# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import (
    SessionRunAdmission,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import AgentRuntime, RuntimeStateError
from jiuwenswarm.runtime.context import (
    get_current_agent_manager,
    get_current_runtime,
    get_runtime_context,
)
from jiuwenswarm.runtime import service as runtime_service_module
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.runtime.plan import PlanStateResult
from jiuwenswarm.runtime.request import prepare_chat_turn
from jiuwenswarm.runtime.session.model import (
    TEAM_ATTACH_ONLY_PARAM,
    TEAM_EXECUTION_CONTEXT_METADATA_KEY,
    CancelExecutionResult,
    RuntimeSessionState,
    SessionCloseTimeoutError,
    SessionExecutionState,
    SessionWorkKind,
    TeamExecutionContext,
    TeamExecutionDetails,
    TeamWaiterKind,
)
from jiuwenswarm.runtime.session_provisioner import (
    SessionCreateResult,
    SessionDescriptor,
    SessionForkInput,
    SessionForkResult,
    SessionProvisionCommitTiming,
    SessionProvisionState,
    SessionSwitchResult,
)

test_logger = logging.getLogger("tests.runtime_service")


async def _collect_events(stream) -> list[RuntimeEvent]:
    return [event async for event in stream]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("work_mode", ["work", "code"])
@pytest.mark.parametrize("session_type", ["agent", "team"])
async def test_runtime_corrects_mode_before_scheduling(monkeypatch, streaming, work_mode, session_type):
    stored_mode = f"{session_type}.{work_mode}.normal"
    wrong_type = "team" if session_type == "agent" else "agent"
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *args, **kwargs: {"mode": stored_mode, "work_mode": work_mode},
    )
    manager = FakeAgentManager()
    runtime = AgentRuntime(agent_manager=manager)
    runtime._started = True
    coordinator = runtime._session_coordinator
    await coordinator.register_session("mode-scheduling", "web")
    runtime._ensure_session_registered = AsyncMock()
    runtime._team_lifecycle = MagicMock()
    runtime._remove_team_response_waiter = AsyncMock()
    entered = {name: asyncio.Event() for name in ("first", "second")}
    release = asyncio.Event()
    order = []
    expected_kind = (
        SessionWorkKind.TEAM_REQUEST if session_type == "team"
        else SessionWorkKind.CHAT_STREAM if streaming else SessionWorkKind.CHAT_UNARY
    )

    async def execute(request):
        await prepare_chat_turn(manager, request, "web", sync_metadata=False)
        assert request.params["mode"] == stored_mode
        if streaming and session_type == "team":
            context = request.metadata[TEAM_EXECUTION_CONTEXT_METADATA_KEY]
            assert isinstance(context, TeamExecutionContext)
            assert context.work_kind is SessionWorkKind.TEAM_REQUEST
        order.append(f"start:{request.request_id}")
        entered[request.request_id].set()
        await release.wait()
        order.append(f"end:{request.request_id}")
        return RuntimeEvent(
            request_id=request.request_id, channel_id="web", session_id=request.session_id,
            payload={"event_type": "chat.final"}, is_complete=True,
        )

    async def stream_started(request, **kwargs):
        yield await execute(request)

    async def invoke_started(request, **kwargs):
        return [await execute(request)]

    monkeypatch.setattr(runtime, "_stream_started", stream_started)
    monkeypatch.setattr(runtime, "_invoke_started", invoke_started)

    def submit(request_id, mode):
        request = AgentRequest(
            request_id=request_id, channel_id="web", session_id="mode-scheduling",
            req_method=ReqMethod.CHAT_SEND, is_stream=streaming,
            params={"query": "hello", "mode": mode, "work_mode": work_mode},
        )
        return asyncio.create_task(
            _collect_events(runtime.stream(request)) if streaming else runtime.invoke(request)
        )

    tasks = [submit("first", stored_mode)]
    try:
        await asyncio.wait_for(entered["first"].wait(), timeout=1)
        tasks.append(submit("second", f"{wrong_type}.{work_mode}.normal"))
        async with asyncio.timeout(1):
            while True:
                snapshot = coordinator.snapshot_session("mode-scheduling")
                executions = [item for item in snapshot.executions if item.request_id == "second"]
                if executions:
                    break
                await asyncio.sleep(0)
        execution = executions[0]
        assert execution.work_kind is expected_kind
        if session_type == "agent":
            assert execution.state is SessionExecutionState.QUEUED
            assert not entered["second"].is_set()
        else:
            assert isinstance(execution.team, TeamExecutionDetails)
            await asyncio.wait_for(entered["second"].wait(), timeout=1)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        assert all(events[0].event_type == "chat.final" for events in results)
        if session_type == "agent":
            assert order == ["start:first", "end:first", "start:second", "end:second"]
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await coordinator.close()


@pytest.mark.parametrize(
    "mode",
    [
        "team.work.normal",
        "team.work.plan",
        "team.code.normal",
        "team.code.plan",
    ],
)
def test_team_modes_use_unscheduled_runtime_work_kind(mode: str) -> None:
    request = AgentRequest(
        request_id="team-request",
        channel_id="web",
        session_id="session-a",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": mode},
    )

    work_kind = AgentRuntime.session_work_kind(request)

    assert work_kind is SessionWorkKind.TEAM_REQUEST
    assert work_kind.scheduled is False


@pytest.mark.asyncio
async def test_team_cross_session_steer_uses_team_request_path() -> None:
    captured: dict[str, object] = {}

    class TeamCoordinator:
        def snapshot_session(self, session_id: str) -> None:
            return None

        def run_team_stream(
            self,
            session_id: str,
            request_id: str,
            work_kind: SessionWorkKind,
            operation: Any,
            **kwargs: Any,
        ) -> AsyncIterator[RuntimeEvent]:
            captured.update(
                session_id=session_id,
                request_id=request_id,
                work_kind=work_kind,
                details=kwargs["details"],
            )

            async def events() -> AsyncIterator[RuntimeEvent]:
                yield RuntimeEvent(
                    request_id=request_id,
                    channel_id="web",
                    session_id=session_id,
                    payload={"event_type": "runtime.accepted"},
                )

            return events()

    runtime = AgentRuntime(agent_manager=FakeAgentManager())
    runtime._started = True
    runtime._session_coordinator = cast(Any, TeamCoordinator())
    runtime._ensure_session_registered = AsyncMock()
    runtime._team_lifecycle = MagicMock(return_value=object())
    request = AgentRequest(
        request_id="team-cross-session",
        channel_id="web",
        session_id="team-session",
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        params={
            "query": "report status",
            "mode": "team.work.normal",
            "input_mode": "steer",
            "_jiuwenswarm_cross_session": {"message_id": "sm-team"},
        },
    )

    events = await _collect_events(runtime.stream(request))

    assert events[0].event_type == "runtime.accepted"
    assert captured["work_kind"] is SessionWorkKind.TEAM_REQUEST
    details = cast(TeamExecutionDetails, captured["details"])
    assert details.session_message_id == "sm-team"


@pytest.mark.asyncio
async def test_team_attach_only_registers_persistent_waiter_kind() -> None:
    """Entering a Team Session as an observer owns a persistent waiter slot."""
    captured: dict[str, object] = {}

    class TeamCoordinator:
        def snapshot_session(self, session_id: str) -> None:
            return None

        def run_team_stream(
            self,
            session_id: str,
            request_id: str,
            work_kind: SessionWorkKind,
            operation: Any,
            **kwargs: Any,
        ) -> AsyncIterator[RuntimeEvent]:
            captured.update(
                session_id=session_id,
                request_id=request_id,
                work_kind=work_kind,
                details=kwargs["details"],
            )

            async def events() -> AsyncIterator[RuntimeEvent]:
                yield RuntimeEvent(
                    request_id=request_id,
                    channel_id="web",
                    session_id=session_id,
                    payload={"event_type": "runtime.accepted"},
                )

            return events()

    runtime = AgentRuntime(agent_manager=FakeAgentManager())
    runtime._started = True
    runtime._session_coordinator = cast(Any, TeamCoordinator())
    runtime._ensure_session_registered = AsyncMock()
    runtime._team_lifecycle = MagicMock(return_value=object())
    request = AgentRequest(
        request_id="team-attach",
        channel_id="web",
        session_id="team-session",
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        params={
            "query": "",
            "mode": "team.work.normal",
            TEAM_ATTACH_ONLY_PARAM: True,
        },
    )

    events = await _collect_events(runtime.stream(request))

    assert events[0].event_type == "runtime.accepted"
    assert captured["work_kind"] is SessionWorkKind.TEAM_REQUEST
    details = cast(TeamExecutionDetails, captured["details"])
    assert details.waiter_kind is TeamWaiterKind.PERSISTENT
    assert details.observation_only is True
    assert details.session_message_id is None


@pytest.mark.parametrize("browser_attached", [False, True])
@pytest.mark.parametrize("managed_design", [False, True])
def test_managed_design_request_keeps_its_own_result_waiter(monkeypatch, browser_attached, managed_design):
    from jiuwenswarm.agents.harness import team

    monkeypatch.setattr(team, "get_team_manager", lambda _: SimpleNamespace(
        has_interactive_waiter=lambda _: browser_attached,
    ))
    runtime = AgentRuntime(agent_manager=FakeAgentManager())
    request = AgentRequest(
        request_id="voice-task", channel_id="video_tool", session_id="design-chat",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "team.design", **({"managed_task_binding": {"endpoint": "gateway", "task_id": "task"}}
                                         if managed_design else {})},
    )
    kind = runtime._team_waiter_kind(request, work_kind=SessionWorkKind.TEAM_REQUEST, background=False)
    expected = (TeamWaiterKind.REQUEST if managed_design else
                TeamWaiterKind.NONE if browser_attached else TeamWaiterKind.PERSISTENT)
    assert kind is expected


class FakeAgentManager:
    def __init__(self) -> None:
        self.cancel_calls: list[str] = []
        self.cancel_exclusion_calls: list[object] = []
        self.cleanup_calls = 0
        self.session_calls: list[tuple[str, str | None]] = []
        self.cancel_error: Exception | None = None
        self.agent = FakeAgent()
        self.wait_calls: list[str | None] = []
        self.agent_calls: list[dict[str, object]] = []
        self.cleanup_session_calls: list[tuple[str, str]] = []
        self.cleanup_session_error: BaseException | None = None
        self.foreground_calls: list[str] = []

    async def cancel_all_inflight_work(
        self,
        reason: str,
        *,
        exclude_session_ids: object = None,
    ) -> None:
        self.cancel_calls.append(reason)
        self.cancel_exclusion_calls.append(exclude_session_ids)
        if self.cancel_error is not None:
            raise self.cancel_error

    async def cleanup(self) -> None:
        self.cleanup_calls += 1

    async def create_session(
        self,
        channel_id: str = "",
        session_id: str | None = None,
    ) -> str:
        self.session_calls.append((channel_id, session_id))
        return session_id or "process_cli_generated"

    async def wait_for_session_prewarm(self, session_id: str | None) -> None:
        self.wait_calls.append(session_id)

    async def get_agent(self, **kwargs: object) -> object:
        self.agent_calls.append(kwargs)
        return self.agent

    async def get_agent_for_request(
        self,
        request: AgentRequest,
        *,
        mode: str | None = None,
        sub_mode: str | None = None,
        admit_request=None,
    ) -> object:
        project_dir = admit_request() if callable(admit_request) else None
        return await self.get_agent(
            channel_id=request.channel_id,
            mode=mode,
            project_dir=project_dir,
            sub_mode=sub_mode,
        )

    def get_agent_nowait(self, *args: object, **kwargs: object) -> None:
        return None

    async def cleanup_session_runtime(
        self,
        *,
        channel_id: str,
        session_id: str,
    ) -> bool:
        self.cleanup_session_calls.append((channel_id, session_id))
        if self.cleanup_session_error is not None:
            raise self.cleanup_session_error
        return True

    async def begin_foreground_chat(self) -> None:
        self.foreground_calls.append("begin")

    async def end_foreground_chat(self) -> None:
        self.foreground_calls.append("end")


class FakeAgent:
    async def process_message(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={"event_type": "chat.final", "content": "done"},
            metadata={"route": "unary"},
        )

    async def execute_message(self, request: AgentRequest) -> AgentResponse:
        return await self.process_message(request)

    async def process_message_stream(self, request: AgentRequest):
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={"event_type": "chat.delta", "delta": "ok"},
            metadata={"route": "stream"},
        )
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={"event_type": "chat.final", "content": "ok"},
            is_complete=True,
            metadata={"route": "stream"},
        )

    async def deliver_control_input(self, request: AgentRequest):
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={"event_type": "runtime.accepted"},
            is_complete=False,
        )
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload=None,
            is_complete=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("case", [
    "smart", "no_callback", "manual", "ordinary", "goal", "control_id",
    "foreign_session", "failed_handoff",
])
async def test_permission_control_ack_precedes_continuation_completion(streaming, case):
    paused, release = asyncio.Event(), asyncio.Event()
    received = []
    # Each parameter owns its persisted Session; sharing one path lets a
    # previous case's asynchronous metadata writer race the next case.
    session_id = f"permission-session-{case}-{streaming}"

    class WaitingAgent(FakeAgent):
        async def process_message_stream(self, request):
            yield AgentResponseChunk(
                request_id=request.request_id, channel_id=request.channel_id,
                payload={"event_type": "chat.ask_user_question", "request_id": "inner-call"},
            )

        async def deliver_control_input(self, request):
            if case == "failed_handoff":
                paused.set()
                raise RuntimeError("handoff failed")
            ack_id = "inner-call" if case == "control_id" else request.request_id
            payload = {"event_type": "runtime.accepted", "request_id": ack_id}
            if case == "foreign_session":
                payload["session_id"] = "other-session"
            yield AgentResponseChunk(
                request_id=request.request_id, channel_id=request.channel_id, payload=payload,
            )
            paused.set()
            await release.wait()
            yield AgentResponseChunk(
                request_id=request.request_id, channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": "continued"}, is_complete=True,
            )

    class WaitingManager(FakeAgentManager):
        def get_agent_for_session_nowait(self, channel_id, session_id):
            return self.agent

    manager = WaitingManager()
    manager.agent = WaitingAgent()
    runtime = AgentRuntime(
        agent_manager=manager, initializer=AsyncMock(), plan_controller=FakePlanController(),
    )
    original = AgentRequest(
        request_id="original", channel_id="web", session_id=session_id,
        req_method=ReqMethod.COMMAND_GOAL if case == "goal" else ReqMethod.CHAT_SEND,
        is_stream=True,
        params={"query": "Continue the task", "mode": "agent", "action": "set", "objective": "finish the task"},
    )
    await _collect_events(runtime.stream(original, trigger_hook=False))
    answer = AgentRequest(
        request_id="answer-transport", channel_id="web", session_id=session_id,
        req_method=ReqMethod.CHAT_SEND, is_stream=streaming,
        params={
            "query": "", "mode": "agent", "request_id": "inner-call",
            "source": "ask_user_interrupt" if case in {"ordinary", "goal"} else "permission_interrupt",
            "answers": [{
                "selected_options": ["allow"],
                **({} if case in {"manual", "ordinary", "goal"} else {"card_id": "opaque-card"}),
            }],
        },
    )
    def record_ack(event):
        assert get_current_runtime() is runtime
        received.append(event)

    callback = AsyncMock(side_effect=record_ack)
    callback_arg = None if case == "no_callback" else callback
    task = asyncio.create_task(
        _collect_events(runtime.stream(answer, trigger_hook=False, on_control_event=callback_arg))
        if streaming else runtime.invoke(answer, trigger_hook=False, on_control_event=callback_arg)
    )
    try:
        try:
            await asyncio.wait_for(paused.wait(), timeout=1)
        except TimeoutError:
            # Surface a failed delivery instead of hiding it behind the gate.
            if task.done():
                await task
            raise
        assert len(received) == (1 if case == "smart" else 0)
        if case == "failed_handoff":
            with pytest.raises(RuntimeError, match="handoff failed"):
                await task
        else:
            assert not task.done()
            release.set()
            events = await asyncio.wait_for(task, timeout=1)
            assert [event.event_type for event in events] == (
                ["chat.final"] if case == "smart" else ["runtime.accepted", "chat.final"]
            )
            if received:
                assert received[0].request_id == "answer-transport"
                assert received[0].payload["request_id"] != answer.params["request_id"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


class AskUserAgent(FakeAgent):
    async def process_message(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={
                "event_type": "chat.ask_user_question",
                "request_id": "call_ask_user",
            },
        )

    async def process_message_stream(self, request: AgentRequest):
        params = request.params if isinstance(request.params, dict) else {}
        if params.get("answers"):
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": "answered"},
                is_complete=True,
            )
            return
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={
                "event_type": "chat.ask_user_question",
                "request_id": "call_ask_user",
            },
            is_complete=True,
        )

    async def deliver_control_input(self, request: AgentRequest):
        async for chunk in self.process_message_stream(request):
            yield chunk


class FakeAdmissionController:
    def __init__(self, *, user_active: bool) -> None:
        self.user_active = user_active
        self.calls: list[str] = []

    def is_user_active(self, session_id: str) -> bool:
        return self.user_active

    async def begin_user(self, session_id: str) -> None:
        self.calls.append("begin")
        if self.user_active:
            raise AssertionError("interrupt resume must not wait for active user")

    async def end_user(self, session_id: str) -> None:
        self.calls.append("end")


class TerminalSentinelAgent(FakeAgent):
    async def process_message_stream(self, request: AgentRequest):
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload=None,
            is_complete=True,
            metadata={"route": "stream"},
        )


class CancellingAgent(FakeAgent):
    async def process_message_stream(self, request: AgentRequest):
        if False:
            yield
        raise asyncio.CancelledError


class FakePlanController:
    def __init__(self) -> None:
        self.reset_calls: list[str] = []

    async def ensure_state(self, *args: object) -> PlanStateResult:
        return PlanStateResult()

    async def check_post_process_exit(
        self,
        *args: object,
    ) -> list[dict[str, object]]:
        return []

    def reset_session(self, session_id: str) -> None:
        self.reset_calls.append(session_id)


@pytest.mark.asyncio
async def test_runtime_kvc_tracking_uses_current_product_hook_contract(
    monkeypatch,
) -> None:
    from jiuwenswarm.runtime.session_lifecycle import RuntimeParticipantRegistry

    events: list[tuple[str, str, object]] = []

    class Participant:
        async def execution_started(self, event) -> None:
            events.append(("started", event.target.descriptor.session_id, event.request_id))

        def execution_finished(self, event) -> None:
            events.append(("finished", event.target.descriptor.session_id, event.succeeded))

    registry = RuntimeParticipantRegistry()
    registry.replace_session_participants(activity=(Participant(),), delete=())
    runtime = AgentRuntime(
        agent_manager=FakeAgentManager(),
        participant_registry=registry,
    )
    request = AgentRequest(
        request_id="kvc-contract",
        channel_id="web",
        session_id="session-a",
        req_method=ReqMethod.CHAT_SEND,
        params={},
    )

    await runtime._record_session_execution_started(request)
    runtime._record_session_execution_finished(request, succeeded=True)

    assert events == [
        ("started", "session-a", "kvc-contract"),
        ("finished", "session-a", True),
    ]


@pytest.mark.asyncio
async def test_runtime_activity_tracking_off_skips_target_and_history(monkeypatch) -> None:
    runtime = AgentRuntime(agent_manager=FakeAgentManager())
    request = AgentRequest(
        request_id="tracking-off",
        channel_id="web",
        session_id="session-off",
        req_method=ReqMethod.CHAT_SEND,
        params={},
    )
    target_lookup = MagicMock(side_effect=AssertionError("target must not be built"))
    monkeypatch.setattr(runtime, "_activity_target", target_lookup)

    started = await runtime._record_session_execution_started(request)
    runtime._record_session_execution_finished(request, succeeded=True)
    prepare_result = await runtime.record_session_input_intent(request)
    await runtime.record_session_inactive(request)

    assert started is False
    assert prepare_result == "disabled"
    target_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_start_and_close_are_idempotent() -> None:
    manager = FakeAgentManager()
    initialize_calls = 0

    async def initialize() -> None:
        nonlocal initialize_calls
        initialize_calls += 1

    runtime = AgentRuntime(agent_manager=manager, initializer=initialize)

    await runtime.start()
    await runtime.start()
    await runtime.close()
    await runtime.close()

    assert initialize_calls == 1
    assert manager.cancel_calls == ["[runtime close] "]
    assert manager.cleanup_calls == 1
    assert runtime.started is False
    assert runtime.closed is True


@pytest.mark.asyncio
async def test_create_or_resume_session_uses_runtime_manager() -> None:
    manager = FakeAgentManager()

    async def initialize() -> None:
        return None

    runtime = AgentRuntime(agent_manager=manager, initializer=initialize)
    await runtime.start()

    created = await runtime.create_or_resume_session(channel_id="process_cli")
    resumed = await runtime.create_or_resume_session(
        channel_id="process_cli",
        session_id="cli-session-1",
    )

    assert created == "process_cli_generated"
    assert resumed == "cli-session-1"
    assert manager.session_calls == [
        ("process_cli", None),
        ("process_cli", "cli-session-1"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "owned"),
    [("agent.work.normal", True), ("agent.work.plan", False)],
)
async def test_session_switch_commit_registers_single_agent_runtime(
    mode: str,
    owned: bool,
) -> None:
    runtime = AgentRuntime(
        agent_manager=FakeAgentManager(),
        initializer=AsyncMock(),
    )
    await runtime.start()
    prepared = MagicMock()
    prepared.state = SessionProvisionState.COMMITTED
    runtime._session_provisioner.commit_session_provision = AsyncMock(
        return_value=SessionSwitchResult(
            channel_id="web",
            session_id="switched-session",
            mode=mode,
        )
    )

    await runtime.commit_session_provision(
        prepared,
        timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
    )

    assert runtime._owns_session("switched-session") is owned
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "owned"),
    [("agent.work.normal", True), ("agent.work.plan", False)],
)
async def test_session_create_commit_registers_single_agent_runtime(
    mode: str,
    owned: bool,
) -> None:
    runtime = AgentRuntime(
        agent_manager=FakeAgentManager(),
        initializer=AsyncMock(),
    )
    await runtime.start()
    prepared = MagicMock()
    prepared.state = SessionProvisionState.COMMITTED
    runtime._session_provisioner.commit_session_provision = AsyncMock(
        return_value=SessionCreateResult(
            channel_id="web",
            session_id="created-session",
            project_id="default_work",
            project_dir="",
            work_mode="work",
            persist_session=False,
            prewarm_hit=False,
            prewarm_status="warming",
            created=True,
            canonical_mode=mode,
        )
    )

    await runtime.commit_session_provision(
        prepared,
        timing=SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY,
    )

    assert runtime._owns_session("created-session") is owned
    await runtime.close()


@pytest.mark.asyncio
async def test_describe_session_returns_transport_neutral_persisted_facts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from jiuwenswarm.common import utils
    from jiuwenswarm.server.runtime.session import session_metadata

    sessions_root = tmp_path / "sessions"
    session_dir = sessions_root / "process_cli_described"
    session_dir.mkdir(parents=True)
    (session_dir / "metadata.json").write_text(
        """{
          "channel_id": "process_cli",
          "mode": "team.code.normal",
          "work_mode": "code",
          "project_id": "project-1",
          "project_dir": "D:/project"
        }""",
        encoding="utf-8",
    )
    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(
        session_metadata,
        "get_agent_sessions_dir",
        lambda: sessions_root,
    )

    async def initialize() -> None:
        return None

    runtime = AgentRuntime(
        agent_manager=FakeAgentManager(),
        initializer=initialize,
    )
    await runtime.start()

    descriptor = await runtime.describe_session(session_id="process_cli_described")

    assert descriptor == SessionDescriptor(
        session_id="process_cli_described",
        channel_id="process_cli",
        mode="team.code.normal",
        work_mode="code",
        project_id="project-1",
        project_dir="D:/project",
    )
    assert await runtime.describe_session(session_id="missing") is None
    assert await runtime.describe_session(session_id="../invalid") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "work_mode"),
    [("agent.work.normal", "work"), ("agent.code.normal", "code")],
)
async def test_session_message_restores_persisted_target_and_starts_new_execution(
    mode: str,
    work_mode: str,
) -> None:
    manager = FakeAgentManager()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
    )
    descriptors = {
        "source": SessionDescriptor(
            session_id="source",
            channel_id="web",
            mode="agent.work.normal",
            work_mode="work",
            user_id="owner",
        ),
        "target": SessionDescriptor(
            session_id="target",
            channel_id="feishu",
            mode=mode,
            work_mode=work_mode,
            project_id="project-1",
            project_dir="D:/project",
            user_id="owner",
        ),
    }
    runtime.describe_session = AsyncMock(
        side_effect=lambda *, session_id: descriptors.get(session_id)
    )
    delivered = asyncio.Event()
    captured: list[AgentRequest] = []

    async def invoke_started(request: AgentRequest, **_kwargs: object):
        captured.append(request)
        delivered.set()
        return []

    runtime._invoke_started = invoke_started  # type: ignore[method-assign]

    receipt = await runtime.send_session_message(
        source_session_id="source",
        target_session_id="target",
        content="continue this work",
        request_id="message-1",
    )

    assert receipt.state is SessionExecutionState.QUEUED
    await asyncio.wait_for(delivered.wait(), timeout=1)
    assert manager.session_calls == [("feishu", "target")]
    assert captured[0].channel_id == "feishu"
    assert captured[0].params == {
        "query": "continue this work",
        "mode": mode,
        "work_mode": work_mode,
        "project_id": "project-1",
        "project_dir": "D:/project",
    }
    execution = runtime._session_coordinator.get_execution(receipt.execution_id)
    assert execution is not None
    assert execution.work_kind is SessionWorkKind.SESSION_MESSAGE
    await runtime.close()


@pytest.mark.asyncio
async def test_session_message_waits_until_target_control_is_resolved() -> None:
    runtime = AgentRuntime(
        agent_manager=FakeAgentManager(),
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
    )
    descriptor = SessionDescriptor(
        session_id="target",
        channel_id="web",
        mode="agent.work.normal",
        work_mode="work",
        user_id="owner",
    )
    runtime.describe_session = AsyncMock(
        side_effect=lambda *, session_id: (
            descriptor
            if session_id == "target"
            else SessionDescriptor(
                session_id="source",
                channel_id="web",
                mode="agent.work.normal",
                work_mode="work",
                user_id="owner",
            )
        )
    )
    await runtime.start()
    await runtime._register_session(session_id="target", channel_id="web")
    await runtime._session_coordinator.run_unary(
        "target",
        "question",
        SessionWorkKind.CHAT_UNARY,
        lambda: asyncio.sleep(0, result="ask"),
        suspension_key=lambda _value: "control-1",
    )
    delivered = asyncio.Event()

    async def invoke_started(_request: AgentRequest, **_kwargs: object):
        delivered.set()
        return []

    runtime._invoke_started = invoke_started  # type: ignore[method-assign]
    await runtime.send_session_message(
        source_session_id="source",
        target_session_id="target",
        content="queued work",
    )
    await asyncio.sleep(0.01)
    assert delivered.is_set() is False

    await runtime._session_coordinator.deliver_control(
        "target",
        "control-1",
        lambda: asyncio.sleep(0, result="answered"),
    )
    await asyncio.wait_for(delivered.wait(), timeout=1)
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_owner", "target_owner"),
    [("source", "target"), ("", "target"), ("source", "")],
)
async def test_session_message_rejects_cross_owner_delivery(
    source_owner: str,
    target_owner: str,
) -> None:
    runtime = AgentRuntime(
        agent_manager=FakeAgentManager(),
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
    )
    runtime.describe_session = AsyncMock(
        side_effect=lambda *, session_id: SessionDescriptor(
            session_id=session_id,
            channel_id="web",
            mode="agent.work.normal",
            work_mode="work",
            user_id=source_owner if session_id == "source" else target_owner,
        )
    )

    with pytest.raises(PermissionError, match="different owners"):
        await runtime.send_session_message(
            source_session_id="source",
            target_session_id="target",
            content="not allowed",
        )
    await runtime.close()


@pytest.mark.asyncio
async def test_session_message_rejects_target_without_persisted_channel() -> None:
    runtime = AgentRuntime(
        agent_manager=FakeAgentManager(),
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
    )
    runtime.describe_session = AsyncMock(
        side_effect=lambda *, session_id: SessionDescriptor(
            session_id=session_id,
            channel_id="web" if session_id == "source" else "",
            mode="agent.work.normal",
            work_mode="work",
            user_id="owner",
        )
    )

    with pytest.raises(ValueError, match="target session has no channel_id"):
        await runtime.send_session_message(
            source_session_id="source",
            target_session_id="target",
            content="not routable",
        )
    await runtime.close()


@pytest.mark.asyncio
async def test_session_operations_require_started_runtime() -> None:
    runtime = AgentRuntime(agent_manager=FakeAgentManager(), initializer=lambda: None)

    with pytest.raises(RuntimeStateError, match="not started"):
        await runtime.create_or_resume_session(channel_id="process_cli")
    with pytest.raises(RuntimeStateError, match="not started"):
        await runtime.describe_session(session_id="process_cli_target")


@pytest.mark.asyncio
async def test_prepare_chat_turn_uses_runtime_manager() -> None:
    manager = FakeAgentManager()

    async def initialize() -> None:
        return None

    runtime = AgentRuntime(agent_manager=manager, initializer=initialize)
    await runtime.start()
    request = AgentRequest(
        request_id="process-cli-request",
        channel_id="process_cli",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello", "mode": "agent", "work_mode": "work"},
    )

    mode, sub_mode, agent = await runtime._prepare_chat_turn(
        request,
        "process_cli",
    )

    assert (mode, sub_mode, agent) == ("agent", None, manager.agent)
    assert manager.wait_calls == [None]
    assert manager.agent_calls == [
        {
            "channel_id": "process_cli",
            "mode": "agent",
            "project_dir": None,
            "sub_mode": None,
        }
    ]


@pytest.mark.asyncio
async def test_prepare_chat_turn_requires_atomic_manager_admission() -> None:
    manager = SimpleNamespace(
        wait_for_session_prewarm=AsyncMock(),
        get_agent=AsyncMock(),
    )
    request = AgentRequest(
        request_id="process-cli-request",
        channel_id="process_cli",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello", "mode": "agent", "work_mode": "work"},
    )

    with pytest.raises(TypeError, match="atomic get_agent_for_request"):
        await prepare_chat_turn(
            manager,
            request,
            "process_cli",
        )

    manager.get_agent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("registered", "paused", "adapter_success", "timed_out", "expect_push"),
    [
        (True, True, False, (), True),
        (True, False, True, (), True),
        (True, False, False, (), False),
        (True, True, False, ("observer",), False),
        (False, True, True, (), True),
        (False, False, False, (), False),
    ],
)
async def test_team_pause_publishes_once_after_business_consumers_exit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    registered: bool,
    paused: bool,
    adapter_success: bool,
    timed_out: tuple[str, ...],
    expect_push: bool,
) -> None:
    monkeypatch.setattr(runtime_service_module.logger, "handlers", [caplog.handler])
    order: list[str] = []
    pushes: list[dict[str, Any]] = []
    observer = asyncio.create_task(asyncio.Event().wait())

    async def pause_session(*args: object, **kwargs: object) -> bool:
        order.append("pause")
        return paused

    async def cancel_agent(*args: object, **kwargs: object) -> AgentResponse:
        order.append("adapter")
        return AgentResponse(
            request_id="pause-request",
            channel_id="web",
            payload={"success": adapter_success},
        )

    async def cancel_execution(*args: object, **kwargs: object) -> CancelExecutionResult:
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
        order.append("drained")
        return CancelExecutionResult(matched=1, cancelled=1, timed_out=timed_out)

    async def publish(session_id: str, *, generation: int | None, channel_id: str) -> None:
        if registered:
            assert observer.cancelled()
            assert order[-1] == "drained"
        pushes.append({"session_id": session_id, "generation": generation, "channel_id": channel_id})
        order.append("push")

    coordinator = SimpleNamespace(
        snapshot_session=lambda session_id: (
            SimpleNamespace(
                session_id=session_id,
                generation=2,
                state=RuntimeSessionState.READY,
                executions=(),
            )
            if registered else None
        ),
        cancel_execution=AsyncMock(side_effect=cancel_execution),
    )
    lifecycle = SimpleNamespace(
        pause_session=AsyncMock(side_effect=pause_session),
        publish_pause_completed=AsyncMock(side_effect=publish),
    )
    runtime = AgentRuntime(
        agent_manager=FakeAgentManager(),
        initializer=AsyncMock(),
        session_coordinator=coordinator,
        session_runtime_lifecycle=lifecycle,
    )
    request = AgentRequest(
        request_id="pause-request",
        channel_id="web",
        session_id="session-a",
        req_method=ReqMethod.CHAT_CANCEL,
        params={"mode": "team", "intent": "pause"},
    )
    monkeypatch.setattr("jiuwenswarm.runtime.request.cancel_request", cancel_agent)
    try:
        response = await runtime.cancel_request(request)
    finally:
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)

    assert response.ok is True
    assert len(pushes) == int(expect_push)
    if timed_out:
        assert "skipped after cancellation timeout" in caplog.text
        assert "session_id=session-a generation=2" in caplog.text
        assert "observer" in caplog.text
    if registered:
        assert order[:3] == ["pause", "adapter", "drained"]
    if expect_push:
        assert pushes[0] == {
            "channel_id": "web",
            "session_id": "session-a",
            "generation": 2 if registered else None,
        }
    test_logger.info("Pause order=%s pushes=%s", order, len(pushes))


@pytest.mark.asyncio
@pytest.mark.parametrize("new_state", [RuntimeSessionState.READY, RuntimeSessionState.CLOSED])
async def test_team_pause_does_not_push_completion_to_replaced_session(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    new_state: RuntimeSessionState,
) -> None:
    monkeypatch.setattr(runtime_service_module.logger, "handlers", [caplog.handler])
    snapshot = SimpleNamespace(
        session_id="session-a",
        generation=2,
        state=RuntimeSessionState.READY,
        executions=(),
    )

    async def cancel_agent(*args: object, **kwargs: object) -> AgentResponse:
        snapshot.generation = 3
        snapshot.state = new_state
        return AgentResponse(request_id="pause", channel_id="web", payload={"success": True})

    # Return independent snapshots so replacement cannot mutate the captured generation.
    coordinator = SimpleNamespace(
        snapshot_session=lambda session_id: SimpleNamespace(**vars(snapshot)),
        cancel_execution=AsyncMock(return_value=CancelExecutionResult(matched=0, cancelled=0, timed_out=())),
    )
    publish = AsyncMock()
    runtime = AgentRuntime(
        agent_manager=FakeAgentManager(),
        initializer=AsyncMock(),
        session_coordinator=coordinator,
        session_runtime_lifecycle=SimpleNamespace(
            pause_session=AsyncMock(return_value=False),
            publish_pause_completed=publish,
        ),
    )
    monkeypatch.setattr("jiuwenswarm.runtime.request.cancel_request", cancel_agent)

    response = await runtime.cancel_request(
        AgentRequest(
            request_id="pause",
            channel_id="web",
            session_id="session-a",
            req_method=ReqMethod.CHAT_CANCEL,
            params={"mode": "team", "intent": "pause"},
        )
    )

    assert response.ok is True
    publish.assert_not_awaited()
    assert "Skipped stale Team pause completion" in caplog.text
    assert "generation=2 current_generation=3" in caplog.text
    test_logger.info("Suppressed pause completion after Session replacement: state=%s", new_state)


@pytest.mark.asyncio
async def test_team_pause_publication_failure_preserves_completed_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish = AsyncMock(side_effect=RuntimeError("Broadcast unavailable"))
    runtime = AgentRuntime(
        agent_manager=FakeAgentManager(),
        initializer=AsyncMock(),
        session_runtime_lifecycle=SimpleNamespace(publish_pause_completed=publish),
    )
    response = AgentResponse(request_id="pause", channel_id="web", payload={"success": True})
    monkeypatch.setattr(
        "jiuwenswarm.runtime.request.cancel_request",
        AsyncMock(return_value=response),
    )

    result = await runtime.cancel_request(
        AgentRequest(
            request_id="pause",
            channel_id="web",
            session_id="session-a",
            req_method=ReqMethod.CHAT_CANCEL,
            params={"mode": "team", "intent": "pause"},
        )
    )

    assert result is response
    publish.assert_awaited_once()
    test_logger.info("Broadcast failure did not replace the pause response")


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["pause", "cancel"])
async def test_team_pause_retains_attach_for_later_mailbox_output(
    monkeypatch: pytest.MonkeyPatch,
    intent: str,
) -> None:
    from contextlib import AsyncExitStack, aclosing

    from jiuwenswarm.agents.harness.team import TeamManager
    from jiuwenswarm.runtime.session import RuntimeSessionCoordinator
    from jiuwenswarm.runtime.team_session_lifecycle import TeamSessionLifecycleAdapter
    from jiuwenswarm.server.runtime.agent_adapter.team_helpers import _consume_attach_only_stream

    coordinator = RuntimeSessionCoordinator()
    session = await coordinator.register_session("session-a", "web")
    manager = TeamManager()
    manager.bind_generation("session-a", session.generation)
    monkeypatch.setattr(manager, "pause_session_runtime", AsyncMock(return_value=True))
    monkeypatch.setattr(manager, "stop_session_runtime", AsyncMock(return_value=True))
    lifecycle = TeamSessionLifecycleAdapter(coordinator=coordinator, team_manager=manager)
    runtime = AgentRuntime(
        agent_manager=FakeAgentManager(),
        initializer=AsyncMock(),
        session_coordinator=coordinator,
        session_runtime_lifecycle=lifecycle,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.request.cancel_request",
        AsyncMock(
            return_value=AgentResponse(
                request_id="interrupt",
                channel_id="web",
                payload={"success": True},
            ),
        ),
    )
    attached = asyncio.Event()
    business_started = asyncio.Event()
    terminal_seen = asyncio.Event()
    next_round_seen = asyncio.Event()
    payloads: list[dict[str, Any]] = []
    request = AgentRequest(
        request_id="attach",
        channel_id="web",
        session_id="session-a",
        params={"mode": "team", TEAM_ATTACH_ONLY_PARAM: True},
    )
    startup = AsyncExitStack()

    async def signal_attached() -> None:
        attached.set()

    startup.push_async_callback(signal_attached)

    async def observe() -> None:
        stream = coordinator.run_team_stream(
            "session-a",
            "attach",
            SessionWorkKind.TEAM_REQUEST,
            lambda context: _consume_attach_only_stream(
                request=request,
                team_manager=manager,
                startup=startup,
                generation=context.session.generation,
            ),
            details=TeamExecutionDetails(waiter_kind=TeamWaiterKind.PERSISTENT, observation_only=True),
        )
        async with aclosing(stream):
            async for chunk in stream:
                payloads.append(chunk.payload)
                if chunk.payload.get("is_processing") is False:
                    terminal_seen.set()
                if chunk.payload.get("content") == "mailbox reply after pause":
                    next_round_seen.set()

    async def business_operation(context: TeamExecutionContext) -> AsyncIterator[dict[str, Any]]:
        manager.add_waiter("session-a", "business", asyncio.Queue(), generation=context.session.generation)
        business_started.set()
        try:
            await asyncio.Event().wait()
            yield {}
        finally:
            manager.remove_waiter("session-a", "business", generation=context.session.generation)

    async def business() -> None:
        stream = coordinator.run_team_stream(
            "session-a",
            "business",
            SessionWorkKind.TEAM_REQUEST,
            business_operation,
            details=TeamExecutionDetails(waiter_kind=TeamWaiterKind.PERSISTENT),
        )
        async with aclosing(stream):
            async for _chunk in stream:
                pass

    observer = asyncio.create_task(observe())
    worker = asyncio.create_task(business())
    try:
        await asyncio.wait_for(attached.wait(), timeout=2)
        await asyncio.wait_for(business_started.wait(), timeout=2)
        await runtime.cancel_request(
            AgentRequest(
                request_id="interrupt",
                channel_id="web",
                session_id="session-a",
                req_method=ReqMethod.CHAT_CANCEL,
                params={"mode": "team", "intent": intent},
            )
        )
        await asyncio.gather(worker, return_exceptions=True)
        assert worker.cancelled()
        if intent == "pause":
            await asyncio.wait_for(terminal_seen.wait(), timeout=2)
            assert not observer.done()
            assert [rid for rid, _queue in manager.get_waiters("session-a")] == ["attach"]
            executions = coordinator.snapshot_session("session-a").executions
            attach = next(item for item in executions if item.request_id == "attach")
            assert attach.state is SessionExecutionState.WAITING_FOR_INPUT
            assert attach.cancellation_requested is False
            assert sum(item.get("is_processing") is False for item in payloads) == 1
            coordinator.resume_waiting_for_input("session-a", session.generation)
            await manager.broadcast_event(
                "session-a",
                {"event_type": "chat.delta", "content": "mailbox reply after pause"},
            )
            await asyncio.wait_for(next_round_seen.wait(), timeout=2)
            await coordinator.close_session("session-a")
            await asyncio.gather(observer, return_exceptions=True)
            assert observer.cancelled()
        else:
            await asyncio.gather(observer, return_exceptions=True)
            assert observer.cancelled()
            assert manager.get_waiters("session-a") == []
    finally:
        observer.cancel()
        worker.cancel()
        await asyncio.gather(observer, worker, return_exceptions=True)
        await coordinator.close_session("session-a")
    assert manager.get_waiters("session-a") == []
    test_logger.info("Team interrupt=%s preserved observations only for pause", intent)


@pytest.mark.asyncio
async def test_cancel_and_cleanup_session_are_runtime_operations() -> None:
    manager = FakeAgentManager()

    async def initialize() -> None:
        return None

    runtime = AgentRuntime(agent_manager=manager, initializer=initialize)
    await runtime.start()
    request = AgentRequest(
        request_id="cancel-request",
        channel_id="process_cli",
        session_id="process-cli-session",
        req_method=ReqMethod.CHAT_CANCEL,
        params={},
    )

    response = await runtime.cancel_request(request)
    cleaned = await runtime.cleanup_session(
        channel_id="process_cli",
        session_id="process-cli-session",
    )

    assert response.ok is True
    assert response.payload["event_type"] == "chat.interrupt_result"
    assert cleaned is True
    assert manager.cleanup_session_calls == [
        ("process_cli", "process-cli-session")
    ]


@pytest.mark.asyncio
async def test_cancel_all_inflight_work_delegates_without_starting_runtime() -> None:
    manager = FakeAgentManager()
    initializer = AsyncMock()
    runtime = AgentRuntime(agent_manager=manager, initializer=initializer)
    excluded = ("heartbeat-session",)

    await runtime.cancel_all_inflight_work(
        reason="[gateway ws closed] ",
        exclude_session_ids=excluded,
    )

    assert manager.cancel_calls == ["[gateway ws closed] "]
    assert manager.cancel_exclusion_calls == [{"heartbeat-session"}]
    assert runtime.started is False
    initializer.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_all_materializes_generator_for_every_agent() -> None:
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

    class RecordingCancelAgent:
        def __init__(self) -> None:
            self.calls: list[tuple[str, set[str]]] = []

        async def cancel_inflight_work(
            self,
            reason: str,
            *,
            exclude_session_ids: Iterable[str] | None = None,
        ) -> None:
            self.calls.append((reason, set(exclude_session_ids or ())))

    first = RecordingCancelAgent()
    second = RecordingCancelAgent()
    manager = AgentManager()
    manager.agents = cast(
        Any,
        {
            "tui": {
                "first": first,
                "second": second,
            }
        },
    )
    initializer = AsyncMock()
    runtime = AgentRuntime(agent_manager=manager, initializer=initializer)
    exclusions = (session_id for session_id in ("heartbeat-1", "heartbeat-2"))

    await runtime.cancel_all_inflight_work(
        reason="[gateway ws closed] ",
        exclude_session_ids=exclusions,
    )

    expected = [
        (
            "[gateway ws closed] ",
            {"heartbeat-1", "heartbeat-2"},
        )
    ]
    assert first.calls == expected
    assert second.calls == expected
    assert runtime.started is False
    initializer.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_all_inflight_work_rejects_closed_runtime() -> None:
    manager = FakeAgentManager()
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock())
    await runtime.close()
    manager.cancel_calls.clear()
    manager.cancel_exclusion_calls.clear()

    with pytest.raises(RuntimeStateError, match="runtime is already closed"):
        await runtime.cancel_all_inflight_work()

    assert manager.cancel_calls == []
    assert manager.cancel_exclusion_calls == []


@pytest.mark.asyncio
async def test_cancel_resolves_same_composed_mode_and_project_as_execution() -> None:
    class RecordingManager(FakeAgentManager):
        def __init__(self) -> None:
            super().__init__()
            self.nowait_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def get_agent_nowait(self, *args: object, **kwargs: object) -> object:
            self.nowait_calls.append((args, kwargs))
            return self.agent

    manager = RecordingManager()
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock())
    request = AgentRequest(
        request_id="cancel-code-project",
        channel_id="tui",
        session_id="session-code-project",
        req_method=ReqMethod.CHAT_CANCEL,
        params={
            "intent": "cancel",
            "mode": "agent",
            "work_mode": "code",
            "project_dir": "D:/workspace/project-a",
        },
    )

    await runtime.cancel_request(request)

    assert manager.nowait_calls == [
        (
            ("tui",),
            {
                "mode": "code",
                "project_dir": "D:/workspace/project-a",
                "sub_mode": "normal",
            },
        )
    ]


@pytest.mark.asyncio
async def test_cancel_resolves_project_id_inside_target_runtime(monkeypatch) -> None:
    class RecordingManager(FakeAgentManager):
        def __init__(self) -> None:
            super().__init__()
            self.nowait_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def get_agent_nowait(self, *args: object, **kwargs: object) -> object:
            self.nowait_calls.append((args, kwargs))
            return self.agent

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.get_project_dir_by_id",
        lambda project_id: (
            "D:/workspace/project-from-id" if project_id == "project-a" else ""
        ),
    )
    manager = RecordingManager()
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock())
    request = AgentRequest(
        request_id="cancel-project-id",
        channel_id="tui",
        session_id="cron-session",
        req_method=ReqMethod.CHAT_CANCEL,
        params={
            "intent": "cancel",
            "mode": "agent",
            "work_mode": "code",
            "project_id": "project-a",
        },
    )

    response = await runtime.cancel_request(request)

    assert response.ok is True
    assert manager.nowait_calls == [
        (
            ("tui",),
            {
                "mode": "code",
                "project_dir": "D:/workspace/project-from-id",
                "sub_mode": "normal",
            },
        )
    ]


@pytest.mark.asyncio
async def test_cleanup_session_does_not_require_runtime_start() -> None:
    manager = FakeAgentManager()
    plan = FakePlanController()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=plan,
    )

    cleaned = await runtime.cleanup_session(
        channel_id="tui",
        session_id="disconnect-during-start",
    )

    assert cleaned is True
    assert runtime.started is False
    assert manager.cleanup_session_calls == [
        ("tui", "disconnect-during-start")
    ]
    assert plan.reset_calls == ["disconnect-during-start"]


@pytest.mark.asyncio
async def test_cleanup_session_can_defer_plan_reset_for_transactional_delete() -> None:
    manager = FakeAgentManager()
    plan = FakePlanController()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=plan,
    )

    cleaned = await runtime.cleanup_session(
        channel_id="web",
        session_id="transactional-delete",
        reset_plan_state=False,
    )

    assert cleaned is True
    assert manager.cleanup_session_calls == [("web", "transactional-delete")]
    assert plan.reset_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cleanup_error",
    [RuntimeError("cleanup failed"), asyncio.CancelledError()],
)
async def test_cleanup_session_preserves_plan_and_original_failure(
    cleanup_error: BaseException,
) -> None:
    manager = FakeAgentManager()
    manager.cleanup_session_error = cleanup_error
    plan = FakePlanController()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=plan,
    )

    with pytest.raises(type(cleanup_error)) as caught:
        await runtime.cleanup_session(
            channel_id="web",
            session_id="cleanup-failure-session",
        )

    assert caught.value is cleanup_error
    assert manager.cleanup_session_calls == [
        ("web", "cleanup-failure-session")
    ]
    assert plan.reset_calls == []


@pytest.mark.asyncio
async def test_answer_interaction_preserves_contract_and_lifecycle_order() -> None:
    order: list[str] = []

    class InteractionAgent(FakeAgent):
        async def process_message(self, request: AgentRequest) -> AgentResponse:
            order.append("process")
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "event_type": "chat.interaction_answered",
                    "interaction_id": "interaction-1",
                },
                metadata={"source": "interaction"},
                agent_ref={"mode": "agent", "id": "interaction-agent"},
            )

    class InteractionManager(FakeAgentManager):
        async def begin_foreground_chat(self) -> None:
            order.append("foreground.begin")
            await super().begin_foreground_chat()

        async def end_foreground_chat(self) -> None:
            order.append("cleanup.foreground")
            await super().end_foreground_chat()

    class InteractionPlanController(FakePlanController):
        async def ensure_state(
            self,
            *args: object,
        ) -> PlanStateResult:
            order.append("plan.ensure")
            return PlanStateResult()

        async def check_post_process_exit(
            self,
            *args: object,
        ) -> list[dict[str, object]]:
            order.append("cleanup.plan")
            return []

    async def initialize() -> None:
        order.append("start")

    manager = InteractionManager()
    manager.agent = InteractionAgent()

    class InteractionRuntime(AgentRuntime):
        async def _prepare_chat_turn(
            self,
            request: AgentRequest,
            channel_id: str,
            *,
            sync_metadata: bool = True,
        ) -> tuple[str, str | None, object]:
            order.append("prepare")
            assert channel_id == "web"
            assert sync_metadata is True
            return "agent", "normal", manager.agent

    runtime = InteractionRuntime(
        agent_manager=manager,
        initializer=initialize,
        plan_controller=InteractionPlanController(),
    )

    async def trigger_hook(_request: AgentRequest) -> None:
        order.append("hook")

    runtime._trigger_before_chat_request_hook = trigger_hook
    request = AgentRequest(
        request_id="answer-request",
        channel_id="web",
        session_id="answer-session",
        req_method=ReqMethod.CHAT_ANSWER,
        params={
            "interaction_id": "interaction-1",
            "answer": "approve",
            "mode": "agent",
            "work_mode": "work",
        },
        metadata={"client": "web"},
    )

    events = await runtime.answer_interaction(request)

    assert order == [
        "start",
        "hook",
        "foreground.begin",
        "prepare",
        "plan.ensure",
        "process",
        "cleanup.plan",
        "cleanup.foreground",
    ]
    assert manager.foreground_calls == ["begin", "end"]
    assert len(events) == 1
    assert events[0].to_dict() == {
        "request_id": "answer-request",
        "channel_id": "web",
        "session_id": "answer-session",
        "payload": {
            "event_type": "chat.interaction_answered",
            "interaction_id": "interaction-1",
        },
        "agent_ref": {"mode": "agent", "id": "interaction-agent"},
        "metadata": {"source": "interaction"},
        "is_complete": True,
        "ok": True,
    }


@pytest.mark.asyncio
async def test_answer_interaction_rejects_non_answer_before_runtime_start() -> None:
    manager = FakeAgentManager()
    manager.agent = SimpleNamespace(process_message=AsyncMock())
    initializer = AsyncMock()
    runtime = AgentRuntime(agent_manager=manager, initializer=initializer)
    request = AgentRequest(
        request_id="not-an-answer",
        channel_id="web",
        session_id="answer-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello", "mode": "agent", "work_mode": "work"},
    )

    with pytest.raises(ValueError) as exc_info:
        await runtime.answer_interaction(request)

    assert str(exc_info.value) == ("interaction answer must use ReqMethod.CHAT_ANSWER")
    assert runtime.started is False
    initializer.assert_not_awaited()
    assert manager.foreground_calls == []
    assert manager.agent_calls == []
    manager.agent.process_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_answer_interaction_forwards_runtime_execution_options() -> None:
    runtime = AgentRuntime(
        agent_manager=FakeAgentManager(),
        initializer=AsyncMock(),
    )
    request = AgentRequest(
        request_id="answer-options",
        channel_id="tui",
        session_id="answer-session",
        req_method=ReqMethod.CHAT_ANSWER,
        params={"answer": "approve"},
    )
    control_handler = AsyncMock()
    expected_events = [
        RuntimeEvent(
            request_id=request.request_id,
            channel_id=request.channel_id,
            session_id=request.session_id,
            payload={"event_type": "chat.final", "content": "done"},
            is_complete=True,
        )
    ]
    runtime.invoke = AsyncMock(return_value=expected_events)

    events = await runtime.answer_interaction(
        request,
        trigger_hook=False,
        on_control_event=control_handler,
    )

    assert events is expected_events
    runtime.invoke.assert_awaited_once_with(
        request,
        trigger_hook=False,
        on_control_event=control_handler,
    )


@pytest.mark.asyncio
async def test_pending_interaction_blocks_heartbeat_until_matching_answer() -> None:
    admission = SessionRunAdmission()

    class InteractionManager(FakeAgentManager):
        def get_agent_for_session_nowait(
            self, channel_id: str, session_id: str
        ) -> object:
            return self.agent

    manager = InteractionManager()
    manager.agent = AskUserAgent()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
        admission_controller=admission,
    )
    session_id = "ask-user-session"
    request = AgentRequest(
        request_id="ask-user-request",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "ask", "mode": "agent", "work_mode": "work"},
        is_stream=True,
    )

    events = [event async for event in runtime.stream(request, trigger_hook=False)]

    assert [event.event_type for event in events] == ["chat.ask_user_question"]
    assert admission.has_pending_interaction(session_id) is True
    assert await admission.try_begin_heartbeat(session_id, "heartbeat-1") is False

    answer = AgentRequest(
        request_id="answer-request",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "",
            "request_id": "call_ask_user",
            "answers": [{"question": "choose", "selected_options": ["A"]}],
            "source": "ask_user_interrupt",
            "mode": "agent",
            "work_mode": "work",
        },
        is_stream=True,
    )
    answer_events = [
        event async for event in runtime.stream(answer, trigger_hook=False)
    ]

    assert [event.event_type for event in answer_events] == ["chat.final"]
    assert admission.has_pending_interaction(session_id) is False
    assert await admission.try_begin_heartbeat(session_id, "heartbeat-2") is True
    await admission.end_heartbeat(session_id, "heartbeat-2")


class ResumingAskUserAgent(AskUserAgent):
    async def process_message(self, request: AgentRequest) -> AgentResponse:
        params = request.params if isinstance(request.params, dict) else {}
        if params.get("answers"):
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": "answered"},
            )
        return await super().process_message(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_stale_ask_user_answer_resumes_via_normal_chat_round(streaming: bool) -> None:
    admission = SessionRunAdmission()

    class InteractionManager(FakeAgentManager):
        def get_agent_for_session_nowait(self, channel_id: str, session_id: str) -> object:
            return self.agent

    session_id = f"ask-user-stale-session-{streaming}"
    manager = InteractionManager()
    manager.agent = ResumingAskUserAgent()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
        admission_controller=admission,
    )
    # Post-restart state: no prior execution is waiting for this answer.
    answer = AgentRequest(
        request_id="stale-answer-request",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "",
            "request_id": "call_ask_user",
            "answers": [{"question": "choose", "selected_options": ["A"]}],
            "source": "ask_user_interrupt",
            "mode": "agent",
            "work_mode": "work",
        },
        is_stream=streaming,
    )

    try:
        if streaming:
            events = [
                event async for event in runtime.stream(answer, trigger_hook=False)
            ]
        else:
            events = await runtime.invoke(answer, trigger_hook=False)
        assert [event.event_type for event in events] == ["chat.final"]
        assert admission.has_pending_interaction(session_id) is False
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_stale_permission_answer_still_requires_active_execution(streaming: bool) -> None:
    class InteractionManager(FakeAgentManager):
        def get_agent_for_session_nowait(self, channel_id: str, session_id: str) -> object:
            return self.agent

    manager = InteractionManager()
    manager.agent = AskUserAgent()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
    )
    answer = AgentRequest(
        request_id="stale-permission-request",
        channel_id="web",
        session_id=f"permission-stale-session-{streaming}",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "",
            "request_id": "call_ask_user",
            "answers": [{"card_id": "opaque-card", "selected_options": ["allow"]}],
            "source": "permission_interrupt",
            "mode": "agent",
            "work_mode": "work",
        },
        is_stream=streaming,
    )

    try:
        with pytest.raises(RuntimeError, match="session has no active execution"):
            if streaming:
                async for _event in runtime.stream(answer, trigger_hook=False):
                    pass
            else:
                await runtime.invoke(answer, trigger_hook=False)
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_stale_ask_user_answer_resumes_on_idle_reregistered_session(streaming: bool) -> None:
    """A re-registered idle session (heartbeat/reconnect after restart) still recovers.

    The post-restart session record exists, so the cold-session probe alone
    would fail; with every execution settled the stale answer must still be
    routed to the chat round.
    """
    admission = SessionRunAdmission()

    class InteractionManager(FakeAgentManager):
        def get_agent_for_session_nowait(self, channel_id: str, session_id: str) -> object:
            return self.agent

    session_id = f"ask-user-reregistered-session-{streaming}"
    manager = InteractionManager()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
        admission_controller=admission,
    )
    # First a plain turn: registers the session and settles.
    manager.agent = FakeAgent()
    first = AgentRequest(
        request_id="reregister-first-turn",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello", "mode": "agent", "work_mode": "work"},
        is_stream=True,
    )
    await _collect_events(runtime.stream(first, trigger_hook=False))

    # Then the stale ask_user answer on the now-idle session.
    manager.agent = ResumingAskUserAgent()
    answer = AgentRequest(
        request_id="reregister-stale-answer",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "",
            "request_id": "call_ask_user",
            "answers": [{"question": "choose", "selected_options": ["A"]}],
            "source": "ask_user_interrupt",
            "mode": "agent",
            "work_mode": "work",
        },
        is_stream=streaming,
    )

    try:
        if streaming:
            events = [event async for event in runtime.stream(answer, trigger_hook=False)]
        else:
            events = await runtime.invoke(answer, trigger_hook=False)
        assert [event.event_type for event in events] == ["chat.final"]
        assert admission.has_pending_interaction(session_id) is False
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_wrong_ask_user_answer_rejected_while_interaction_waits() -> None:
    """A wrong-id answer must not reach the agent while another interaction waits."""
    admission = SessionRunAdmission()

    class InteractionManager(FakeAgentManager):
        def get_agent_for_session_nowait(self, channel_id: str, session_id: str) -> object:
            return self.agent

    session_id = "ask-user-wrong-id-session"
    manager = InteractionManager()
    manager.agent = AskUserAgent()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
        admission_controller=admission,
    )
    original = AgentRequest(
        request_id="wrong-id-original",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "ask", "mode": "agent", "work_mode": "work"},
        is_stream=True,
    )
    events = [event async for event in runtime.stream(original, trigger_hook=False)]
    assert [event.event_type for event in events] == ["chat.ask_user_question"]
    assert admission.has_pending_interaction(session_id) is True

    wrong = AgentRequest(
        request_id="wrong-id-answer",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "",
            "request_id": "some-other-question",
            "answers": [{"question": "choose", "selected_options": ["A"]}],
            "source": "ask_user_interrupt",
            "mode": "agent",
            "work_mode": "work",
        },
        is_stream=True,
    )

    try:
        with pytest.raises(RuntimeError, match="session has no active execution"):
            async for _event in runtime.stream(wrong, trigger_hook=False):
                pass
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("action", ["set", "resume"])
async def test_goal_start_waits_for_active_session_message(streaming, action) -> None:
    admission = SessionRunAdmission()
    manager = FakeAgentManager()
    entered = asyncio.Event()

    class GoalAgent(FakeAgent):
        async def process_message(self, request):
            entered.set()
            return await super().process_message(request)

        async def process_message_stream(self, request):
            entered.set()
            async for chunk in super().process_message_stream(request):
                yield chunk

    manager.agent = GoalAgent()
    runtime = AgentRuntime(
        agent_manager=manager, initializer=AsyncMock(),
        plan_controller=FakePlanController(), admission_controller=admission,
    )
    sid = f"goal-mailbox-{action}-{streaming}"
    await admission.begin_session_message(sid, "mailbox-run")
    request = AgentRequest(
        request_id="goal", session_id=sid, channel_id="web",
        req_method=ReqMethod.COMMAND_GOAL, is_stream=streaming,
        params={"mode": "agent", "action": action, "objective": "finish"},
    )
    task = asyncio.create_task(
        _collect_events(runtime.stream(request, trigger_hook=False))
        if streaming else runtime.invoke(request, trigger_hook=False)
    )
    try:
        # Wait until the runtime has either queued admission or called the agent.
        async with asyncio.timeout(1):
            while not admission.is_user_active(sid) and not entered.is_set():
                await asyncio.sleep(0)
        assert not entered.is_set()
        await admission.end_session_message(sid, "mailbox-run")
        await asyncio.wait_for(task, timeout=1)
        assert entered.is_set()
        assert not admission.is_user_active(sid)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await admission.end_session_message(sid, "mailbox-run")
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["pause", "cancel"])
async def test_goal_stop_releases_waiting_session_message(stop) -> None:
    admission = SessionRunAdmission()
    entered, release = asyncio.Event(), asyncio.Event()

    class GoalAgent(FakeAgent):
        async def process_message_stream(self, request):
            entered.set()
            await release.wait()
            async for chunk in super().process_message_stream(request):
                yield chunk

        async def process_message(self, request):
            assert request.params["action"] == "pause"
            release.set()
            return await super().process_message(request)

    manager = FakeAgentManager()
    manager.agent = GoalAgent()
    runtime = AgentRuntime(
        agent_manager=manager, initializer=AsyncMock(),
        plan_controller=FakePlanController(), admission_controller=admission,
    )
    sid = f"goal-stop-mailbox-{stop}"
    request = AgentRequest(
        request_id="goal", session_id=sid, channel_id="web",
        req_method=ReqMethod.COMMAND_GOAL, is_stream=True,
        params={"mode": "agent", "action": "set", "objective": "finish"},
    )
    task = asyncio.create_task(_collect_events(runtime.stream(request, trigger_hook=False)))
    message = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        message = asyncio.create_task(admission.begin_session_message(sid, "run-1"))
        await asyncio.sleep(0)
        assert not message.done()
        if stop == "cancel":
            task.cancel()
        else:
            pause = AgentRequest(
                request_id="pause", session_id=sid, channel_id="web",
                req_method=ReqMethod.COMMAND_GOAL,
                params={"mode": "agent", "action": "pause"},
            )
            await asyncio.wait_for(runtime.invoke(pause, trigger_hook=False), 1)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(message, 1)
        assert not admission.is_user_active(sid)
    finally:
        task.cancel()
        if message is not None:
            message.cancel()
            await asyncio.gather(message, return_exceptions=True)
        await asyncio.gather(task, return_exceptions=True)
        await admission.end_session_message(sid, "run-1")
        await runtime.close()


@pytest.mark.asyncio
async def test_session_message_waits_for_goal_after_output_stream_ends() -> None:
    admission = SessionRunAdmission()

    class GoalAgent(FakeAgent):
        goal_active = True

        def has_active_goal(self, session_id):
            return self.goal_active

    class GoalManager(FakeAgentManager):
        def has_active_goal(self, channel_id, session_id):
            return self.agent.has_active_goal(session_id)

    manager = GoalManager()
    manager.agent = GoalAgent()
    runtime = AgentRuntime(
        agent_manager=manager, initializer=AsyncMock(),
        plan_controller=FakePlanController(), admission_controller=admission,
    )
    sid = "goal-background-mailbox"
    request = AgentRequest(
        request_id="goal", session_id=sid, channel_id="web",
        req_method=ReqMethod.COMMAND_GOAL, is_stream=True,
        params={"mode": "agent", "action": "set", "objective": "finish"},
    )
    await _collect_events(runtime.stream(request, trigger_hook=False))
    assert not admission.is_user_active(sid)
    message = asyncio.create_task(admission.begin_session_message(sid, "run-1"))
    try:
        await asyncio.sleep(0)
        assert not message.done()
        # A different session remains usable while this Goal runs.
        await asyncio.wait_for(admission.begin_session_message("other", "run-2"), 1)
        await admission.end_session_message("other", "run-2")
        # No foreground event is required to wake the waiting mailbox.
        manager.agent.goal_active = False
        await asyncio.wait_for(message, timeout=1)
    finally:
        message.cancel()
        await asyncio.gather(message, return_exceptions=True)
        await admission.end_session_message(sid, "run-1")
        await runtime.close()


@pytest.mark.asyncio
async def test_stale_answer_does_not_clear_current_pending_interaction() -> None:
    admission = SessionRunAdmission()
    await admission.mark_interaction_pending("session-1", "current-question")

    await admission.begin_user("session-1")
    await admission.clear_interaction_pending("session-1", "stale-question")
    await admission.end_user("session-1")

    assert admission.has_pending_interaction("session-1") is True
    assert await admission.try_begin_heartbeat("session-1", "heartbeat-1") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_method", "request_params", "expected_work_kind"),
    [
        (
            ReqMethod.CHAT_SEND,
            {"query": "choose", "mode": "agent", "work_mode": "work"},
            "chat_stream",
        ),
        (
            ReqMethod.COMMAND_GOAL,
            {
                "action": "set",
                "objective": "finish the task",
                "mode": "agent",
                "work_mode": "work",
            },
            "goal_stream",
        ),
    ],
)
async def test_interrupt_answer_resumes_session_execution_after_stream_ends(
    request_method: ReqMethod,
    request_params: dict[str, object],
    expected_work_kind: str,
) -> None:
    class InteractionAgent(FakeAgent):
        async def process_message_stream(self, request: AgentRequest):
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "chat.ask_user_question",
                    "request_id": "call_ask_user",
                    "source": "ask_user_interrupt",
                    "questions": [],
                },
            )

        async def deliver_control_input(self, request: AgentRequest):
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": "continued"},
                is_complete=True,
            )

    class InteractionManager(FakeAgentManager):
        def get_agent_for_session_nowait(
            self, channel_id: str, session_id: str
        ) -> object:
            return self.agent

    manager = InteractionManager()
    manager.agent = InteractionAgent()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
    )
    runtime._trigger_before_chat_request_hook = AsyncMock()
    # Keep parameter cases separate from asynchronous metadata writes.
    session_id = f"ask-user-stream-session-{expected_work_kind}"
    original = AgentRequest(
        request_id="original",
        channel_id="web",
        session_id=session_id,
        req_method=request_method,
        is_stream=True,
        params=request_params,
    )
    original_events = await _collect_events(runtime.stream(original, trigger_hook=False))
    assert [event.event_type for event in original_events] == [
        "chat.ask_user_question"
    ]
    waiting_snapshot = runtime._session_coordinator.snapshot_session(session_id)
    assert waiting_snapshot is not None
    parent = next(
        item
        for item in waiting_snapshot.executions
        if item.work_kind.value == expected_work_kind
    )
    assert parent.state.value == "waiting_for_control"

    answer = AgentRequest(
        request_id="answer-dispatch",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        params={
            "query": "",
            "request_id": "call_ask_user",
            "answers": [{"question": "选择方案", "selected_options": ["A"]}],
            "source": "ask_user_interrupt",
            "mode": "agent",
            "work_mode": "work",
        },
    )
    control_events = await asyncio.wait_for(
        _collect_events(runtime.stream(answer, trigger_hook=False)),
        timeout=0.2,
    )
    assert [event.event_type for event in control_events] == ["chat.final"]
    assert control_events[0].payload["content"] == "continued"
    snapshot = runtime._session_coordinator.snapshot_session(session_id)
    assert snapshot is not None
    control = next(
        item
        for item in snapshot.executions
        if item.work_kind.value == "control_input"
    )
    assert control.parent_execution_id == parent.execution_id
    completed_parent = next(
        item for item in snapshot.executions if item.execution_id == parent.execution_id
    )
    assert completed_parent.state.value == "succeeded"
    assert control.state.value == "succeeded"


@pytest.mark.asyncio
async def test_stream_uses_selected_existing_agent_and_runtime_events() -> None:
    manager = FakeAgentManager()
    plan = FakePlanController()

    async def initialize() -> None:
        return None

    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=initialize,
        plan_controller=plan,
    )

    async def no_hook(request: AgentRequest) -> None:
        return None

    runtime._trigger_before_chat_request_hook = no_hook
    request = AgentRequest(
        request_id="stream-request",
        channel_id="process_cli",
        session_id=None,
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello", "mode": "agent", "work_mode": "work"},
    )

    events = [event async for event in runtime.stream(request)]

    assert [event.event_type for event in events] == ["chat.delta", "chat.final"]
    assert events[-1].is_complete is True
    assert manager.foreground_calls == ["begin", "end"]


@pytest.mark.asyncio
async def test_stream_preserves_none_terminal_sentinel() -> None:
    manager = FakeAgentManager()
    manager.agent = TerminalSentinelAgent()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
    )
    runtime._trigger_before_chat_request_hook = AsyncMock()
    request = AgentRequest(
        request_id="terminal-sentinel-request",
        channel_id="web",
        session_id="terminal-sentinel-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello", "mode": "team", "work_mode": "work"},
    )

    events = [event async for event in runtime.stream(request)]

    assert len(events) == 1
    assert events[0].payload is None
    assert events[0].event_type == ""
    assert events[0].is_complete is True
    assert events[0].metadata == {"route": "stream"}
    assert events[0].to_dict()["payload"] is None


@pytest.mark.asyncio
async def test_stream_preserves_cancellation_and_checks_plan_exit() -> None:
    manager = FakeAgentManager()
    manager.agent = CancellingAgent()
    plan = FakePlanController()
    plan.check_post_process_exit = AsyncMock(return_value=[])

    async def initialize() -> None:
        return None

    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=initialize,
        plan_controller=plan,
    )

    async def no_hook(request: AgentRequest) -> None:
        return None

    runtime._trigger_before_chat_request_hook = no_hook
    request = AgentRequest(
        request_id="cancelled-stream-request",
        channel_id="process_cli",
        session_id=None,
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello", "mode": "agent", "work_mode": "work"},
    )

    with pytest.raises(asyncio.CancelledError):
        async for _event in runtime.stream(request):
            pass

    assert manager.foreground_calls == ["begin", "end"]
    plan.check_post_process_exit.assert_awaited_once()


@pytest.mark.asyncio
async def test_closed_runtime_cannot_restart() -> None:
    manager = FakeAgentManager()

    async def initialize() -> None:
        return None

    runtime = AgentRuntime(agent_manager=manager, initializer=initialize)
    await runtime.close()

    with pytest.raises(RuntimeStateError, match="already closed"):
        await runtime.start()


@pytest.mark.asyncio
async def test_close_still_cleans_up_when_cancel_fails() -> None:
    manager = FakeAgentManager()
    manager.cancel_error = RuntimeError("cancel failed")
    runtime = AgentRuntime(agent_manager=manager)

    with pytest.raises(RuntimeError, match="cancel failed"):
        await runtime.close()

    assert manager.cleanup_calls == 1
    assert runtime.closed is True


def test_agent_server_owns_the_same_runtime_manager() -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    server = AgentWebSocketServer()

    assert server.get_runtime().agent_manager is server.get_agent_manager()


@pytest.mark.asyncio
async def test_agent_server_cancel_delegates_to_runtime_public_api() -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    manager = object()
    expected = AgentResponse(
        request_id="cancel-request",
        channel_id="tui",
        payload={"event_type": "chat.interrupt_result", "success": True},
    )
    runtime = SimpleNamespace(
        agent_manager=manager,
        cancel_request=AsyncMock(return_value=expected),
    )
    server = object.__new__(AgentWebSocketServer)
    server._runtime = runtime
    server._agent_manager = manager
    request = AgentRequest(
        request_id="cancel-request",
        channel_id="tui",
        session_id="session-1",
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": "cancel"},
    )

    response = await server._handle_cancel(
        None,
        request,
        asyncio.Lock(),
        send_response=False,
    )

    assert response is expected
    runtime.cancel_request.assert_awaited_once_with(request, allow_create=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cleanup_result", "expected_cleaned"),
    [(True, True), (RuntimeError("cleanup failed"), False)],
)
async def test_agent_server_disconnect_cleanup_delegates_to_runtime_public_api(
    cleanup_result: bool | RuntimeError,
    expected_cleaned: bool,
) -> None:
    from jiuwenswarm.server import agent_ws_server as agent_ws_server_module

    AgentWebSocketServer = agent_ws_server_module.AgentWebSocketServer

    manager = object()
    cleanup_session = (
        AsyncMock(side_effect=cleanup_result)
        if isinstance(cleanup_result, RuntimeError)
        else AsyncMock(return_value=cleanup_result)
    )
    runtime = SimpleNamespace(
        agent_manager=manager,
        cleanup_session=cleanup_session,
    )
    server = object.__new__(AgentWebSocketServer)
    server._runtime = runtime
    server._agent_manager = manager
    request = AgentRequest(
        request_id="disconnect-cleanup",
        channel_id="tui",
        session_id="session-1",
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": "cancel"},
    )
    agent_ws_server_module._plan_active_sessions.add("session-1")
    agent_ws_server_module._plan_exited_sessions.add("session-1")

    try:
        cleaned = await server._cleanup_client_disconnect_session_runtime(request)

        assert cleaned is expected_cleaned
        assert "session-1" not in agent_ws_server_module._plan_active_sessions
        assert "session-1" not in agent_ws_server_module._plan_exited_sessions
        runtime.cleanup_session.assert_awaited_once_with(
            channel_id="tui",
            session_id="session-1",
        )
    finally:
        agent_ws_server_module._plan_active_sessions.discard("session-1")
        agent_ws_server_module._plan_exited_sessions.discard("session-1")


@pytest.mark.asyncio
async def test_agent_server_auto_fork_uses_runtime_public_api() -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    manager = object()
    prepared = SimpleNamespace(
        state=SessionProvisionState.PREPARED,
    )
    result = SessionForkResult(
        channel_id="tui",
        source_session_id="fork-source",
        session_id="fork-target",
        title="Forked session",
    )
    async def commit_fork(
        _prepared: object,
        *,
        timing: object,
    ) -> object:
        del timing
        prepared.state = SessionProvisionState.COMMITTED
        return result

    runtime = SimpleNamespace(
        agent_manager=manager,
        start=AsyncMock(),
        prepare_session_fork=AsyncMock(return_value=prepared),
        commit_session_provision=AsyncMock(side_effect=commit_fork),
        abort_session_provision=AsyncMock(),
    )

    server = object.__new__(AgentWebSocketServer)
    server._runtime = runtime
    server._agent_manager = manager
    ws = SimpleNamespace(send=AsyncMock())
    request = AgentRequest(
        request_id="fork-request",
        channel_id="tui",
        session_id="fork-source",
        req_method=ReqMethod.SESSION_FORK,
        params={"source_session_id": "fork-source", "title": "Forked session"},
    )

    await server._handle_session_fork(ws, request, asyncio.Lock())

    runtime.start.assert_awaited_once_with()
    runtime.prepare_session_fork.assert_awaited_once_with(
        SessionForkInput(
            channel_id="tui",
            source_session_id="fork-source",
            target_session_id=None,
            title="Forked session",
        )
    )
    runtime.commit_session_provision.assert_awaited_once_with(
        prepared,
        timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
    )
    ws.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_server_explicit_fork_waits_for_runtime() -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    order: list[str] = []
    manager = object()
    prepared = SimpleNamespace(
        state=SessionProvisionState.PREPARED,
    )

    async def start_runtime() -> None:
        order.append("runtime.start")

    async def prepare_fork(_provision_input: object) -> object:
        order.append("runtime.prepare")
        return prepared

    async def commit_fork(
        _prepared: object,
        *,
        timing: object,
    ) -> object:
        del timing
        order.append("runtime.commit")
        prepared.state = SessionProvisionState.COMMITTED
        return SessionForkResult(
            channel_id="tui",
            source_session_id="fork-source",
            session_id="fork-target",
            title="Forked session",
        )

    runtime = SimpleNamespace(
        agent_manager=manager,
        start=AsyncMock(side_effect=start_runtime),
        prepare_session_fork=AsyncMock(side_effect=prepare_fork),
        commit_session_provision=AsyncMock(side_effect=commit_fork),
        abort_session_provision=AsyncMock(),
    )

    server = object.__new__(AgentWebSocketServer)
    server._runtime = runtime
    server._agent_manager = manager
    ws = SimpleNamespace(send=AsyncMock())
    request = AgentRequest(
        request_id="fork-request",
        channel_id="tui",
        session_id="fork-source",
        req_method=ReqMethod.SESSION_FORK,
        params={
            "source_session_id": "fork-source",
            "target_session_id": "fork-target",
            "title": "Forked session",
        },
    )

    await server._handle_session_fork(ws, request, asyncio.Lock())

    runtime.start.assert_awaited_once_with()
    runtime.prepare_session_fork.assert_awaited_once_with(
        SessionForkInput(
            channel_id="tui",
            source_session_id="fork-source",
            target_session_id="fork-target",
            title="Forked session",
        )
    )
    assert order == ["runtime.start", "runtime.prepare", "runtime.commit"]
    ws.send.assert_awaited_once()


def test_runtime_error_event_metadata_is_optional_and_copied() -> None:
    metadata = {"trace_id": "runtime-error"}

    event = RuntimeEvent.error(
        request_id="error-with-metadata",
        channel_id="process_cli",
        session_id="session-1",
        error=ValueError("boom"),
        metadata=metadata,
    )
    event_without_metadata = RuntimeEvent.error(
        request_id="error-without-metadata",
        channel_id="process_cli",
        session_id=None,
        error=ValueError("boom"),
    )
    metadata["trace_id"] = "mutated"

    assert event.metadata == {"trace_id": "runtime-error"}
    assert event_without_metadata.metadata is None


@pytest.mark.asyncio
async def test_runtime_events_preserve_unary_and_stream_metadata() -> None:
    manager = FakeAgentManager()
    plan = FakePlanController()

    async def initialize() -> None:
        return None

    async def no_hook(request: AgentRequest) -> None:
        return None

    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=initialize,
        plan_controller=plan,
    )
    runtime._trigger_before_chat_request_hook = no_hook
    request = AgentRequest(
        request_id="metadata-request",
        channel_id="process_cli",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello", "mode": "agent", "work_mode": "work"},
    )

    unary = await runtime.invoke(request)
    streamed = [event async for event in runtime.stream(request)]

    assert unary[-1].metadata == {"route": "unary"}
    assert [event.metadata for event in streamed] == [
        {"route": "stream"},
        {"route": "stream"},
    ]


@pytest.mark.asyncio
async def test_unary_control_event_is_delivered_before_agent_execution() -> None:
    order: list[str] = []

    class OrderedAgent(FakeAgent):
        async def process_message(self, request: AgentRequest) -> AgentResponse:
            order.append("agent")
            return await super().process_message(request)

    class OrderedPlanController(FakePlanController):
        async def ensure_state(self, *args: object) -> PlanStateResult:
            order.append("plan")
            return PlanStateResult(
                events=[{"event_type": "plan.mode_exited", "mode": "code.normal"}]
            )

    async def initialize() -> None:
        return None

    async def no_hook(request: AgentRequest) -> None:
        return None

    manager = FakeAgentManager()
    manager.agent = OrderedAgent()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=initialize,
        plan_controller=OrderedPlanController(),
    )
    runtime._trigger_before_chat_request_hook = no_hook

    async def control_handler(event) -> None:
        order.append(f"control:{event.event_type}")

    events = await runtime.invoke(
        _chat_request("ordered-control"),
        on_control_event=control_handler,
    )

    assert order[:3] == ["plan", "control:plan.mode_exited", "agent"]
    assert [event.event_type for event in events] == ["chat.final"]


@pytest.mark.asyncio
async def test_cancel_does_not_wait_for_first_runtime_start() -> None:
    manager = FakeAgentManager()
    initialize_started = asyncio.Event()
    allow_initialize = asyncio.Event()

    async def initialize() -> None:
        initialize_started.set()
        await allow_initialize.wait()

    runtime = AgentRuntime(agent_manager=manager, initializer=initialize)
    start_task = asyncio.create_task(runtime.start())
    await initialize_started.wait()
    request = AgentRequest(
        request_id="cancel-during-start",
        channel_id="tui",
        session_id="session-1",
        req_method=ReqMethod.CHAT_CANCEL,
        params={},
    )

    response = await asyncio.wait_for(runtime.cancel_request(request), timeout=0.1)

    assert response.ok is True
    assert response.payload["event_type"] == "chat.interrupt_result"
    allow_initialize.set()
    await start_task


@pytest.mark.asyncio
async def test_cancel_with_agent_creation_waits_for_runtime_start() -> None:
    manager = FakeAgentManager()
    initialize_started = asyncio.Event()
    allow_initialize = asyncio.Event()

    async def initialize() -> None:
        initialize_started.set()
        await allow_initialize.wait()

    runtime = AgentRuntime(agent_manager=manager, initializer=initialize)
    request = AgentRequest(
        request_id="cancel-create-during-start",
        channel_id="tui",
        session_id="session-1",
        req_method=ReqMethod.CHAT_CANCEL,
        params={"mode": "agent"},
    )
    cancel_task = asyncio.create_task(
        runtime.cancel_request(request, allow_create=True)
    )
    await initialize_started.wait()
    await asyncio.sleep(0)

    assert manager.agent_calls == []
    assert cancel_task.done() is False

    allow_initialize.set()
    response = await cancel_task

    assert response.ok is True
    assert len(manager.agent_calls) == 1


class _FailingPlanController(FakePlanController):
    async def check_post_process_exit(self, *args: object) -> list[dict[str, object]]:
        raise RuntimeError("plan post failed")


class _FailingAgent(FakeAgent):
    async def process_message(self, request: AgentRequest) -> AgentResponse:
        raise ValueError("agent execution failed")

    async def process_message_stream(self, request: AgentRequest):
        if False:
            yield
        raise ValueError("agent stream failed")


def _chat_request(request_id: str) -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        channel_id="process_cli",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello", "mode": "agent", "work_mode": "work"},
    )


@pytest.mark.asyncio
async def test_plan_post_error_always_ends_foreground_chat() -> None:
    manager = FakeAgentManager()

    async def initialize() -> None:
        return None

    async def no_hook(request: AgentRequest) -> None:
        return None

    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=initialize,
        plan_controller=_FailingPlanController(),
    )
    runtime._trigger_before_chat_request_hook = no_hook

    with pytest.raises(RuntimeError, match="plan post failed"):
        await runtime.invoke(_chat_request("plan-post-unary"))
    assert manager.foreground_calls == ["begin", "end"]

    manager.foreground_calls.clear()
    with pytest.raises(RuntimeError, match="plan post failed"):
        async for _event in runtime.stream(_chat_request("plan-post-stream")):
            pass
    assert manager.foreground_calls == ["begin", "end"]


@pytest.mark.asyncio
async def test_plan_post_error_does_not_replace_agent_error() -> None:
    manager = FakeAgentManager()
    manager.agent = _FailingAgent()

    async def initialize() -> None:
        return None

    async def no_hook(request: AgentRequest) -> None:
        return None

    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=initialize,
        plan_controller=_FailingPlanController(),
    )
    runtime._trigger_before_chat_request_hook = no_hook

    unary_request = _chat_request("primary-unary-error")
    unary_request.metadata = {"trace_id": "unary-error"}
    stream_request = _chat_request("primary-stream-error")
    stream_request.metadata = {"trace_id": "stream-error"}

    unary = await runtime.invoke(unary_request)
    streamed = [
        event async for event in runtime.stream(stream_request)
    ]

    assert unary[-1].event_type == "runtime.error"
    assert unary[-1].ok is False
    assert unary[-1].payload["error"] == "agent execution failed"
    assert unary[-1].metadata == {"trace_id": "unary-error"}
    assert streamed[-1].event_type == "runtime.error"
    assert streamed[-1].ok is False
    assert streamed[-1].is_complete is True
    assert streamed[-1].payload["error"] == "agent stream failed"
    assert streamed[-1].metadata == {"trace_id": "stream-error"}
    assert manager.foreground_calls == ["begin", "end", "begin", "end"]


@pytest.mark.asyncio
async def test_runtime_context_is_inherited_by_child_tasks_and_isolated() -> None:
    observed: list[tuple[object, object, object, object]] = []

    class ContextAgent(FakeAgent):
        def __init__(self, runtime_getter, manager_getter) -> None:
            self._runtime_getter = runtime_getter
            self._manager_getter = manager_getter

        async def process_message(self, request: AgentRequest) -> AgentResponse:
            direct = (self._runtime_getter(), self._manager_getter())

            async def child_context() -> tuple[object, object]:
                await asyncio.sleep(0)
                return self._runtime_getter(), self._manager_getter()

            child = await asyncio.create_task(child_context())
            observed.append((*direct, *child))
            return await super().process_message(request)

    async def initialize() -> None:
        return None

    async def no_hook(request: AgentRequest) -> None:
        return None

    runtimes: list[AgentRuntime] = []
    managers: list[FakeAgentManager] = []
    for index in range(2):
        manager = FakeAgentManager()
        runtime = AgentRuntime(
            agent_manager=manager,
            initializer=initialize,
            plan_controller=FakePlanController(),
        )
        manager.agent = ContextAgent(
            get_current_runtime,
            get_current_agent_manager,
        )
        runtime._trigger_before_chat_request_hook = no_hook
        managers.append(manager)
        runtimes.append(runtime)

    await asyncio.gather(
        runtimes[0].invoke(_chat_request("context-a")),
        runtimes[1].invoke(_chat_request("context-b")),
    )

    assert set(observed) == {
        (runtimes[0], managers[0], runtimes[0], managers[0]),
        (runtimes[1], managers[1], runtimes[1], managers[1]),
    }
    assert get_runtime_context() is None


@pytest.mark.asyncio
async def test_runtime_context_is_available_during_early_stream_close() -> None:
    manager = FakeAgentManager()
    observed: list[tuple[object, object]] = []
    agent_stream_closed = False

    class ClosingAgent(FakeAgent):
        async def process_message_stream(self, request: AgentRequest):
            nonlocal agent_stream_closed
            try:
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={"event_type": "chat.delta", "delta": "ok"},
                )
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={"event_type": "chat.final", "content": "ok"},
                    is_complete=True,
                )
            finally:
                agent_stream_closed = True

    class ContextPlanController(FakePlanController):
        async def check_post_process_exit(
            self,
            *args: object,
        ) -> list[dict[str, object]]:
            observed.append(
                (
                    get_current_runtime(),
                    get_current_agent_manager(),
                )
            )
            return [{"event_type": "plan.mode_exited", "mode": "agent"}]

    async def initialize() -> None:
        return None

    async def no_hook(request: AgentRequest) -> None:
        return None

    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=initialize,
        plan_controller=ContextPlanController(),
    )
    manager.agent = ClosingAgent()
    runtime._trigger_before_chat_request_hook = no_hook
    stream = runtime.stream(_chat_request("context-early-close"))

    await anext(stream)
    await stream.aclose()

    assert observed == [(runtime, manager)]
    assert agent_stream_closed is True
    assert manager.foreground_calls == ["begin", "end"]
    assert get_runtime_context() is None


@pytest.mark.asyncio
async def test_early_stream_close_delivers_post_event_to_callback() -> None:
    manager = FakeAgentManager()
    delivered: list[str] = []

    class ExitPlanController(FakePlanController):
        async def check_post_process_exit(
            self,
            *args: object,
        ) -> list[dict[str, object]]:
            return [{"event_type": "plan.mode_exited", "mode": "agent"}]

    async def initialize() -> None:
        return None

    async def no_hook(request: AgentRequest) -> None:
        return None

    async def control_handler(event) -> None:
        delivered.append(event.event_type)

    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=initialize,
        plan_controller=ExitPlanController(),
    )
    runtime._trigger_before_chat_request_hook = no_hook
    stream = runtime.stream(
        _chat_request("callback-early-close"),
        on_control_event=control_handler,
    )

    await anext(stream)
    await stream.aclose()

    assert delivered == ["plan.mode_exited"]
    assert manager.foreground_calls == ["begin", "end"]


@pytest.mark.asyncio
async def test_runtime_owned_extensions_are_reference_counted(monkeypatch) -> None:
    from openjiuwen.core.runner import Runner

    from jiuwenswarm.extensions.manager import ExtensionManager
    from jiuwenswarm.extensions.registry import ExtensionRegistry
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    ExtensionRegistry.reset_instance()
    load_extensions = AsyncMock()
    shutdown_extensions = AsyncMock()
    monkeypatch.setattr(
        runtime_service_module,
        "_initialize_runtime_dependencies",
        AsyncMock(),
    )
    monkeypatch.setattr(Runner, "start", AsyncMock(return_value=True))
    monkeypatch.setattr(Runner, "stop", AsyncMock(return_value=True))
    monkeypatch.setattr(
        interface_deep,
        "close_persistent_checkpointer",
        AsyncMock(),
    )
    monkeypatch.setattr(ExtensionManager, "load_all_extensions", load_extensions)
    monkeypatch.setattr(
        ExtensionManager,
        "shutdown_all_extensions",
        shutdown_extensions,
    )
    monkeypatch.setattr(runtime_service_module, "_PROCESS_RUNTIME_DEPENDENCY_USERS", 0)
    monkeypatch.setattr(runtime_service_module, "_PROCESS_RUNTIME_EXTENSION_USERS", 0)
    monkeypatch.setattr(
        runtime_service_module,
        "_PROCESS_RUNTIME_EXTENSION_MANAGER",
        None,
    )
    monkeypatch.setattr(
        runtime_service_module,
        "_PROCESS_RUNTIME_EXTENSION_REGISTRY",
        None,
    )

    first = AgentRuntime()
    second = AgentRuntime()
    await first.start()
    registry = ExtensionRegistry.get_instance()
    await second.start()

    load_extensions.assert_awaited_once_with(include_transport_extensions=False)
    assert ExtensionRegistry.get_instance() is registry
    await first.close()
    shutdown_extensions.assert_not_awaited()
    assert ExtensionRegistry.get_instance() is registry

    await second.close()
    shutdown_extensions.assert_awaited_once()
    with pytest.raises(RuntimeError, match="ExtensionRegistry"):
        ExtensionRegistry.get_instance()
    assert runtime_service_module._PROCESS_RUNTIME_EXTENSION_USERS == 0


@pytest.mark.asyncio
async def test_runtime_does_not_close_externally_owned_extensions(monkeypatch) -> None:
    from openjiuwen.core.runner import Runner

    from jiuwenswarm.extensions.manager import ExtensionManager
    from jiuwenswarm.extensions.registry import ExtensionRegistry
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    ExtensionRegistry.reset_instance()
    external_registry = ExtensionRegistry.create_instance(
        callback_framework=Runner.callback_framework,
        config={},
        logger=None,
    )
    shutdown_extensions = AsyncMock()
    monkeypatch.setattr(
        runtime_service_module,
        "_initialize_runtime_dependencies",
        AsyncMock(),
    )
    monkeypatch.setattr(Runner, "start", AsyncMock(return_value=True))
    monkeypatch.setattr(Runner, "stop", AsyncMock(return_value=True))
    monkeypatch.setattr(
        interface_deep,
        "close_persistent_checkpointer",
        AsyncMock(),
    )
    monkeypatch.setattr(
        ExtensionManager,
        "shutdown_all_extensions",
        shutdown_extensions,
    )
    monkeypatch.setattr(runtime_service_module, "_PROCESS_RUNTIME_DEPENDENCY_USERS", 0)
    monkeypatch.setattr(runtime_service_module, "_PROCESS_RUNTIME_EXTENSION_USERS", 0)
    monkeypatch.setattr(
        runtime_service_module,
        "_PROCESS_RUNTIME_EXTENSION_MANAGER",
        None,
    )
    monkeypatch.setattr(
        runtime_service_module,
        "_PROCESS_RUNTIME_EXTENSION_REGISTRY",
        None,
    )

    runtime = AgentRuntime()
    await runtime.start()
    await runtime.close()

    shutdown_extensions.assert_not_awaited()
    assert ExtensionRegistry.get_instance() is external_registry
    ExtensionRegistry.reset_instance()


@pytest.mark.asyncio
async def test_process_dependencies_are_reference_counted(monkeypatch) -> None:
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    initialize = AsyncMock()
    runner_start = AsyncMock(return_value=True)
    runner_stop = AsyncMock(return_value=True)
    close_checkpointer = AsyncMock()
    ensure_extensions = AsyncMock()
    monkeypatch.setattr(
        runtime_service_module,
        "_initialize_runtime_dependencies",
        initialize,
    )
    monkeypatch.setattr(Runner, "start", runner_start)
    monkeypatch.setattr(Runner, "stop", runner_stop)
    monkeypatch.setattr(
        interface_deep,
        "close_persistent_checkpointer",
        close_checkpointer,
    )
    monkeypatch.setattr(AgentRuntime, "_ensure_extensions", ensure_extensions)
    monkeypatch.setattr(runtime_service_module, "_PROCESS_RUNTIME_DEPENDENCY_USERS", 0)

    first = AgentRuntime()
    second = AgentRuntime()
    await first.start()
    await second.start()

    initialize.assert_awaited_once()
    runner_start.assert_awaited_once()
    await first.close()
    runner_stop.assert_not_awaited()
    close_checkpointer.assert_not_awaited()

    await second.close()
    runner_stop.assert_awaited_once()
    close_checkpointer.assert_awaited_once()
    assert runtime_service_module._PROCESS_RUNTIME_DEPENDENCY_USERS == 0


@pytest.mark.asyncio
async def test_process_dependency_lease_rolls_back_on_start_failure(
    monkeypatch,
) -> None:
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    monkeypatch.setattr(
        runtime_service_module,
        "_initialize_runtime_dependencies",
        AsyncMock(),
    )
    runner_stop = AsyncMock(return_value=True)
    close_checkpointer = AsyncMock()
    monkeypatch.setattr(Runner, "start", AsyncMock(return_value=True))
    monkeypatch.setattr(Runner, "stop", runner_stop)
    monkeypatch.setattr(
        interface_deep,
        "close_persistent_checkpointer",
        close_checkpointer,
    )
    monkeypatch.setattr(runtime_service_module, "_PROCESS_RUNTIME_DEPENDENCY_USERS", 0)

    runtime = AgentRuntime()
    runtime._ensure_extensions = AsyncMock(side_effect=RuntimeError("extension failed"))

    with pytest.raises(RuntimeError, match="extension failed"):
        await runtime.start()

    runner_stop.assert_awaited_once()
    close_checkpointer.assert_awaited_once()
    assert runtime_service_module._PROCESS_RUNTIME_DEPENDENCY_USERS == 0


@pytest.mark.asyncio
async def test_checkpointer_init_error_survives_close_error_and_allows_retry(
    monkeypatch,
) -> None:
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    initialize = AsyncMock(
        side_effect=[ValueError("checkpointer init failed"), None],
    )
    runner_start = AsyncMock(return_value=True)
    runner_stop = AsyncMock(return_value=True)
    close_checkpointer = AsyncMock(
        side_effect=[RuntimeError("checkpointer rollback failed"), None],
    )
    monkeypatch.setattr(
        runtime_service_module,
        "_initialize_runtime_dependencies",
        initialize,
    )
    monkeypatch.setattr(Runner, "start", runner_start)
    monkeypatch.setattr(Runner, "stop", runner_stop)
    monkeypatch.setattr(
        interface_deep,
        "close_persistent_checkpointer",
        close_checkpointer,
    )
    monkeypatch.setattr(AgentRuntime, "_ensure_extensions", AsyncMock())
    monkeypatch.setattr(runtime_service_module, "_PROCESS_RUNTIME_DEPENDENCY_USERS", 0)

    failed_runtime = AgentRuntime()
    with pytest.raises(ValueError, match="checkpointer init failed"):
        await failed_runtime.start()

    assert runner_start.await_count == 0
    assert runner_stop.await_count == 0
    assert close_checkpointer.await_count == 1
    assert failed_runtime._shared_dependencies_acquired is False
    assert runtime_service_module._PROCESS_RUNTIME_DEPENDENCY_USERS == 0

    retry_runtime = AgentRuntime()
    await retry_runtime.start()
    await retry_runtime.close()
    await failed_runtime.close()

    assert initialize.await_count == 2
    assert runner_start.await_count == 1
    assert runner_stop.await_count == 1
    assert close_checkpointer.await_count == 2
    assert runtime_service_module._PROCESS_RUNTIME_DEPENDENCY_USERS == 0


@pytest.mark.asyncio
async def test_dispose_error_resets_checkpointer_state_and_reinitializes(
    monkeypatch,
    tmp_path,
) -> None:
    from openjiuwen.core.session.checkpointer import CheckpointerFactory
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    dispose_error = RuntimeError("dispose failed")
    dispose = AsyncMock(side_effect=dispose_error)
    stale_checkpointer = SimpleNamespace(
        _kv_store=SimpleNamespace(engine=SimpleNamespace(dispose=dispose))
    )
    replacement_checkpointer = object()
    create_checkpointer = AsyncMock(return_value=replacement_checkpointer)
    installed: list[object | None] = []

    monkeypatch.setattr(interface_deep, "_PERSISTENT_CHECKPOINTER_READY", True)
    monkeypatch.setattr(
        interface_deep,
        "PersistenceCheckpointerProvider",
        lambda: None,
    )
    monkeypatch.setattr(interface_deep, "get_checkpoint_dir", lambda: tmp_path)
    monkeypatch.setattr(
        CheckpointerFactory,
        "get_checkpointer",
        lambda: stale_checkpointer,
    )
    monkeypatch.setattr(CheckpointerFactory, "create", create_checkpointer)
    monkeypatch.setattr(
        CheckpointerFactory,
        "set_default_checkpointer",
        installed.append,
    )

    with pytest.raises(RuntimeError, match="dispose failed") as caught:
        await interface_deep.close_persistent_checkpointer()

    assert caught.value is dispose_error
    dispose.assert_awaited_once()
    assert installed == [None]
    assert interface_deep._PERSISTENT_CHECKPOINTER_READY is False

    await interface_deep.ensure_persistent_checkpointer()

    create_checkpointer.assert_awaited_once()
    assert installed == [None, replacement_checkpointer]
    assert interface_deep._PERSISTENT_CHECKPOINTER_READY is True


@pytest.mark.asyncio
async def test_runner_start_error_survives_checkpointer_rollback_error_and_retries(
    monkeypatch,
) -> None:
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    initialize = AsyncMock()
    runner_start = AsyncMock(
        side_effect=[ValueError("runner start failed"), True],
    )
    close_checkpointer = AsyncMock(
        side_effect=[RuntimeError("checkpointer close failed"), None],
    )
    monkeypatch.setattr(
        runtime_service_module,
        "_initialize_runtime_dependencies",
        initialize,
    )
    monkeypatch.setattr(Runner, "start", runner_start)
    monkeypatch.setattr(Runner, "stop", AsyncMock(return_value=True))
    monkeypatch.setattr(
        interface_deep,
        "close_persistent_checkpointer",
        close_checkpointer,
    )
    monkeypatch.setattr(AgentRuntime, "_ensure_extensions", AsyncMock())
    monkeypatch.setattr(runtime_service_module, "_PROCESS_RUNTIME_DEPENDENCY_USERS", 0)

    failed_runtime = AgentRuntime()
    with pytest.raises(ValueError, match="runner start failed"):
        await failed_runtime.start()

    assert failed_runtime.started is False
    assert failed_runtime._shared_dependencies_acquired is False
    assert runtime_service_module._PROCESS_RUNTIME_DEPENDENCY_USERS == 0

    retry_runtime = AgentRuntime()
    await retry_runtime.start()
    await retry_runtime.close()
    await failed_runtime.close()

    assert initialize.await_count == 2
    assert runner_start.await_count == 2
    assert close_checkpointer.await_count == 2
    assert runtime_service_module._PROCESS_RUNTIME_DEPENDENCY_USERS == 0


@pytest.mark.asyncio
async def test_runner_false_start_result_is_rolled_back(monkeypatch) -> None:
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    runner_stop = AsyncMock(return_value=True)
    close_checkpointer = AsyncMock()
    monkeypatch.setattr(
        runtime_service_module,
        "_initialize_runtime_dependencies",
        AsyncMock(),
    )
    monkeypatch.setattr(Runner, "start", AsyncMock(return_value=False))
    monkeypatch.setattr(Runner, "stop", runner_stop)
    monkeypatch.setattr(
        interface_deep,
        "close_persistent_checkpointer",
        close_checkpointer,
    )
    monkeypatch.setattr(runtime_service_module, "_PROCESS_RUNTIME_DEPENDENCY_USERS", 0)

    runtime = AgentRuntime()
    with pytest.raises(RuntimeError, match="Runner failed to start"):
        await runtime.start()

    runner_stop.assert_awaited_once()
    close_checkpointer.assert_awaited_once()
    assert runtime.started is False
    assert runtime._shared_dependencies_acquired is False
    assert runtime_service_module._PROCESS_RUNTIME_DEPENDENCY_USERS == 0


@pytest.mark.asyncio
async def test_runner_false_stop_result_closes_runtime_and_allows_retry(
    monkeypatch,
) -> None:
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    runner_start = AsyncMock(return_value=True)
    runner_stop = AsyncMock(side_effect=[False, True])
    close_checkpointer = AsyncMock()
    monkeypatch.setattr(
        runtime_service_module,
        "_initialize_runtime_dependencies",
        AsyncMock(),
    )
    monkeypatch.setattr(Runner, "start", runner_start)
    monkeypatch.setattr(Runner, "stop", runner_stop)
    monkeypatch.setattr(
        interface_deep,
        "close_persistent_checkpointer",
        close_checkpointer,
    )
    monkeypatch.setattr(AgentRuntime, "_ensure_extensions", AsyncMock())
    monkeypatch.setattr(runtime_service_module, "_PROCESS_RUNTIME_DEPENDENCY_USERS", 0)

    first = AgentRuntime()
    await first.start()
    with pytest.raises(RuntimeError, match="Runner failed to stop"):
        await first.close()

    assert first.closed is True
    assert first.started is False
    assert runtime_service_module._PROCESS_RUNTIME_DEPENDENCY_USERS == 0

    second = AgentRuntime()
    await second.start()
    await second.close()

    assert runner_start.await_count == 2
    assert runner_stop.await_count == 2
    assert close_checkpointer.await_count == 2
    assert runtime_service_module._PROCESS_RUNTIME_DEPENDENCY_USERS == 0


@pytest.mark.asyncio
async def test_extension_load_error_survives_shutdown_error_and_retries(
    monkeypatch,
) -> None:
    from jiuwenswarm.extensions.manager import ExtensionManager
    from jiuwenswarm.extensions.registry import ExtensionRegistry

    ExtensionRegistry.reset_instance()
    load_extensions = AsyncMock(
        side_effect=[ValueError("extension load failed"), None],
    )
    shutdown_extensions = AsyncMock(
        side_effect=[RuntimeError("extension shutdown failed"), None],
    )
    monkeypatch.setattr(ExtensionManager, "load_all_extensions", load_extensions)
    monkeypatch.setattr(
        ExtensionManager,
        "shutdown_all_extensions",
        shutdown_extensions,
    )
    monkeypatch.setattr(runtime_service_module, "_PROCESS_RUNTIME_EXTENSION_USERS", 0)
    monkeypatch.setattr(
        runtime_service_module,
        "_PROCESS_RUNTIME_EXTENSION_MANAGER",
        None,
    )
    monkeypatch.setattr(
        runtime_service_module,
        "_PROCESS_RUNTIME_EXTENSION_REGISTRY",
        None,
    )

    try:
        with pytest.raises(ValueError, match="extension load failed"):
            await runtime_service_module._acquire_process_runtime_extensions()

        with pytest.raises(RuntimeError, match="ExtensionRegistry"):
            ExtensionRegistry.get_instance()
        assert runtime_service_module._PROCESS_RUNTIME_EXTENSION_USERS == 0

        assert await runtime_service_module._acquire_process_runtime_extensions()
        await runtime_service_module._release_process_runtime_extensions()

        assert load_extensions.await_count == 2
        assert shutdown_extensions.await_count == 2
        assert runtime_service_module._PROCESS_RUNTIME_EXTENSION_USERS == 0
        with pytest.raises(RuntimeError, match="ExtensionRegistry"):
            ExtensionRegistry.get_instance()
    finally:
        ExtensionRegistry.reset_instance()


@pytest.mark.asyncio
async def test_start_preserves_primary_error_when_rollback_is_cancelled(
    monkeypatch,
) -> None:
    acquire_dependencies = AsyncMock()
    release_dependencies = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(
        runtime_service_module,
        "_acquire_process_runtime_dependencies",
        acquire_dependencies,
    )
    monkeypatch.setattr(
        runtime_service_module,
        "_release_process_runtime_dependencies",
        release_dependencies,
    )

    runtime = AgentRuntime()
    runtime._ensure_extensions = AsyncMock(
        side_effect=ValueError("extension initialization failed")
    )

    with pytest.raises(ValueError, match="extension initialization failed"):
        await runtime.start()

    acquire_dependencies.assert_awaited_once()
    release_dependencies.assert_awaited_once()
    assert runtime._shared_dependencies_acquired is False
    assert runtime.started is False


@pytest.mark.asyncio
async def test_close_finishes_cleanup_before_propagating_cancellation() -> None:
    manager = FakeAgentManager()
    manager.cancel_error = asyncio.CancelledError()

    async def initialize() -> None:
        return None

    runtime = AgentRuntime(agent_manager=manager, initializer=initialize)
    await runtime.start()

    with pytest.raises(asyncio.CancelledError):
        await runtime.close()

    assert manager.cancel_calls == ["[runtime close] "]
    assert manager.cleanup_calls == 1
    assert runtime.started is False
    assert runtime.closed is True

    await runtime.close()
    assert manager.cleanup_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [TimeoutError, RuntimeError])
async def test_close_drain_failure_still_runs_host_and_agent_cleanup(error_type) -> None:
    manager = FakeAgentManager()
    lease = SimpleNamespace(release=AsyncMock())
    runtime = AgentRuntime(agent_manager=manager, resource_lease=lease)
    coordinator_close = AsyncMock(side_effect=error_type("drain failed"))
    runtime._session_coordinator.close = coordinator_close

    with pytest.raises(error_type, match="drain failed"):
        await runtime.close()

    assert runtime.closed
    assert manager.cleanup_calls == 1
    lease.release.assert_awaited_once()
    coordinator_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_timeout_keeps_runtime_and_agent_resources_alive() -> None:
    manager = FakeAgentManager()
    lease = SimpleNamespace(release=AsyncMock())
    runtime = AgentRuntime(agent_manager=manager, resource_lease=lease)
    error = SessionCloseTimeoutError("runtime", ("heartbeat-run",))
    runtime._session_coordinator.close = AsyncMock(side_effect=error)

    with pytest.raises(SessionCloseTimeoutError):
        await runtime.close()

    assert not runtime.closed
    assert manager.cleanup_calls == 0
    lease.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_resource_lease_failure_propagates_after_agent_cleanup() -> None:
    manager = FakeAgentManager()
    lease = SimpleNamespace(
        release=AsyncMock(side_effect=RuntimeError("lease release failed"))
    )
    runtime = AgentRuntime(agent_manager=manager, resource_lease=lease)

    with pytest.raises(RuntimeError, match="lease release failed"):
        await runtime.close()
    await runtime.close()

    lease.release.assert_awaited_once()
    assert manager.cleanup_calls == 1
    assert runtime.closed


@pytest.mark.asyncio
async def test_plain_runtime_does_not_release_another_runtimes_lease() -> None:
    lease = SimpleNamespace(release=AsyncMock())
    owner = AgentRuntime(agent_manager=FakeAgentManager(), resource_lease=lease)
    plain = AgentRuntime(agent_manager=FakeAgentManager())

    await plain.close()
    lease.release.assert_not_awaited()
    await owner.close()
    lease.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_server_stop_never_closes_kvc_runtime(monkeypatch) -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_application_runtime

    server = AgentWebSocketServer()
    previous_runtime = server.get_runtime()
    server._personal_context_host.stop = AsyncMock()
    close_kvc = AsyncMock()
    monkeypatch.setattr(kv_cache_application_runtime, "close_kv_cache_runtime", close_kvc)

    await server.stop()

    close_kvc.assert_not_awaited()
    assert previous_runtime.closed
    assert server.get_runtime() is not previous_runtime
    server._personal_context_host.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_server_drain_cancellation_still_closes_runtime(monkeypatch) -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_model_provider

    monkeypatch.setattr(kv_cache_model_provider, "is_kv_cache_affinity_enabled", lambda: True)

    server = AgentWebSocketServer()
    previous_runtime = server.get_runtime()
    previous_runtime._session_coordinator.close = AsyncMock(
        side_effect=asyncio.CancelledError
    )
    server._personal_context_host.stop = AsyncMock()

    with pytest.raises(asyncio.CancelledError):
        await server.stop()

    assert previous_runtime.closed
    assert server.get_runtime() is not previous_runtime
    server._personal_context_host.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("affinity_enabled", [False, True])
async def test_agent_server_close_does_not_schedule_remote_kvc_cleanup(
    monkeypatch, affinity_enabled,
) -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.session.kv_cache import (
        kv_cache_application_runtime,
        kv_cache_model_provider,
    )

    monkeypatch.setattr(kv_cache_model_provider, "is_kv_cache_affinity_enabled", lambda: affinity_enabled)
    events: list[str] = []

    async def record(event: str) -> None:
        events.append(event)

    server = AgentWebSocketServer()
    closing_runtime = server.get_runtime()
    async def drain_sessions():
        await record("session-drain")

    async def cancel_work(*args):
        await record("inflight-cancel")

    async def dispose_agents():
        await record("agent-cleanup")

    closing_runtime._session_coordinator.close = AsyncMock(side_effect=drain_sessions)
    closing_runtime.agent_manager.cancel_all_inflight_work = AsyncMock(side_effect=cancel_work)
    closing_runtime.agent_manager.cleanup = AsyncMock(side_effect=dispose_agents)
    server._suspend_kv_cache = AsyncMock(
        side_effect=lambda: events.append("kvc-suspend")
    )
    close_kvc = AsyncMock(side_effect=lambda: record("kvc-runtime-close"))
    monkeypatch.setattr(kv_cache_application_runtime, "close_kv_cache_runtime", close_kvc)

    await server.stop()

    assert events == [
        "kvc-suspend",
        "session-drain",
        "inflight-cancel",
        "agent-cleanup",
    ]
    close_kvc.assert_not_awaited()
    closing_runtime._session_coordinator.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_server_stop_replaces_closed_one_shot_runtime(monkeypatch) -> None:
    from jiuwenswarm.server import agent_ws_server as server_module
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    server = AgentWebSocketServer()
    previous_runtime = server.get_runtime()
    previous_plan_controller = previous_runtime.plan_controller
    previous_plan_controller.active_sessions.add("session-before-stop")
    previous_plan_controller.exited_sessions.add("session-before-stop")
    previous_lock = previous_plan_controller.lock_for("session-before-stop")
    previous_runtime.close = AsyncMock()
    await server.stop()

    previous_runtime.close.assert_awaited_once()
    assert server.get_runtime() is not previous_runtime
    assert server.get_runtime().closed is False
    assert server.get_runtime().agent_manager is server.get_agent_manager()
    recovered_plan_controller = server.get_runtime().plan_controller
    assert recovered_plan_controller is not previous_plan_controller
    assert recovered_plan_controller.active_sessions == set()
    assert recovered_plan_controller.exited_sessions == set()
    assert recovered_plan_controller.sync_locks.get("session-before-stop") is None
    assert server_module._plan_active_sessions is recovered_plan_controller.active_sessions
    assert server_module._plan_exited_sessions is recovered_plan_controller.exited_sessions
    assert server_module._session_mode_sync_locks is recovered_plan_controller.sync_locks
    assert previous_lock is not None


@pytest.mark.asyncio
async def test_agent_server_stop_does_not_recreate_kvc_runtime(monkeypatch) -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.session.kv_cache import (
        kv_cache_application_owner,
        kv_cache_application_runtime,
        kv_cache_model_provider,
    )

    runtime_lookups: list[str] = []
    monkeypatch.setattr(kv_cache_application_owner, "_OWNER", None)
    monkeypatch.setattr(
        kv_cache_model_provider,
        "is_kv_cache_affinity_enabled",
        lambda: True,
    )
    kvc_runtime = object()
    monkeypatch.setattr(
        kv_cache_application_runtime,
        "get_kv_cache_runtime",
        lambda: runtime_lookups.append("get") or kvc_runtime,
    )
    close_kvc = AsyncMock()
    monkeypatch.setattr(kv_cache_application_runtime, "close_kv_cache_runtime", close_kvc)
    server = AgentWebSocketServer()
    server._personal_context_host.stop = AsyncMock()

    assert runtime_lookups == []
    await server._kv_cache_application_owner.set_enabled(True)
    assert runtime_lookups == ["get"]
    await server.stop()

    assert runtime_lookups == ["get"]
    assert server._kv_cache_application_owner.state.value == "suspended"
    close_kvc.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_server_stop_retains_runtime_when_close_is_rejected(
    monkeypatch,
) -> None:
    from jiuwenswarm.server import agent_ws_server as server_module
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.session.kv_cache import (
        kv_cache_application_runtime,
        kv_cache_model_provider,
    )

    monkeypatch.setattr(kv_cache_model_provider, "is_kv_cache_affinity_enabled", lambda: True)

    class FakeWebSocketServer:
        def __init__(self) -> None:
            self.closed = False
            self.wait_closed = AsyncMock()

        def close(self) -> None:
            self.closed = True

    server = AgentWebSocketServer()
    previous_runtime = server.get_runtime()
    listener = FakeWebSocketServer()
    server._server = listener
    runtime_push_handler = object()
    previous_runtime_push_handler = object()
    server._runtime_push_handler = runtime_push_handler
    server._previous_runtime_push_handler = previous_runtime_push_handler
    restore_runtime_push_handler = MagicMock()
    monkeypatch.setattr(
        server_module,
        "restore_runtime_push_handler",
        restore_runtime_push_handler,
    )
    server._jiuwenbox_runner.stop = AsyncMock()
    server._personal_context_host.stop = AsyncMock()
    close_rejected = RuntimeStateError(
        "runtime has unfinished session provisions; commit or abort them before close"
    )
    previous_runtime._session_provision_prepares = 1
    previous_runtime.close = AsyncMock(wraps=previous_runtime.close)
    await server._kv_cache_application_owner.activate_from_config()
    assert server._kv_cache_application_owner.state.value == "enabled"
    close_kv_cache_runtime = AsyncMock()
    monkeypatch.setattr(
        kv_cache_application_runtime,
        "close_kv_cache_runtime",
        close_kv_cache_runtime,
    )

    with pytest.raises(RuntimeStateError) as caught:
        await server.stop()

    assert str(caught.value) == str(close_rejected)
    assert server.get_runtime() is previous_runtime
    assert server.get_agent_manager() is previous_runtime.agent_manager
    assert listener.closed is True
    listener.wait_closed.assert_awaited_once_with()
    restore_runtime_push_handler.assert_called_once_with(
        runtime_push_handler,
        previous_runtime_push_handler,
    )
    server._jiuwenbox_runner.stop.assert_awaited_once_with()
    server._personal_context_host.stop.assert_awaited_once_with()
    previous_runtime.close.assert_awaited_once_with()
    close_kv_cache_runtime.assert_not_awaited()
    assert server._kv_cache_application_owner.state.value == "suspended"

    previous_runtime._session_provision_prepares = 0
    await server.stop()

    assert previous_runtime.close.await_count == 2
    close_kv_cache_runtime.assert_not_awaited()
    assert server.get_runtime() is not previous_runtime
    assert server.get_runtime().agent_manager is server.get_agent_manager()


@pytest.mark.asyncio
async def test_agent_server_stop_replaces_runtime_after_closed_cleanup_error(
    monkeypatch,
) -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    server = AgentWebSocketServer()
    previous_runtime = server.get_runtime()

    async def close_with_cleanup_error() -> None:
        previous_runtime._started = False
        previous_runtime._closed = True
        raise RuntimeError("runtime cleanup failed")

    previous_runtime.close = AsyncMock(side_effect=close_with_cleanup_error)

    await server.stop()

    previous_runtime.close.assert_awaited_once_with()
    assert previous_runtime.closed is True
    assert server.get_runtime() is not previous_runtime
    assert server.get_runtime().agent_manager is server.get_agent_manager()


@pytest.mark.asyncio
async def test_agent_server_start_restores_remote_service_after_stop(
    monkeypatch,
) -> None:
    from jiuwenswarm.server import agent_ws_server as server_module
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    class FakeWebSocketServer:
        def __init__(self) -> None:
            self.closed = False
            self.wait_closed = AsyncMock()

        def close(self) -> None:
            self.closed = True

    listeners: list[FakeWebSocketServer] = []

    async def serve(*args: object, **kwargs: object) -> FakeWebSocketServer:
        listener = FakeWebSocketServer()
        listeners.append(listener)
        return listener

    monkeypatch.setattr("websockets.legacy.server.serve", serve)
    install_runtime_push_handler = MagicMock(return_value=None)
    monkeypatch.setattr(
        server_module,
        "install_runtime_push_handler",
        install_runtime_push_handler,
    )
    restore_runtime_push_handler = MagicMock()
    monkeypatch.setattr(
        server_module,
        "restore_runtime_push_handler",
        restore_runtime_push_handler,
    )

    server = AgentWebSocketServer()
    server._bootstrap_internal_jiuwenbox = AsyncMock()
    monkeypatch.setattr(server._jiuwenbox_runner, "stop", AsyncMock())
    first_runtime = server.get_runtime()
    first_manager = server.get_agent_manager()
    first_runtime.start = AsyncMock()
    first_runtime.close = AsyncMock(wraps=first_runtime.close)

    await server.start(bind_transport=True)
    await server._checkpointer_warmup_task
    await server.stop()

    recovered_runtime = server.get_runtime()
    recovered_manager = server.get_agent_manager()
    recovered_runtime.start = AsyncMock()
    recovered_runtime.close = AsyncMock(wraps=recovered_runtime.close)

    await server.start(bind_transport=True)
    await server._checkpointer_warmup_task

    assert len(listeners) == 2
    assert listeners[0].closed is True
    listeners[0].wait_closed.assert_awaited_once_with()
    first_runtime.start.assert_awaited_once_with()
    first_runtime.close.assert_awaited_once_with()
    assert recovered_runtime is not first_runtime
    assert recovered_manager is not first_manager
    assert recovered_runtime.agent_manager is recovered_manager
    recovered_runtime.start.assert_awaited_once_with()
    assert server._server is listeners[1]
    assert install_runtime_push_handler.call_count == 2
    restore_runtime_push_handler.assert_called_once()

    await server.stop()
    recovered_runtime.close.assert_awaited_once_with()
    assert listeners[1].closed is True


class _ArchiveStopAgentManager(FakeAgentManager):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order
        self.release_calls: list[tuple[str, str, str]] = []
        self.cancel_session_calls: list[tuple[str, str]] = []

    async def release_subagent_runtime_for_session(
        self,
        *,
        channel_id: str = "",
        session_id: str,
        reason: str = "",
    ) -> bool:
        self.release_calls.append((channel_id, session_id, reason))
        return False

    async def cancel_session_tasks(
        self,
        *,
        channel_id: str = "",
        session_id: str,
    ) -> None:
        self._order.append("cancel_session_tasks")
        self.cancel_session_calls.append((channel_id, session_id))

    async def cleanup_session_runtime(
        self,
        *,
        channel_id: str,
        session_id: str,
    ) -> bool:
        self._order.append("cleanup_session_runtime")
        return await super().cleanup_session_runtime(
            channel_id=channel_id, session_id=session_id
        )


class _RecordingTeamManager:
    """TeamManager stand-in capturing stop_session_runtime archive calls."""

    def __init__(
        self,
        order: list[str],
        *,
        error: Exception | None = None,
    ) -> None:
        self._order = order
        self._error = error
        self.stop_calls: list[tuple[str, str, bool]] = []
        self.channel_ids: list[str | None] = []

    async def stop_session_runtime(
        self,
        session_id: str,
        reason: str = "",
        *,
        stop_runner: bool = True,
        generation: int | None = None,
    ) -> bool:
        self._order.append("team_stop")
        self.stop_calls.append((session_id, reason, stop_runner))
        if self._error is not None:
            raise self._error
        return True


@pytest.mark.asyncio
async def test_stop_session_for_archive_stops_parked_team_runtime_first(
    monkeypatch,
) -> None:
    """Delete stop drains the parked Team leader stream before the strict check.

    A swarmflow pause/stop parks the persistent leader stream; its per-session
    state keeps ``has_session_runtime`` true forever, so without this teardown
    session.delete always fails with ``cleanup_session_runtime failed``.
    """
    from jiuwenswarm.agents.harness import team as team_harness
    from jiuwenswarm.server.runtime.session import lifecycle as session_lifecycle

    monkeypatch.setattr(
        session_lifecycle, "assert_runtime_owner", lambda session_id: None
    )
    monkeypatch.setattr(session_lifecycle, "release_runtime", lambda session_id: None)

    order: list[str] = []
    team_manager = _RecordingTeamManager(order)

    def fake_get_team_manager(channel_id=None):
        team_manager.channel_ids.append(channel_id)
        return team_manager

    monkeypatch.setattr(team_harness, "get_team_manager", fake_get_team_manager)
    manager = _ArchiveStopAgentManager(order)
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
    )

    await runtime.stop_session_for_archive(
        channel_id="web", session_id="web_parked_1"
    )

    assert team_manager.stop_calls == [("web_parked_1", "session.lifecycle: ", False)]
    assert team_manager.channel_ids == ["web"]
    assert order == [
        "team_stop",
        "cancel_session_tasks",
        "cleanup_session_runtime",
    ]
    assert manager.cleanup_session_calls == [("web", "web_parked_1")]
    assert manager.cancel_session_calls == [("web", "web_parked_1")]
    assert manager.release_calls == [("web", "web_parked_1", "session_archived")]


@pytest.mark.asyncio
async def test_stop_session_for_archive_survives_team_stop_failure(
    monkeypatch,
) -> None:
    """A failing team teardown is logged, not fatal: the strict drain continues."""
    from jiuwenswarm.agents.harness import team as team_harness
    from jiuwenswarm.server.runtime.session import lifecycle as session_lifecycle

    monkeypatch.setattr(
        session_lifecycle, "assert_runtime_owner", lambda session_id: None
    )
    monkeypatch.setattr(session_lifecycle, "release_runtime", lambda session_id: None)

    team_manager = _RecordingTeamManager(
        [], error=RuntimeError("team stop exploded")
    )
    monkeypatch.setattr(
        team_harness, "get_team_manager", lambda channel_id=None: team_manager
    )
    manager = _ArchiveStopAgentManager([])
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
    )

    await runtime.stop_session_for_archive(
        channel_id="web", session_id="web_parked_2"
    )

    assert team_manager.stop_calls == [("web_parked_2", "session.lifecycle: ", False)]
    assert manager.cleanup_session_calls == [("web", "web_parked_2")]


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_archive", [False, True])
async def test_unarchive_after_stop_timeout_accepts_a_new_turn(
    monkeypatch: pytest.MonkeyPatch,
    finish_archive: bool,
) -> None:
    from jiuwenswarm.runtime.session import RuntimeSessionState
    from jiuwenswarm.server.runtime.session import lifecycle as lc
    from jiuwenswarm.server.runtime.session.session_archive import SessionArchiveService

    sid = "web_archive_timeout"
    directory = lc.session_paths(sid)[0]
    directory.mkdir(parents=True)
    lc.atomic_json(
        directory / "metadata.json",
        {"session_id": sid, "channel_id": "web", "project_id": "default"},
    )
    manager = _ArchiveStopAgentManager([])
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
    )
    monkeypatch.setattr(runtime, "_stop_team_runtime_for_lifecycle", AsyncMock())
    await runtime.start()
    await runtime._register_session(session_id=sid, channel_id="web")
    coordinator = runtime._session_coordinator
    started = asyncio.Event()
    release = asyncio.Event()

    async def resistant() -> None:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    old_turn = asyncio.create_task(
        coordinator.run_unary(sid, "old", SessionWorkKind.CHAT_UNARY, resistant)
    )
    service = SessionArchiveService(runtime)
    try:
        await started.wait()
        lc.begin("session", sid, "archive")
        result = await coordinator.close_session(sid, wait_timeout=0.01)
        assert result.timed_out
        lc.update("session", sid, status="failed", errors=["stop timed out"])
        release.set()
        await old_turn
        assert coordinator.snapshot_session(sid).state is RuntimeSessionState.QUIESCING

        if finish_archive:
            await service._finalize_session(sid, "archive", "web", "default", 1)
            assert lc.state("session", sid)["operation"]["status"] == "completed"
            assert coordinator.snapshot_session(sid).state is RuntimeSessionState.CLOSED
        restored = await service.session(sid, "unarchive", "web")
        assert restored["restored"] is True
        request = AgentRequest(
            request_id="after-unarchive",
            channel_id="web",
            session_id=sid,
            req_method=ReqMethod.CHAT_SEND,
            is_stream=True,
            params={"query": "continue", "mode": "agent.work.normal"},
        )
        events = await _collect_events(runtime.stream(request, trigger_hook=False))

        assert events[-1].event_type == "chat.final"
        assert coordinator.snapshot_session(sid).generation == 2
        assert manager.cleanup_session_calls == [("web", sid)]
    finally:
        release.set()
        await old_turn
        await service.close()
        await runtime.close()


@pytest.mark.asyncio
async def test_quiescing_session_cannot_resume_through_archive_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.runtime.session import RuntimeSessionState
    from jiuwenswarm.server.runtime.session import lifecycle as lc

    sid = "web_archive_fenced"
    runtime = AgentRuntime(agent_manager=FakeAgentManager(), initializer=AsyncMock())
    await runtime.start()
    await runtime._register_session(session_id=sid, channel_id="web")
    await runtime._session_coordinator.quiesce_session(sid)
    stopper = AsyncMock()
    monkeypatch.setattr(runtime, "stop_session_for_archive", stopper)
    lc.begin("session", sid, "archive")
    request = AgentRequest(
        request_id="still-archiving",
        channel_id="web",
        session_id=sid,
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "continue"},
    )
    try:
        with pytest.raises(lc.LifecycleError) as error:
            await runtime.invoke(request)
        assert error.value.code == "OPERATION_IN_PROGRESS"
        stopper.assert_not_awaited()
        snapshot = runtime._session_coordinator.snapshot_session(sid)
        assert snapshot.state is RuntimeSessionState.QUIESCING
        assert snapshot.generation == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", [1, 2])
async def test_lifecycle_stop_only_cancels_its_heartbeat_generation(
    monkeypatch: pytest.MonkeyPatch,
    generation: int,
) -> None:
    from jiuwenswarm.runtime.session import RuntimeSessionState

    sid = "web_restored_heartbeat"
    manager = _ArchiveStopAgentManager([])
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock())
    monkeypatch.setattr(runtime, "_stop_team_runtime_for_lifecycle", AsyncMock())
    await runtime.start()
    await runtime._register_session(session_id=sid, channel_id="web")
    await runtime._session_coordinator.close_session(sid)
    await runtime._register_session(session_id=sid, channel_id="web")
    heartbeat_stop = AsyncMock(return_value=False)
    runtime._admission_controller = SimpleNamespace(stop_active_heartbeat=heartbeat_stop)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def heartbeat() -> None:
        entered.set()
        await release.wait()

    task = asyncio.create_task(runtime._session_coordinator.run_unary(
        sid, "new-heartbeat", SessionWorkKind.HEARTBEAT, heartbeat
    ))
    try:
        await entered.wait()
        await runtime.stop_session_for_lifecycle(
            channel_id="web", session_id=sid, generation=generation, stop_runner=True
        )

        snapshot = runtime._session_coordinator.snapshot_session(sid)
        assert snapshot.generation == 2
        if generation == 1:
            heartbeat_stop.assert_not_awaited()
            runtime._stop_team_runtime_for_lifecycle.assert_not_awaited()
            assert snapshot.state is RuntimeSessionState.ACTIVE
            assert not task.done()
            assert manager.cleanup_session_calls == []
        else:
            heartbeat_stop.assert_awaited_once_with(sid)
            assert snapshot.state is RuntimeSessionState.CLOSED
            assert task.done()
            assert manager.cleanup_session_calls == [("web", sid)]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_lifecycle_stop_rechecks_generation_after_heartbeat_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.runtime.session import RuntimeSessionState

    sid = "web_replaced_during_heartbeat_stop"
    manager = _ArchiveStopAgentManager([])
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock())
    monkeypatch.setattr(runtime, "_stop_team_runtime_for_lifecycle", AsyncMock())
    await runtime.start()
    await runtime._register_session(session_id=sid, channel_id="web")
    entered = asyncio.Event()
    release = asyncio.Event()
    tasks = []

    async def new_heartbeat() -> None:
        entered.set()
        await release.wait()

    async def replace_while_stopping(session_id: str) -> bool:
        await runtime._session_coordinator.close_session(session_id, generation=1)
        await runtime._register_session(session_id=session_id, channel_id="web")
        tasks.append(asyncio.create_task(runtime._session_coordinator.run_unary(
            sid, "new-heartbeat", SessionWorkKind.HEARTBEAT, new_heartbeat
        )))
        await entered.wait()
        return True

    runtime._admission_controller = SimpleNamespace(stop_active_heartbeat=replace_while_stopping)
    try:
        await runtime.stop_session_for_lifecycle(
            channel_id="web", session_id=sid, generation=1, stop_runner=True
        )

        snapshot = runtime._session_coordinator.snapshot_session(sid)
        assert snapshot.generation == 2
        assert snapshot.state is RuntimeSessionState.ACTIVE
        assert not tasks[0].done()
        runtime._stop_team_runtime_for_lifecycle.assert_not_awaited()
        assert manager.cleanup_session_calls == []
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_lifecycle_stop_does_not_select_replacement_admission_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "web_heartbeat_admission_replaced"
    runtime = AgentRuntime(
        agent_manager=_ArchiveStopAgentManager([]), initializer=AsyncMock()
    )
    monkeypatch.setattr(runtime, "_stop_team_runtime_for_lifecycle", AsyncMock())
    await runtime.start()
    await runtime._register_session(session_id=sid, channel_id="web")
    admission = SessionRunAdmission()
    runtime._admission_controller = admission

    async def preempt(run_id: str) -> bool:
        await admission.end_heartbeat(sid, run_id)
        return True

    preemptor = AsyncMock(side_effect=preempt)
    admission.set_heartbeat_preemptor(preemptor)
    assert await admission.try_begin_heartbeat(sid, "old-run")
    await admission._condition.acquire()
    task = asyncio.create_task(runtime.stop_session_for_lifecycle(
        channel_id="web", session_id=sid, generation=1, stop_runner=True
    ))
    try:
        await asyncio.sleep(0)
        assert not task.done()
        await runtime._session_coordinator.close_session(sid, generation=1)
        await runtime._register_session(session_id=sid, channel_id="web")
        # Model the lock holder replacing its admission marker before release.
        admission._states[sid].heartbeat_run_id = "new-run"
        admission._condition.release()
        await asyncio.wait_for(task, 1)

        preemptor.assert_not_awaited()
        assert admission._states[sid].heartbeat_run_id == "new-run"
        assert runtime._session_coordinator.snapshot_session(sid).generation == 2
        runtime._stop_team_runtime_for_lifecycle.assert_not_awaited()
    finally:
        if admission._condition.locked():
            admission._condition.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("parked_request", [False, True])
async def test_stop_session_for_archive_cancels_requests_without_waiting_for_caches(
    monkeypatch, parked_request,
) -> None:
    """Real cached adapters clean promptly, with or without a parked request."""
    from collections import Counter
    from jiuwenswarm.agents.harness import team as team_harness
    from jiuwenswarm.server.runtime.agent_manager import AgentManager
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    from jiuwenswarm.server.runtime.session.session_manager import SessionManager
    from jiuwenswarm.server.runtime.session import lifecycle as session_lifecycle

    sid = "web_parked_3"
    other_sid = "web_other"
    monkeypatch.setattr(
        session_lifecycle, "assert_runtime_owner", lambda session_id: None
    )
    monkeypatch.setattr(session_lifecycle, "release_runtime", lambda session_id: None)
    monkeypatch.setattr(
        team_harness,
        "get_team_manager",
        lambda channel_id=None: _RecordingTeamManager([]),
    )
    root = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    root._is_session_scoped_adapter = False
    root._active_session_ids = Counter()
    root._session_agent_tasks = {}
    root._session_adapter_locks = {sid: asyncio.Lock()}
    root._session_adapter_last_used = {}
    root._session_adapter_versions = {}
    root._session_adapter_reload_failures = {}

    def child_adapter(session_id):
        child = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
        child._is_session_scoped_adapter = True
        child._parent_session_id = session_id
        child._active_session_ids = Counter()
        child._session_agent_tasks = {}
        child._permission_state = SimpleNamespace(permission_isolated=False)
        child._has_live_root_permission_owner = lambda sid: False
        child.is_deep_agent_executing_for_session = lambda sid: False
        child.cleanup = AsyncMock()
        return child

    child = child_adapter(sid)
    other = child_adapter(other_sid)
    root._session_adapters = {sid: child, other_sid: other}
    facade = JiuWenSwarm.__new__(JiuWenSwarm)
    facade._adapter = root
    facade._session_manager = SessionManager()
    manager = AgentManager()
    manager.agents["web"] = {"team::": facade}
    manager.release_subagent_runtime_for_session = AsyncMock(return_value=False)
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
    )

    async def parked_handler(adapter, session_id, ready):
        adapter._register_session_agent_task(session_id)
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            adapter._unregister_session_agent_task(session_id)

    ready = asyncio.Event()
    other_task = asyncio.create_task(parked_handler(other, other_sid, ready))
    await ready.wait()
    request_task = None
    if parked_request:
        ready = asyncio.Event()
        request_task = asyncio.create_task(parked_handler(child, sid, ready))
        await ready.wait()
    try:
        assert facade.has_session_runtime(sid)
        # Cached adapters are retained until cleanup. A permanently parked
        # request must be cancelled and joined instead of waiting for a tick.
        await asyncio.wait_for(
            runtime.stop_session_for_archive(channel_id="web", session_id=sid),
            timeout=0.5,
        )
        assert not facade.has_session_runtime(sid)
        assert not child.is_session_active(sid)
        child.cleanup.assert_awaited_once()
        if request_task is not None:
            assert request_task.cancelled()
        assert not other_task.done()
        assert facade.has_session_runtime(other_sid)
        other.cleanup.assert_not_awaited()
    finally:
        for task in (request_task, other_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *[task for task in (request_task, other_task) if task is not None],
            return_exceptions=True,
        )


def _session_waiting_on_card() -> SimpleNamespace:
    waiting = SimpleNamespace(state=SessionExecutionState.WAITING_FOR_CONTROL)
    return SimpleNamespace(
        _session_coordinator=SimpleNamespace(
            snapshot_session=lambda _sid: SimpleNamespace(executions=(waiting,))
        )
    )


async def test_voice_task_is_refused_before_admission_and_the_card_stays_clickable() -> None:
    """#5487: admitting a voice turn cancels the execution that waits on a card,
    so the card could no longer be clicked. The refusal must happen before
    admission and be a terminal chat.error, so the voice task fails instead of
    becoming unknown and holding the voice queue."""
    seen: list[str] = []

    class CardAgent(FakeAgent):
        async def process_message_stream(self, request: AgentRequest):
            seen.append(request.request_id)
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "chat.ask_user_question",
                    "request_id": "call_cua",
                    "source": "confirm_interrupt",
                    "questions": [],
                },
            )

        async def deliver_control_input(self, request: AgentRequest):
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": "approved"},
                is_complete=True,
            )

    class CardManager(FakeAgentManager):
        def get_agent_for_session_nowait(self, channel_id: str, session_id: str) -> object:
            return self.agent

    manager = CardManager()
    manager.agent = CardAgent()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
    )
    runtime._trigger_before_chat_request_hook = AsyncMock()
    session_id = "voice-card-session"
    params = {"mode": "agent", "work_mode": "work"}
    typed = AgentRequest(
        request_id="typed", channel_id="web", session_id=session_id,
        req_method=ReqMethod.CHAT_SEND, is_stream=True, params={**params, "query": "open notepad"},
    )
    await _collect_events(runtime.stream(typed, trigger_hook=False))

    voice = AgentRequest(
        request_id="task-voice", channel_id="video_tool", session_id=session_id,
        req_method=ReqMethod.CHAT_SEND, is_stream=True, params={**params, "query": "append a line"},
    )
    events = await _collect_events(runtime.stream(voice, trigger_hook=False))

    assert [(event.event_type, event.ok, event.is_complete) for event in events] == [
        ("chat.error", False, True)
    ]
    assert seen == ["typed"]
    snapshot = runtime._session_coordinator.snapshot_session(session_id)
    assert snapshot is not None
    assert [item.state.value for item in snapshot.executions] == ["waiting_for_control"]

    click = AgentRequest(
        request_id="click", channel_id="web", session_id=session_id,
        req_method=ReqMethod.CHAT_SEND, is_stream=True,
        params={
            **params, "query": "", "request_id": "call_cua", "source": "confirm_interrupt",
            "answers": [{"question": "", "selected_options": ["allow_once"]}],
        },
    )
    clicked = await asyncio.wait_for(_collect_events(runtime.stream(click, trigger_hook=False)), timeout=0.5)
    assert [event.event_type for event in clicked] == ["chat.final"]


def test_typed_input_still_supersedes_a_waiting_card() -> None:
    request = AgentRequest(
        request_id="r1", channel_id="web", session_id="chat-1",
        req_method=ReqMethod.CHAT_SEND, params={"mode": "agent.code.normal"},
    )

    assert AgentRuntime._voice_task_card_refusal(_session_waiting_on_card(), request) == []


async def test_voice_task_approval_resume_reaches_the_chat_agent_that_owns_it() -> None:
    """#5885: a voice task runs on its chat's agent (#9603), so the approval it
    raises waits there. The resume arrives on ``video_tool``; looking the agent
    up by that ingress channel found nothing, the resumed turn produced no final
    and the task became unknown, blocking every later voice task in the chat."""
    seen: list[tuple[str, str]] = []

    class ChatAgent(FakeAgent):
        async def process_message_stream(self, request: AgentRequest):
            seen.append(("stream", request.request_id))
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "chat.ask_user_question",
                    "request_id": "call_notepad",
                    "source": "permission_interrupt",
                    "questions": [],
                },
            )

        async def deliver_control_input(self, request: AgentRequest):
            seen.append(("control", request.request_id))
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": "notepad typed"},
                is_complete=True,
            )

    class ChatOwnedManager(FakeAgentManager):
        def get_agent_for_session_nowait(self, channel_id: str, session_id: str) -> object:
            return self.agent if channel_id == "web" else None

    manager = ChatOwnedManager()
    manager.agent = ChatAgent()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
    )
    runtime._trigger_before_chat_request_hook = AsyncMock()
    session_id = "voice-task-chat"
    params = {"mode": "agent", "work_mode": "work"}
    with patch(
        "jiuwenswarm.runtime.request.resolve_request_owner_channel",
        side_effect=lambda request: "web" if request.channel_id == "video_tool" else request.channel_id,
    ):
        task = AgentRequest(
            request_id="task-1", channel_id="video_tool", session_id=session_id,
            req_method=ReqMethod.CHAT_SEND, is_stream=True,
            params={**params, "query": "open notepad and type hello"},
        )
        await _collect_events(runtime.stream(task, trigger_hook=False))

        resume = AgentRequest(
            request_id="task-1", channel_id="video_tool", session_id=session_id,
            req_method=ReqMethod.CHAT_SEND, is_stream=True,
            params={
                **params, "query": "", "request_id": "call_notepad",
                "source": "permission_interrupt",
                "answers": [{"question": "", "selected_options": ["allow_once"]}],
            },
        )
        resumed = await asyncio.wait_for(
            _collect_events(runtime.stream(resume, trigger_hook=False)), timeout=0.5,
        )

    assert [event.event_type for event in resumed] == ["chat.final"]
    assert seen == [("stream", "task-1"), ("control", "task-1")]
