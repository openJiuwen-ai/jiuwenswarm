# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm import runtime as runtime_package
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import AgentRuntime, RuntimeStateError
from jiuwenswarm.runtime import service as runtime_service_module
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.runtime.plan import PlanStateResult


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
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_product_hooks

    started: list[tuple[str, str]] = []
    finished: list[tuple[str, bool]] = []

    async def record_started(
        *, session_id: str, params: dict[str, object], channel_id: str
    ) -> None:
        started.append((session_id, channel_id))

    def record_finished(*, session_id: str, succeeded: bool) -> None:
        finished.append((session_id, succeeded))

    monkeypatch.setattr(kv_cache_product_hooks, "record_chat_started", record_started)
    monkeypatch.setattr(kv_cache_product_hooks, "record_chat_finished", record_finished)

    runtime = AgentRuntime(agent_manager=FakeAgentManager())
    request = AgentRequest(
        request_id="kvc-contract",
        channel_id="web",
        session_id="session-a",
        req_method=ReqMethod.CHAT_SEND,
        params={},
    )

    await runtime._record_kvc_chat_started(request)
    runtime._record_kvc_chat_finished(request, succeeded=True)

    assert started == [("session-a", "web")]
    assert finished == [("session-a", True)]


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
async def test_session_operations_require_started_runtime() -> None:
    runtime = AgentRuntime(agent_manager=FakeAgentManager(), initializer=lambda: None)

    with pytest.raises(RuntimeStateError, match="not started"):
        await runtime.create_or_resume_session(channel_id="process_cli")


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

    mode, sub_mode, agent = await runtime.prepare_chat_turn(
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
async def test_cancel_all_team_stream_tasks_delegates_without_starting_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness import team as team_module

    helper = AsyncMock()
    monkeypatch.setattr(
        team_module,
        "cancel_all_team_stream_tasks_across_managers",
        helper,
    )
    initializer = AsyncMock()
    runtime = AgentRuntime(agent_manager=FakeAgentManager(), initializer=initializer)

    await runtime.cancel_all_team_stream_tasks(
        reason="[gateway ws closed] ",
        exclude_session_ids=("heartbeat-session",),
    )

    helper.assert_awaited_once_with(
        reason="[gateway ws closed] ",
        exclude_session_ids={"heartbeat-session"},
    )
    assert runtime.started is False
    initializer.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_all_team_stream_tasks_preserves_none_exclusions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness import team as team_module

    helper = AsyncMock()
    monkeypatch.setattr(
        team_module,
        "cancel_all_team_stream_tasks_across_managers",
        helper,
    )
    runtime = AgentRuntime(agent_manager=FakeAgentManager(), initializer=AsyncMock())

    await runtime.cancel_all_team_stream_tasks(
        reason="cancel all teams",
        exclude_session_ids=None,
    )

    helper.assert_awaited_once_with(
        reason="cancel all teams",
        exclude_session_ids=None,
    )


@pytest.mark.asyncio
async def test_cancel_all_team_stream_tasks_materializes_generator_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness import team as team_module

    helper = AsyncMock()
    monkeypatch.setattr(
        team_module,
        "cancel_all_team_stream_tasks_across_managers",
        helper,
    )
    runtime = AgentRuntime(agent_manager=FakeAgentManager(), initializer=AsyncMock())
    consumed: list[str] = []

    def exclusions() -> Iterable[str]:
        for session_id in ("heartbeat-1", "heartbeat-2"):
            consumed.append(session_id)
            yield session_id

    excluded = exclusions()
    await runtime.cancel_all_team_stream_tasks(
        exclude_session_ids=excluded,
    )

    helper.assert_awaited_once_with(
        reason="[runtime cancel all team streams] ",
        exclude_session_ids={"heartbeat-1", "heartbeat-2"},
    )
    assert consumed == ["heartbeat-1", "heartbeat-2"]
    assert list(excluded) == []


@pytest.mark.asyncio
async def test_cancel_all_team_stream_tasks_rejects_closed_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness import team as team_module

    helper = AsyncMock()
    monkeypatch.setattr(
        team_module,
        "cancel_all_team_stream_tasks_across_managers",
        helper,
    )
    runtime = AgentRuntime(agent_manager=FakeAgentManager(), initializer=AsyncMock())
    await runtime.close()

    with pytest.raises(RuntimeStateError, match="runtime is already closed"):
        await runtime.cancel_all_team_stream_tasks()

    helper.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_all_team_stream_tasks_propagates_helper_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness import team as team_module

    helper = AsyncMock(side_effect=RuntimeError("team cancel failed"))
    monkeypatch.setattr(
        team_module,
        "cancel_all_team_stream_tasks_across_managers",
        helper,
    )
    runtime = AgentRuntime(agent_manager=FakeAgentManager(), initializer=AsyncMock())

    with pytest.raises(RuntimeError, match="team cancel failed"):
        await runtime.cancel_all_team_stream_tasks(
            reason="disconnect",
            exclude_session_ids=("heartbeat-session",),
        )

    helper.assert_awaited_once_with(
        reason="disconnect",
        exclude_session_ids={"heartbeat-session"},
    )


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
        async def prepare_chat_turn(
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
async def test_interrupt_resume_does_not_wait_for_existing_user_admission() -> None:
    admission = FakeAdmissionController(user_active=True)
    manager = FakeAgentManager()

    class InterruptRuntime(AgentRuntime):
        async def prepare_chat_turn(
            self,
            request: AgentRequest,
            channel_id: str,
            *,
            sync_metadata: bool = True,
        ) -> tuple[str, str | None, object]:
            return "agent", "normal", manager.agent

    runtime = InterruptRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
        admission_controller=admission,
    )
    session_id = "ask-user-session"

    request = AgentRequest(
        request_id="answer-dispatch",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "",
            "request_id": "call_ask_user",
            "answers": [{"question": "选择方案", "selected_options": ["A"]}],
            "source": "ask_user_interrupt",
            "mode": "agent",
            "work_mode": "work",
        },
    )

    await asyncio.wait_for(runtime.invoke(request, trigger_hook=False), timeout=0.2)
    assert admission.calls == []


@pytest.mark.asyncio
async def test_stale_interrupt_resume_retains_normal_admission() -> None:
    admission = FakeAdmissionController(user_active=False)
    manager = FakeAgentManager()

    class InterruptRuntime(AgentRuntime):
        async def prepare_chat_turn(
            self,
            request: AgentRequest,
            channel_id: str,
            *,
            sync_metadata: bool = True,
        ) -> tuple[str, str | None, object]:
            return "agent", "normal", manager.agent

    runtime = InterruptRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
        admission_controller=admission,
    )
    request = AgentRequest(
        request_id="stale-answer-dispatch",
        channel_id="web",
        session_id="stale-answer-session",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "",
            "request_id": "call_stale_ask_user",
            "answers": [{"question": "选择方案", "selected_options": ["A"]}],
            "source": "ask_user_interrupt",
            "mode": "agent",
            "work_mode": "work",
        },
    )

    await runtime.invoke(request, trigger_hook=False)
    assert admission.calls == ["begin", "end"]


@pytest.mark.asyncio
async def test_interrupt_resume_stream_does_not_wait_for_existing_user_admission() -> None:
    admission = FakeAdmissionController(user_active=True)
    manager = FakeAgentManager()

    class InterruptRuntime(AgentRuntime):
        async def prepare_chat_turn(
            self,
            request: AgentRequest,
            channel_id: str,
            *,
            sync_metadata: bool = True,
        ) -> tuple[str, str | None, object]:
            return "agent", "normal", manager.agent

    runtime = InterruptRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
        admission_controller=admission,
    )
    session_id = "ask-user-stream-session"

    request = AgentRequest(
        request_id="answer-stream-dispatch",
        channel_id="tui",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        params={
            "query": "",
            "request_id": "call_ask_user_stream",
            "answers": [{"question": "选择方案", "selected_options": ["A"]}],
            "source": "ask_user_interrupt",
            "mode": "agent",
            "work_mode": "work",
        },
    )

    events = [event async for event in runtime.stream(request, trigger_hook=False)]
    assert [event.event_type for event in events] == ["chat.delta", "chat.final"]
    assert admission.calls == []


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
async def test_agent_server_session_fork_allocates_target_through_runtime_public_api(
    monkeypatch,
) -> None:
    from jiuwenswarm.agents.harness.common import session_ops_service
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    manager = SimpleNamespace(get_agent_nowait=MagicMock(return_value=None))
    runtime = SimpleNamespace(
        agent_manager=manager,
        start=AsyncMock(),
        create_or_resume_session=AsyncMock(return_value="fork-target"),
    )
    fork_session = MagicMock(
        return_value={
            "source_session_id": "fork-source",
            "target_session_id": "fork-target",
            "title": "Forked session",
        }
    )
    copy_session_state = AsyncMock()
    monkeypatch.setattr(session_ops_service, "fork_session", fork_session)
    monkeypatch.setattr(
        session_ops_service,
        "copy_session_state",
        copy_session_state,
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
    runtime.create_or_resume_session.assert_awaited_once_with(
        channel_id="tui",
        session_id=None,
    )
    fork_session.assert_called_once_with(
        source_session_id="fork-source",
        target_session_id="fork-target",
        title="Forked session",
        channel_id="tui",
    )
    copy_session_state.assert_awaited_once()
    ws.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_server_session_fork_waits_for_runtime_with_explicit_target(
    monkeypatch,
) -> None:
    from jiuwenswarm.agents.harness.common import session_ops_service
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    order: list[str] = []
    manager = SimpleNamespace(get_agent_nowait=MagicMock(return_value=None))

    async def start_runtime() -> None:
        order.append("runtime.start")

    runtime = SimpleNamespace(
        agent_manager=manager,
        start=AsyncMock(side_effect=start_runtime),
        create_or_resume_session=AsyncMock(),
    )

    def fork_session(**kwargs: object) -> dict[str, str]:
        order.append("fork.filesystem")
        return {
            "source_session_id": str(kwargs["source_session_id"]),
            "target_session_id": str(kwargs["target_session_id"]),
            "title": str(kwargs["title"]),
        }

    async def copy_session_state(**kwargs: object) -> None:
        order.append("fork.checkpointer")

    monkeypatch.setattr(session_ops_service, "fork_session", fork_session)
    monkeypatch.setattr(
        session_ops_service,
        "copy_session_state",
        copy_session_state,
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
    runtime.create_or_resume_session.assert_not_awaited()
    assert order == ["runtime.start", "fork.filesystem", "fork.checkpointer"]
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
            runtime_package.get_current_runtime,
            runtime_package.get_current_agent_manager,
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
    assert runtime_package.get_runtime_context() is None


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
                    runtime_package.get_current_runtime(),
                    runtime_package.get_current_agent_manager(),
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
    assert runtime_package.get_runtime_context() is None


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
async def test_agent_server_stop_replaces_closed_one_shot_runtime(monkeypatch) -> None:
    from jiuwenswarm.server import agent_ws_server as server_module
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_product_hooks

    server = AgentWebSocketServer()
    previous_runtime = server.get_runtime()
    previous_plan_controller = previous_runtime.plan_controller
    previous_plan_controller.active_sessions.add("session-before-stop")
    previous_plan_controller.exited_sessions.add("session-before-stop")
    previous_lock = previous_plan_controller.lock_for("session-before-stop")
    previous_runtime.close = AsyncMock()
    monkeypatch.setattr(
        kv_cache_product_hooks,
        "cancel_pending_tasks",
        AsyncMock(),
    )

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
async def test_agent_server_start_restores_remote_service_after_stop(
    monkeypatch,
) -> None:
    from jiuwenswarm.server import agent_ws_server as server_module
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_product_hooks

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
    cancel_pending_tasks = AsyncMock()
    monkeypatch.setattr(
        kv_cache_product_hooks,
        "cancel_pending_tasks",
        cancel_pending_tasks,
    )

    server = AgentWebSocketServer()
    server._bootstrap_internal_jiuwenbox = AsyncMock()
    monkeypatch.setattr(server._jiuwenbox_runner, "stop", AsyncMock())
    first_runtime = server.get_runtime()
    first_manager = server.get_agent_manager()
    first_runtime.start = AsyncMock()
    first_runtime.close = AsyncMock()

    await server.start()
    await server._checkpointer_warmup_task
    await server.stop()

    recovered_runtime = server.get_runtime()
    recovered_manager = server.get_agent_manager()
    recovered_runtime.start = AsyncMock()
    recovered_runtime.close = AsyncMock()

    await server.start()
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
    assert cancel_pending_tasks.await_count == 2
