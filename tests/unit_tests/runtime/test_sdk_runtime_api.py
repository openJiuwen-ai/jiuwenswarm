# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, sentinel

import pytest

from jiuwenswarm.common.schema.agent import (
    AgentRequest,
    AgentResponse,
    AgentResponseChunk,
)
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.channels.process_cli.client import (
    InProcessRuntimeClient,
    ProcessCliBoundaryError,
)
from jiuwenswarm.runtime.agent_definition import (
    RuntimeAgentDefinition,
    RuntimeAgentDefinitionError,
    RuntimeAgentExecution,
)
from jiuwenswarm.runtime.evolution import SKILL_EVOLUTION_APPROVAL_SCHEMA
from jiuwenswarm.runtime.interaction import (
    InteractionAnswerError,
    InteractionAnswerInput,
)
from jiuwenswarm.runtime.model_catalog import ModelCatalogError
from jiuwenswarm.runtime.permission_catalog import PermissionSnapshotInput
from jiuwenswarm.runtime.service import AgentRuntime, RuntimeStateError
from jiuwenswarm.runtime.session_catalog import (
    SessionCatalogError,
    SessionGetInput,
    SessionListInput,
)
from jiuwenswarm.runtime.session_provisioner import (
    SessionCreateInput,
    SessionCreateResult,
    SessionDescriptor,
    SessionForkInput,
    SessionProvisionCommitTiming,
    SessionSwitchInput,
)


class _Agent:
    async def process_message(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={"event_type": "chat.final", "content": "done"},
        )

    async def process_message_stream(self, request: AgentRequest):
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={"event_type": "chat.final", "content": "done"},
            is_complete=True,
        )


class _Manager:
    def __init__(self) -> None:
        self.agent = _Agent()
        self.calls: list[dict[str, object]] = []

    async def wait_for_session_prewarm(self, _session_id: str | None) -> None:
        return None

    async def get_agent_for_request(
        self,
        request: AgentRequest,
        **kwargs: object,
    ) -> _Agent:
        self.calls.append({"request": request, **kwargs})
        admit = kwargs.get("admit_request")
        if callable(admit):
            admit()
        return self.agent

    async def begin_foreground_chat(self) -> None:
        return None

    async def end_foreground_chat(self) -> None:
        return None

    async def cancel_all_inflight_work(self, _reason: str) -> None:
        return None

    async def cleanup(self) -> None:
        return None

    async def cleanup_session_runtime(
        self,
        *,
        channel_id: str,
        session_id: str,
    ) -> bool:
        self.calls.append(
            {"cleanup_channel_id": channel_id, "cleanup_session_id": session_id}
        )
        return True

    async def create_session(
        self,
        *,
        channel_id: str,
        session_id: str | None = None,
    ) -> str:
        resolved = session_id or f"{channel_id}-generated"
        self.calls.append(
            {"create_channel_id": channel_id, "create_session_id": session_id}
        )
        return resolved


class _PlanController:
    async def ensure_state(self, *_args: object) -> SimpleNamespace:
        return SimpleNamespace(events=[])

    async def check_post_process_exit(self, *_args: object) -> list[object]:
        return []

    def reset_session(self, _session_id: str) -> None:
        return None


def _request(
    *,
    streaming: bool,
    session_id: str | None = None,
) -> AgentRequest:
    return AgentRequest(
        request_id="sdk-request",
        channel_id="process_cli",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "hello",
            "mode": "agent.code.normal",
            "work_mode": "code",
        },
        is_stream=streaming,
    )


def _definition() -> RuntimeAgentDefinition:
    return RuntimeAgentDefinition(
        name="sdk_agent",
        instructions="Answer concisely.",
        skills=("review",),
        max_iterations=8,
    )


@pytest.mark.asyncio
async def test_invoke_agent_reuses_manager_path_and_passes_definition() -> None:
    manager = _Manager()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )

    events = await runtime.invoke_agent(
        _request(streaming=False),
        _definition(),
        trigger_hook=False,
    )

    assert [event.event_type for event in events] == ["chat.final"]
    assert len(manager.calls) == 1
    call = manager.calls[0]
    assert call["agent_definition"] == _definition().to_dict()
    assert call["agent_definition_fingerprint"] == _definition().fingerprint
    assert call["mode"] == "code"
    bound = call["request"]
    assert isinstance(bound, AgentRequest)
    assert bound.params["mode"] == "agent.code.normal"
    assert bound.params["work_mode"] == "code"
    await runtime.close()


@pytest.mark.asyncio
async def test_stream_agent_uses_same_definition_handoff() -> None:
    manager = _Manager()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )

    events = [
        event
        async for event in runtime.stream_agent(
            _request(streaming=True),
            _definition(),
            trigger_hook=False,
        )
    ]

    assert [event.event_type for event in events] == ["chat.final"]
    assert manager.calls[0]["agent_definition_fingerprint"] == (
        _definition().fingerprint
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_model_catalog_matches_adapter_buildability_and_global_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.common import config as config_module
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    def entry(name: str) -> dict[str, object]:
        return {
            "model_client_config": {
                "model_name": name,
                "client_provider": "OpenAI",
            },
            "model_config_obj": {},
        }

    entries = [entry("usable-a"), entry("broken"), entry("usable-b")]
    monkeypatch.setattr(config_module, "get_default_models", lambda: entries)

    def build_model(client_config: dict, _model_config: dict) -> object:
        if client_config["model_name"] == "broken":
            raise ValueError("not constructible")
        return object()

    monkeypatch.setattr(interface_deep, "build_model_from_entry", build_model)
    runtime = AgentRuntime(
        agent_manager=_Manager(),
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )
    await runtime.start()
    try:
        catalog = runtime.list_model_capabilities()
        assert [item.selection_key for item in catalog.models] == [
            "usable-a#0",
            "usable-b#2",
        ]
        with pytest.raises(RuntimeAgentDefinitionError) as caught:
            runtime.validate_agent_definition(
                RuntimeAgentDefinition(
                    name="model_agent",
                    instructions="Use the requested model.",
                    model="broken#1",
                ),
                mode="agent.code.normal",
            )
        assert caught.value.code == "AGENT_DEFINITION_MODEL_NOT_FOUND"
    finally:
        await runtime.close()


def test_interaction_answer_input_preserves_both_existing_protocols() -> None:
    common = {
        "request_id": "answer-1",
        "channel_id": "process_cli",
        "session_id": "session-1",
        "interaction_id": "call-1",
        "answers": ({"selected_options": ["allow"]},),
        "mode": "agent.code.normal",
        "work_mode": "code",
    }
    permission_answer = InteractionAnswerInput(
        **common,
        source="permission_interrupt",
    )
    ordinary_answer = InteractionAnswerInput(**common)

    permission_request = permission_answer.to_agent_request()
    ordinary_request = ordinary_answer.to_agent_request()
    assert permission_request.req_method is ReqMethod.CHAT_SEND
    assert permission_request.is_stream is True
    assert permission_request.params["query"] == ""
    assert ordinary_request.req_method is ReqMethod.CHAT_ANSWER
    assert ordinary_request.is_stream is False
    assert ordinary_request.params["query"] is None
    assert "approval_schema" not in ordinary_request.params
    assert "evolution_meta" not in ordinary_request.params


@pytest.mark.parametrize(
    ("interaction_id", "source", "approval_schema", "evolution_meta"),
    [
        ("call_legacy", "skill_evolution_approval", "", None),
        (
            "legacy-approval",
            "skill_evolution_approval",
            "",
            {"event_kind": "approval", "approval_transport": "interrupt"},
        ),
        (
            "schema-approval",
            "",
            SKILL_EVOLUTION_APPROVAL_SCHEMA,
            {"approval_transport": "interrupt"},
        ),
        (
            "metadata-approval",
            "",
            "",
            {"event_kind": "approval", "approval_transport": "interrupt"},
        ),
    ],
)
def test_legacy_evolution_interrupt_answers_resume_the_suspended_turn(
    interaction_id: str,
    source: str,
    approval_schema: str,
    evolution_meta: dict[str, str] | None,
) -> None:
    answer = InteractionAnswerInput(
        request_id="answer-legacy",
        channel_id="process_cli",
        session_id="session-legacy",
        interaction_id=interaction_id,
        answers=({"selected_options": ["allow_once"]},),
        source=source,
        approval_schema=approval_schema,
        evolution_meta=evolution_meta,
    )

    request = answer.to_agent_request()

    assert answer.resumes_interrupted_turn is True
    assert request.req_method is ReqMethod.CHAT_SEND
    assert request.is_stream is True
    assert request.params["query"] == ""
    if approval_schema:
        assert request.params["approval_schema"] == approval_schema
    if evolution_meta is not None:
        assert request.params["evolution_meta"] == evolution_meta


def test_regular_evolution_and_ordinary_answers_remain_chat_answer() -> None:
    regular_evolution = InteractionAnswerInput(
        request_id="answer-regular-evolution",
        channel_id="process_cli",
        session_id="session-1",
        interaction_id="skill_evolve_1",
        answers=({"selected_options": ["allow_once"]},),
        source="skill_evolution_approval",
        approval_schema=SKILL_EVOLUTION_APPROVAL_SCHEMA,
        evolution_meta={"event_kind": "approval", "approval_kind": "evolve"},
    )
    ordinary = InteractionAnswerInput(
        request_id="answer-ordinary",
        channel_id="process_cli",
        session_id="session-1",
        interaction_id="ordinary-question",
        answers=({"selected_options": ["yes"]},),
        evolution_meta={"approval_transport": "interrupt"},
    )

    regular_request = regular_evolution.to_agent_request()
    ordinary_request = ordinary.to_agent_request()

    assert regular_request.req_method is ReqMethod.CHAT_ANSWER
    assert regular_request.is_stream is False
    assert regular_request.params["query"] is None
    assert regular_request.params["approval_schema"] == (
        SKILL_EVOLUTION_APPROVAL_SCHEMA
    )
    assert ordinary_request.req_method is ReqMethod.CHAT_ANSWER
    assert ordinary_request.is_stream is False
    assert ordinary_request.params["query"] is None


def test_evolution_metadata_is_copied_before_mapping_to_agent_request() -> None:
    metadata = {
        "event_kind": "approval",
        "approval_transport": "interrupt",
    }
    answer = InteractionAnswerInput(
        request_id="answer-copy",
        channel_id="process_cli",
        session_id="session-1",
        interaction_id="legacy-approval",
        answers=({"selected_options": ["allow_once"]},),
        source="skill_evolution_approval",
        evolution_meta=metadata,
    )
    metadata["approval_transport"] = "changed-after-parse"

    first_request = answer.to_agent_request()
    first_request.params["evolution_meta"]["approval_transport"] = "changed-request"
    second_request = answer.to_agent_request()

    assert second_request.params["evolution_meta"]["approval_transport"] == (
        "interrupt"
    )


@pytest.mark.parametrize(
    ("field", "value", "expected_message"),
    [
        ("approval_schema", 7, "approval_schema must be a string"),
        ("evolution_meta", [], "evolution_meta must be an object"),
        ("evolution_meta", {1: "approval"}, "evolution_meta keys must be strings"),
    ],
)
def test_legacy_evolution_metadata_rejects_malformed_values(
    field: str,
    value: object,
    expected_message: str,
) -> None:
    overrides: dict[str, Any] = {field: value}

    with pytest.raises(InteractionAnswerError, match=expected_message) as caught:
        InteractionAnswerInput(
            request_id="answer-invalid",
            channel_id="process_cli",
            session_id="session-1",
            interaction_id="call-invalid",
            answers=({"selected_options": ["allow_once"]},),
            source="skill_evolution_approval",
            **overrides,
        )

    assert caught.value.code == "INTERACTION_BAD_REQUEST"
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_typed_interaction_answer_routes_interrupt_to_stream() -> None:
    runtime = AgentRuntime(
        agent_manager=_Manager(),
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )
    expected = SimpleNamespace(event_type="chat.final")

    async def stream(_request: AgentRequest, **_kwargs: object):
        yield expected

    runtime.stream = stream  # type: ignore[method-assign]
    runtime.get_session = Mock(return_value=sentinel.owned_session)  # type: ignore[method-assign]
    answer = InteractionAnswerInput(
        request_id="answer-1",
        channel_id="process_cli",
        session_id="session-1",
        interaction_id="call-1",
        answers=({"selected_options": ["allow"]},),
        source="permission_interrupt",
    )

    assert await runtime.answer_interaction_input(answer) == [expected]


@pytest.mark.asyncio
async def test_typed_interaction_answer_routes_ordinary_to_chat_answer() -> None:
    runtime = AgentRuntime(
        agent_manager=_Manager(),
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )
    expected = [SimpleNamespace(event_type="chat.final")]
    runtime.answer_interaction = AsyncMock(return_value=expected)
    runtime.get_session = Mock(return_value=sentinel.owned_session)  # type: ignore[method-assign]
    answer = InteractionAnswerInput(
        request_id="answer-1",
        channel_id="process_cli",
        session_id="session-1",
        interaction_id="call-1",
        answers=({"selected_options": ["yes"]},),
    )

    assert await runtime.answer_interaction_input(answer) == expected
    request = runtime.answer_interaction.await_args.args[0]
    assert request.req_method is ReqMethod.CHAT_ANSWER


@pytest.mark.asyncio
async def test_process_client_delegates_typed_answer_and_owns_lifecycle() -> None:
    calls: list[object] = []
    expected = [SimpleNamespace(event_type="chat.final")]

    class _ClientRuntime:
        async def start(self) -> None:
            calls.append("start")

        async def answer_interaction_input(
            self,
            answer: InteractionAnswerInput,
        ) -> list[SimpleNamespace]:
            calls.append(("answer", answer))
            return expected

        async def cancel_all_team_stream_tasks(self, *, reason: str) -> None:
            calls.append(("cancel_team", reason))

        async def close(self) -> None:
            calls.append("close")

    runtime = _ClientRuntime()
    client = InProcessRuntimeClient(runtime)  # type: ignore[arg-type]
    answer = InteractionAnswerInput(
        request_id="answer-client",
        channel_id="process_cli",
        session_id="session-1",
        interaction_id="call-client",
        answers=({"selected_options": ["allow_once"]},),
        source="skill_evolution_approval",
        approval_schema=SKILL_EVOLUTION_APPROVAL_SCHEMA,
        evolution_meta={"approval_transport": "interrupt"},
    )

    await client.start()
    result = await client.answer_interaction_input(answer)
    await client.close()

    assert result is expected
    assert calls == [
        "start",
        ("answer", answer),
        ("cancel_team", "[process CLI close] "),
        "close",
    ]


@pytest.mark.asyncio
async def test_runtime_catalogs_require_one_started_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.common import config
    from jiuwenswarm.runtime import permission_catalog, session_catalog

    monkeypatch.setattr(config, "get_default_models", Mock(return_value=[]))
    monkeypatch.setattr(
        session_catalog,
        "_read_session_metadata",
        Mock(return_value={}),
    )
    monkeypatch.setattr(
        session_catalog,
        "_collect_session_metadata",
        Mock(return_value=()),
    )
    monkeypatch.setattr(
        permission_catalog,
        "_capture_permission_layers",
        Mock(return_value=({}, {}, {}, {})),
    )
    runtime = AgentRuntime(
        agent_manager=_Manager(),
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )
    operations = (
        runtime.list_model_capabilities,
        runtime.list_mode_capabilities,
        lambda: runtime.get_session(
            SessionGetInput(channel_id="process_cli", session_id="session_1")
        ),
        lambda: runtime.list_sessions(SessionListInput(channel_id="process_cli")),
        lambda: runtime.get_permission_snapshot(PermissionSnapshotInput()),
    )

    for operation in operations:
        with pytest.raises(RuntimeStateError, match="runtime is not started"):
            operation()

    await runtime.start()
    assert runtime.list_model_capabilities().models == ()
    assert len(runtime.list_mode_capabilities().modes) == 4
    assert (
        runtime.get_session(
            SessionGetInput(channel_id="process_cli", session_id="session_1")
        )
        is None
    )
    assert runtime.list_sessions(SessionListInput(channel_id="process_cli")).total == 0
    assert runtime.get_permission_snapshot(PermissionSnapshotInput()).scope == "host"
    await runtime.close()

    for operation in operations:
        with pytest.raises(RuntimeStateError, match="runtime is already closed"):
            operation()


@pytest.mark.asyncio
async def test_model_catalog_hides_configuration_loader_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.common import config

    def fail_to_load_models() -> list[object]:
        raise OSError("private-path?token=must-not-leak")

    monkeypatch.setattr(config, "get_default_models", fail_to_load_models)
    runtime = AgentRuntime(
        agent_manager=_Manager(),
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )
    await runtime.start()

    with pytest.raises(ModelCatalogError) as caught:
        runtime.list_model_capabilities()

    assert caught.value.code == "MODEL_CATALOG_UNAVAILABLE"
    assert caught.value.retryable is True
    assert str(caught.value) == "model catalog is unavailable"
    assert "must-not-leak" not in str(caught.value)
    assert caught.value.__cause__ is None
    await runtime.close()


@pytest.mark.asyncio
async def test_process_client_catalogs_share_lifecycle_and_fix_channel_scope() -> None:
    runtime = SimpleNamespace(
        start=AsyncMock(),
        list_model_capabilities=Mock(return_value=sentinel.models),
        list_mode_capabilities=Mock(return_value=sentinel.modes),
        get_session=Mock(return_value=sentinel.session),
        list_sessions=Mock(return_value=sentinel.sessions),
        get_permission_snapshot=Mock(return_value=sentinel.permissions),
        cancel_all_team_stream_tasks=AsyncMock(),
        close=AsyncMock(),
    )
    client = InProcessRuntimeClient(runtime=runtime)  # type: ignore[arg-type]
    session_get = SessionGetInput(
        channel_id="process_cli",
        session_id="session_1",
    )
    session_list = SessionListInput(channel_id="process_cli")
    permissions = PermissionSnapshotInput(
        channel_id="process_cli",
        session_id="session_1",
    )

    await client.start()
    assert client.list_model_capabilities() is sentinel.models
    assert client.list_mode_capabilities() is sentinel.modes
    assert client.get_session(session_get) is sentinel.session
    assert client.list_sessions(session_list) is sentinel.sessions
    assert client.get_permission_snapshot(permissions) is sentinel.permissions
    await client.close()

    runtime.start.assert_awaited_once_with()
    runtime.get_session.assert_called_once_with(session_get)
    runtime.list_sessions.assert_called_once_with(session_list)
    runtime.get_permission_snapshot.assert_called_once_with(permissions)
    runtime.cancel_all_team_stream_tasks.assert_awaited_once_with(
        reason="[process CLI close] ",
    )
    runtime.close.assert_awaited_once_with()

    for ambiguous_channel in ("PROCESS_CLI", " process_cli "):
        with pytest.raises(
            ProcessCliBoundaryError,
            match="channel_id='process_cli'",
        ) as caught:
            client.list_sessions(SessionListInput(channel_id=ambiguous_channel))
        assert caught.value.code == "PROCESS_CLI_CHANNEL_INVALID"
        assert caught.value.retryable is False

    foreign_get = SessionGetInput(channel_id="tui", session_id="session_1")
    foreign_list = SessionListInput(channel_id="web")
    foreign_permissions = PermissionSnapshotInput(
        channel_id="tui",
        session_id="session_1",
    )
    for operation in (
        lambda: client.get_session(foreign_get),
        lambda: client.list_sessions(foreign_list),
        lambda: client.get_permission_snapshot(foreign_permissions),
    ):
        with pytest.raises(
            ProcessCliBoundaryError,
            match="channel_id='process_cli'",
        ) as caught:
            operation()
        assert caught.value.code == "PROCESS_CLI_CHANNEL_INVALID"
        assert caught.value.retryable is False

    runtime.get_session.assert_called_once_with(session_get)
    runtime.list_sessions.assert_called_once_with(session_list)
    runtime.get_permission_snapshot.assert_called_once_with(permissions)


@pytest.mark.asyncio
async def test_process_client_rejects_foreign_channel_for_new_execution_apis() -> None:
    runtime = SimpleNamespace(
        invoke_agent=AsyncMock(),
        stream_agent=Mock(),
        answer_interaction_input=AsyncMock(),
    )
    client = InProcessRuntimeClient(runtime=runtime)  # type: ignore[arg-type]
    request = AgentRequest(
        request_id="foreign-request",
        channel_id="tui",
        session_id="foreign-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello", "mode": "agent.code.normal"},
    )
    answer = InteractionAnswerInput(
        request_id="foreign-answer",
        channel_id="web",
        session_id="foreign-session",
        interaction_id="call-foreign",
        answers=({"selected_options": ["allow_once"]},),
        source="permission_interrupt",
    )

    with pytest.raises(
        ProcessCliBoundaryError,
        match="channel_id='process_cli'",
    ):
        await client.invoke_agent(request, _definition())
    with pytest.raises(
        ProcessCliBoundaryError,
        match="channel_id='process_cli'",
    ):
        client.stream_agent(request, _definition())
    with pytest.raises(
        ProcessCliBoundaryError,
        match="channel_id='process_cli'",
    ):
        await client.answer_interaction_input(answer)

    runtime.invoke_agent.assert_not_awaited()
    runtime.stream_agent.assert_not_called()
    runtime.answer_interaction_input.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_client_rejects_foreign_channel_across_legacy_surface() -> None:
    runtime = SimpleNamespace(
        create_or_resume_session=AsyncMock(),
        describe_session=AsyncMock(
            return_value=SessionDescriptor(
                session_id="foreign-session",
                channel_id="tui",
                mode="agent.code.normal",
                work_mode="code",
            )
        ),
        prepare_session_create=AsyncMock(),
        prepare_session_switch=AsyncMock(),
        prepare_session_fork=AsyncMock(),
        commit_session_provision=AsyncMock(),
        abort_session_provision=AsyncMock(),
        delete_session=AsyncMock(),
        stream=Mock(),
        invoke=AsyncMock(),
        answer_interaction=AsyncMock(),
        cancel_request=AsyncMock(),
        cleanup_session=AsyncMock(),
    )
    client = InProcessRuntimeClient(runtime=runtime)  # type: ignore[arg-type]
    foreign_request = AgentRequest(
        request_id="foreign-legacy-request",
        channel_id="tui",
        session_id="foreign-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello", "mode": "agent.code.normal"},
    )
    prepared = SimpleNamespace(
        result=SessionCreateResult(
            channel_id="tui",
            session_id="foreign-session",
            project_id="",
            project_dir="",
            work_mode="code",
            persist_session=False,
            prewarm_hit=False,
            prewarm_status="disabled",
            created=True,
            canonical_mode="agent.code.normal",
        )
    )
    async_calls = (
        client.create_or_resume_session(
            channel_id="tui",
            session_id="foreign-session",
        ),
        client.prepare_session_create(SessionCreateInput(channel_id="tui")),
        client.prepare_session_switch(
            SessionSwitchInput(
                channel_id="tui",
                target_session_id="foreign-session",
            )
        ),
        client.prepare_session_fork(
            SessionForkInput(
                channel_id="tui",
                source_session_id="foreign-session",
            )
        ),
        client.commit_session_provision(
            prepared,
            timing=SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY,
        ),
        client.abort_session_provision(prepared),
        client.delete_session(channel_id="tui", session_id="foreign-session"),
        client.invoke(foreign_request),
        client.answer_interaction(foreign_request),
        client.cancel(foreign_request),
        client.cleanup_session(channel_id="tui", session_id="foreign-session"),
    )
    for operation in async_calls:
        with pytest.raises(ProcessCliBoundaryError):
            await operation
    with pytest.raises(ProcessCliBoundaryError):
        client.stream(foreign_request)
    assert await client.describe_session(session_id="foreign-session") is None

    runtime.create_or_resume_session.assert_not_awaited()
    runtime.prepare_session_create.assert_not_awaited()
    runtime.prepare_session_switch.assert_not_awaited()
    runtime.prepare_session_fork.assert_not_awaited()
    runtime.commit_session_provision.assert_not_awaited()
    runtime.abort_session_provision.assert_not_awaited()
    runtime.delete_session.assert_not_awaited()
    runtime.stream.assert_not_called()
    runtime.invoke.assert_not_awaited()
    runtime.answer_interaction.assert_not_awaited()
    runtime.cancel_request.assert_not_awaited()
    runtime.cleanup_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_resume_rejects_foreign_persisted_session_before_register() -> None:
    manager = _Manager()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )
    await runtime.start()
    runtime.describe_session = AsyncMock(  # type: ignore[method-assign]
        return_value=SessionDescriptor(
            session_id="known-session",
            channel_id="tui",
            mode="agent.code.normal",
            work_mode="code",
        )
    )

    with pytest.raises(SessionCatalogError) as caught:
        await runtime.create_or_resume_session(
            channel_id="process_cli",
            session_id="known-session",
        )

    assert caught.value.code == "NOT_FOUND"
    assert str(caught.value) == "session not found"
    assert not any("create_channel_id" in call for call in manager.calls)
    assert runtime._session_coordinator.snapshot_session("known-session") is None
    await runtime.close()


@pytest.mark.asyncio
async def test_custom_agent_rejects_active_same_channel_team_session() -> None:
    manager = _Manager()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )
    await runtime.start()
    runtime.describe_session = AsyncMock(  # type: ignore[method-assign]
        return_value=SessionDescriptor(
            session_id="process-cli-team",
            channel_id="process_cli",
            mode="team.code.normal",
            work_mode="code",
        )
    )
    runtime.get_session = Mock(return_value=None)  # type: ignore[method-assign]
    await runtime.create_or_resume_session(
        channel_id="process_cli",
        session_id="process-cli-team",
    )

    with pytest.raises(SessionCatalogError) as caught:
        await runtime.invoke_agent(
            _request(streaming=False, session_id="process-cli-team"),
            _definition(),
            trigger_hook=False,
        )

    assert caught.value.code == "NOT_FOUND"
    assert str(caught.value) == "session not found"
    assert runtime._session_coordinator.snapshot_session("process-cli-team") is not None
    assert runtime.describe_session.await_count == 2
    await runtime.close()


@pytest.mark.asyncio
async def test_custom_agent_owner_is_reused_for_ordinary_interaction_answer() -> None:
    runtime = AgentRuntime(
        agent_manager=_Manager(),
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )
    runtime.get_session = Mock(return_value=sentinel.owned_session)  # type: ignore[method-assign]
    runtime.invoke = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            [SimpleNamespace(event_type="chat.interaction")],
            [SimpleNamespace(event_type="chat.final")],
        ]
    )
    request = _request(streaming=False, session_id="sdk-session")

    await runtime.invoke_agent(request, _definition(), trigger_hook=False)
    events = await runtime.answer_interaction_input(
        InteractionAnswerInput(
            request_id="sdk-answer",
            channel_id="process_cli",
            session_id="sdk-session",
            interaction_id="interaction-1",
            answers=({"selected_options": ["yes"]},),
        ),
        trigger_hook=False,
    )

    assert [event.event_type for event in events] == ["chat.final"]
    answer_call = runtime.invoke.await_args_list[1]
    owner = answer_call.kwargs["_agent_execution"]
    assert owner.definition == _definition()
    assert owner.fingerprint == _definition().fingerprint
    await runtime.close()
    assert runtime._agent_execution_owners == {}


@pytest.mark.asyncio
async def test_custom_agent_owner_is_reused_for_permission_interrupt() -> None:
    runtime = AgentRuntime(
        agent_manager=_Manager(),
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )
    runtime.get_session = Mock(return_value=sentinel.owned_session)  # type: ignore[method-assign]
    runtime.invoke = AsyncMock(return_value=[])  # type: ignore[method-assign]
    request = _request(streaming=False, session_id="sdk-session")
    await runtime.invoke_agent(request, _definition(), trigger_hook=False)
    captured: dict[str, object] = {}

    async def capture_stream(
        _request: AgentRequest,
        **kwargs: object,
    ):
        captured.update(kwargs)
        yield SimpleNamespace(event_type="chat.final")

    runtime.stream = capture_stream  # type: ignore[method-assign]
    events = await runtime.answer_interaction_input(
        InteractionAnswerInput(
            request_id="permission-answer",
            channel_id="process_cli",
            session_id="sdk-session",
            interaction_id="permission-call",
            answers=({"selected_options": ["allow_once"]},),
            source="permission_interrupt",
        ),
        trigger_hook=False,
    )

    assert [event.event_type for event in events] == ["chat.final"]
    owner = captured["_agent_execution"]
    assert isinstance(owner, RuntimeAgentExecution)
    assert owner.fingerprint == _definition().fingerprint
    await runtime.close()


@pytest.mark.asyncio
async def test_custom_agent_owner_rejects_definition_change_in_one_lifecycle() -> None:
    runtime = AgentRuntime(
        agent_manager=_Manager(),
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )
    runtime.get_session = Mock(return_value=sentinel.owned_session)  # type: ignore[method-assign]
    runtime.invoke = AsyncMock(return_value=[])  # type: ignore[method-assign]
    request = _request(streaming=False, session_id="sdk-session")

    await runtime.invoke_agent(request, _definition(), trigger_hook=False)
    changed = RuntimeAgentDefinition(
        name="sdk_agent",
        instructions="Use different instructions.",
    )
    with pytest.raises(RuntimeAgentDefinitionError) as caught:
        await runtime.invoke_agent(request, changed, trigger_hook=False)

    assert caught.value.code == "AGENT_DEFINITION_SESSION_CONFLICT"
    assert caught.value.field == "agent"
    assert runtime.invoke.await_count == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_invalid_custom_agent_request_does_not_claim_session_owner() -> None:
    runtime = AgentRuntime(
        agent_manager=_Manager(),
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )
    runtime.get_session = Mock(return_value=sentinel.owned_session)  # type: ignore[method-assign]
    runtime.invoke = AsyncMock(return_value=[])  # type: ignore[method-assign]
    invalid_request = AgentRequest(
        request_id="invalid-custom-agent-request",
        channel_id="process_cli",
        session_id="sdk-session",
        req_method=ReqMethod.CHAT_ANSWER,
        params={"mode": "agent.code.normal"},
    )

    with pytest.raises(RuntimeAgentDefinitionError) as caught:
        await runtime.invoke_agent(
            invalid_request,
            _definition(),
            trigger_hook=False,
        )

    assert caught.value.code == "AGENT_DEFINITION_REQUEST_INVALID"
    changed = RuntimeAgentDefinition(
        name="replacement_agent",
        instructions="Run after request validation.",
    )
    await runtime.invoke_agent(
        _request(streaming=False, session_id="sdk-session"),
        changed,
        trigger_hook=False,
    )

    owner = runtime.invoke.await_args.kwargs["_agent_execution"]
    assert owner.definition == changed
    await runtime.close()


@pytest.mark.asyncio
async def test_session_cleanup_releases_owner_for_a_new_definition() -> None:
    manager = _Manager()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )
    runtime.get_session = Mock(return_value=sentinel.owned_session)  # type: ignore[method-assign]
    runtime.invoke = AsyncMock(return_value=[])  # type: ignore[method-assign]
    request = _request(streaming=False, session_id="sdk-session")
    await runtime.invoke_agent(request, _definition(), trigger_hook=False)

    assert await runtime.cleanup_session(
        channel_id="process_cli",
        session_id="sdk-session",
    ) is True
    replacement = RuntimeAgentDefinition(
        name="replacement_agent",
        instructions="Use a new definition after cleanup.",
    )
    await runtime.invoke_agent(request, replacement, trigger_hook=False)

    owner = runtime.invoke.await_args.kwargs["_agent_execution"]
    assert owner.definition == replacement
    assert {
        "cleanup_channel_id": "process_cli",
        "cleanup_session_id": "sdk-session",
    } in manager.calls
    await runtime.close()


@pytest.mark.asyncio
async def test_new_execution_accepts_current_runtime_session_without_disk_metadata() -> None:
    runtime = AgentRuntime(
        agent_manager=_Manager(),
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )
    await runtime.start()
    await runtime._register_session(
        session_id="fresh-session",
        channel_id="process_cli",
    )
    runtime.get_session = Mock(return_value=None)  # type: ignore[method-assign]
    runtime.describe_session = AsyncMock(return_value=None)  # type: ignore[method-assign]
    runtime.invoke = AsyncMock(return_value=[])  # type: ignore[method-assign]

    await runtime.invoke_agent(
        _request(streaming=False, session_id="fresh-session"),
        _definition(),
        trigger_hook=False,
    )

    runtime.get_session.assert_called_once_with(
        SessionGetInput(
            channel_id="process_cli",
            session_id="fresh-session",
        )
    )
    runtime.describe_session.assert_awaited_once_with(session_id="fresh-session")
    runtime.invoke.assert_awaited_once()
    await runtime.close()


@pytest.mark.asyncio
async def test_new_execution_rejects_foreign_active_or_persisted_session() -> None:
    runtime = AgentRuntime(
        agent_manager=_Manager(),
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )
    await runtime.start()
    await runtime._register_session(session_id="foreign-active", channel_id="tui")
    runtime.get_session = Mock(return_value=None)  # type: ignore[method-assign]
    runtime.invoke = AsyncMock(return_value=[])  # type: ignore[method-assign]

    for session_id in ("foreign-active", "foreign-persisted"):
        with pytest.raises(SessionCatalogError) as caught:
            await runtime.invoke_agent(
                _request(streaming=False, session_id=session_id),
                _definition(),
                trigger_hook=False,
            )
        assert getattr(caught.value, "code", None) == "NOT_FOUND"
        assert str(caught.value) == "session not found"

    assert runtime.get_session.call_count == 1
    runtime.invoke.assert_not_awaited()
    await runtime.close()
