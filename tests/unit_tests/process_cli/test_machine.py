# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One-shot runner ownership, failure and shutdown contracts without a model."""

from __future__ import annotations

import asyncio
import io
import json
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from jiuwenswarm.channels.process_cli import machine
from jiuwenswarm.channels.process_cli.machine_io import OneShotWriter
from jiuwenswarm.channels.process_cli.protocol import (
    AgentSpec,
    OneShotRunInput,
    WorkspaceSpec,
)
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.events import RuntimeEvent


def _event(event_type: str, **payload) -> RuntimeEvent:
    return RuntimeEvent(
        request_id="runtime-internal-request",
        channel_id="process_cli",
        session_id="runtime-session",
        payload={"event_type": event_type, **payload},
        is_complete=event_type == "chat.final",
    )


class _FakeStream:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index == 0:
            await self.client.checkpoint("stream")
        if self.index >= len(self.client.events):
            await self.client.checkpoint("stream_tail")
            raise StopAsyncIteration
        event = self.client.events[self.index]
        self.index += 1
        return event

    async def aclose(self) -> None:
        self.client.calls.append("stream_close")
        await self.client.checkpoint("stream_close")


class FakeClient:
    """Fault injection stays at the public client/stream boundary."""

    def __init__(self, *, events=None, descriptor=None) -> None:
        self.calls: list[str] = []
        self.events = (
            [_event("chat.delta", delta="hello"), _event("chat.final", content="hello")]
            if events is None
            else events
        )
        self.descriptor = descriptor
        self.errors: dict[str, BaseException] = {}
        self.hang_at: str | None = None
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()
        self.cleanup_result = True
        self.request = None
        self.cancel_request = None
        self.agent_definition = None
        self.validated_definition = None
        self.allocated_session = "runtime-session"
        self.cleaned_sessions: list[tuple[str, str]] = []

    def raise_at(self, name: str) -> None:
        if name in self.errors:
            raise self.errors[name]

    async def checkpoint(self, name: str) -> None:
        self.raise_at(name)
        if self.hang_at == name:
            self.blocked.set()
            await self.release.wait()

    async def start(self) -> None:
        self.calls.append("start")
        await self.checkpoint("start")

    async def describe_session(self, *, session_id):
        self.calls.append(f"describe:{session_id}")
        await self.checkpoint("describe")
        return self.descriptor

    def resolve_mode_capability(self, requested):
        self.calls.append(f"mode:{requested}")
        self.raise_at("mode")
        modes = {
            "agent.code.normal": "code",
            "agent.code.plan": "code",
            "agent.work.normal": "work",
            "agent.work.plan": "work",
        }
        if requested not in modes:
            raise machine.MachineRunError("Unsupported root mode.", code="MODE_INVALID")
        return SimpleNamespace(mode=requested, work_mode=modes[requested])

    def validate_agent_definition(self, definition, *, mode):
        self.calls.append("validate_agent")
        self.raise_at("validate_agent")
        self.validated_definition = (definition, mode)

    async def create_or_resume_session(self, *, channel_id, session_id):
        self.calls.append(f"session:{channel_id}:{session_id or ''}")
        await self.checkpoint("session")
        return session_id or self.allocated_session

    def stream(self, request):
        self.calls.append("stream")
        self.request = request
        return _FakeStream(self)

    def stream_agent(self, request, definition):
        self.calls.append("stream_agent")
        self.request = request
        self.agent_definition = definition
        return _FakeStream(self)

    async def cancel(self, request) -> None:
        self.calls.append("cancel")
        self.cancel_request = request
        await self.checkpoint("cancel")

    async def cleanup_session(self, *, channel_id, session_id):
        self.calls.append("cleanup_session")
        self.cleaned_sessions.append((channel_id, session_id))
        await self.checkpoint("cleanup_session")
        return self.cleanup_result

    async def close(self) -> None:
        self.calls.append("close")
        await self.checkpoint("runtime_close")


def _writer(stdout=None):
    output = io.StringIO() if stdout is None else stdout
    return OneShotWriter(output, request_id="external-run"), output


async def _run(client, run_input=None, *, writer=None):
    writer = writer or _writer()[0]
    return await machine.run_machine(
        run_input or OneShotRunInput(input="hello"),
        writer,
        client_factory=lambda: client,
    )


@pytest.mark.asyncio
async def test_one_command_owns_one_runtime_and_returns_result_after_close() -> None:
    client = FakeClient()
    writer, output = _writer()

    result = await _run(client, writer=writer)

    assert client.calls == [
        "start",
        "mode:agent.code.normal",
        "session:process_cli:",
        "stream",
        "stream_close",
        "cleanup_session",
        "close",
    ]
    assert client.cleaned_sessions == [("process_cli", "runtime-session")]
    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.output == "hello"
    assert result.error is None
    assert result.session_id == "runtime-session"
    assert result.request_id == "external-run"
    assert result.sequence == 2
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [record["type"] for record in records] == ["event", "event"]
    assert {record["request_id"] for record in records} == {"external-run"}
    assert writer.final_written is False
    assert client.agent_definition is None


@pytest.mark.asyncio
async def test_custom_agent_is_validated_before_session_and_uses_stream_agent() -> None:
    client = FakeClient()
    agent = AgentSpec(name="custom-agent", instructions="Keep the task focused.")
    run_input = OneShotRunInput(input="question", agent=agent, mode="agent.work.plan")

    result = await _run(client, run_input)

    assert result.status == "completed"
    assert client.calls[:5] == [
        "start",
        "mode:agent.work.plan",
        "validate_agent",
        "session:process_cli:",
        "stream_agent",
    ]
    assert client.validated_definition == (agent.to_dict(), "agent.work.plan")
    assert client.agent_definition == agent.to_dict()
    assert client.request.params["work_mode"] == "work"
    assert "stream" not in client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_workspace", [False, True])
async def test_new_session_workspace_defaults_and_explicit_values(
    monkeypatch, tmp_path, explicit_workspace
) -> None:
    monkeypatch.chdir(tmp_path)
    client = FakeClient()
    workspace = (
        WorkspaceSpec(
            cwd="requested-cwd",
            project_dir="requested-project",
            trusted_dirs=("trusted",),
        )
        if explicit_workspace
        else None
    )

    result = await _run(client, OneShotRunInput(input="question", workspace=workspace))

    assert result.status == "completed"
    params = client.request.params
    assert params["cwd"] == ("requested-cwd" if explicit_workspace else str(tmp_path))
    assert params["project_dir"] == (
        "requested-project" if explicit_workspace else str(tmp_path)
    )
    assert params["trusted_dirs"] == (
        ["trusted"] if explicit_workspace else [str(tmp_path)]
    )
    assert params["query"] == params["content"] == "question"
    assert params["supports_user_interaction"] is False
    assert client.request.channel_id == "process_cli"
    assert client.request.req_method == ReqMethod.CHAT_SEND


@pytest.mark.asyncio
async def test_resume_omissions_inherit_runtime_mode_and_do_not_replace_workspace() -> (
    None
):
    client = FakeClient(descriptor=SimpleNamespace(mode="agent.work.plan"))

    result = await _run(
        client, OneShotRunInput(input="continue", session_id="existing")
    )

    assert result.session_id == "existing"
    assert client.request.params["mode"] == "agent.work.plan"
    assert client.request.params["work_mode"] == "work"
    assert {"cwd", "project_dir", "trusted_dirs"}.isdisjoint(client.request.params)
    assert client.cleaned_sessions == [("process_cli", "existing")]
    assert client.calls[:4] == [
        "start",
        "describe:existing",
        "mode:agent.work.plan",
        "mode:agent.work.plan",
    ]


@pytest.mark.asyncio
async def test_resume_only_passes_explicit_workspace_overrides() -> None:
    client = FakeClient(descriptor=SimpleNamespace(mode="agent.work.normal"))
    run_input = OneShotRunInput(
        input="continue",
        session_id="existing",
        mode="agent.code.plan",
        workspace=WorkspaceSpec(cwd="new-cwd"),
    )

    result = await _run(client, run_input)

    assert result.status == "completed"
    assert client.request.params["mode"] == "agent.code.plan"
    assert client.request.params["work_mode"] == "code"
    assert client.request.params["cwd"] == "new-cwd"
    assert "project_dir" not in client.request.params
    assert "trusted_dirs" not in client.request.params


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", ["missing-session", "foreign-session"])
async def test_missing_or_foreign_resume_never_allocates_or_cleans_session(
    session_id,
) -> None:
    # The public client deliberately reports a foreign Session as missing.
    client = FakeClient(descriptor=None)

    result = await _run(
        client, OneShotRunInput(input="continue", session_id=session_id)
    )

    assert result.error.code == "SESSION_NOT_FOUND"
    assert result.session_id is None
    assert client.calls == ["start", f"describe:{session_id}", "close"]
    assert client.cleaned_sessions == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "root_mode", ["team.code.normal", "workflow.normal", "auto.normal"]
)
@pytest.mark.parametrize("explicit_mode", [None, "agent.code.normal"])
async def test_non_single_agent_resume_is_rejected_before_session_ownership(
    root_mode, explicit_mode
) -> None:
    client = FakeClient(descriptor=SimpleNamespace(mode=root_mode))

    result = await _run(
        client,
        OneShotRunInput(input="continue", session_id="existing", mode=explicit_mode),
    )

    assert result.status == "failed"
    assert result.error.code == "MODE_INVALID"
    assert result.session_id is None
    assert client.calls == ["start", "describe:existing", f"mode:{root_mode}", "close"]
    assert client.cleaned_sessions == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage", ["start", "mode", "validate_agent", "session", "stream"]
)
async def test_execution_failures_close_runtime_and_only_clean_owned_session(
    stage,
) -> None:
    client = FakeClient()
    client.errors[stage] = RuntimeError("sensitive dependency message")
    agent = (
        AgentSpec(name="custom-agent", instructions="answer")
        if stage == "validate_agent"
        else None
    )

    result = await _run(client, OneShotRunInput(input="question", agent=agent))

    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.error.code == "RUNTIME_FAILED"
    assert "sensitive" not in result.error.message
    assert client.calls[-1] == "close"
    if stage == "stream":
        assert client.calls[-4:] == [
            "cancel",
            "stream_close",
            "cleanup_session",
            "close",
        ]
        assert result.session_id == "runtime-session"
    else:
        assert result.session_id is None
        assert client.cleaned_sessions == []
        assert "cancel" not in client.calls


@pytest.mark.asyncio
async def test_client_construction_failure_returns_safe_failure_without_resources() -> (
    None
):
    writer, output = _writer()

    def fail_factory():
        raise RuntimeError("secret factory details")

    result = await machine.run_machine(
        OneShotRunInput(input="question"), writer, client_factory=fail_factory
    )

    assert result.status == "failed"
    assert result.error.code == "RUNTIME_FAILED"
    assert result.session_id is None
    assert "secret" not in result.error.message
    assert output.getvalue() == ""


@pytest.mark.asyncio
async def test_resume_lookup_exception_does_not_claim_the_requested_session() -> None:
    client = FakeClient()
    client.errors["describe"] = RuntimeError("lookup failure")

    result = await _run(
        client, OneShotRunInput(input="continue", session_id="existing")
    )

    assert result.error.code == "RUNTIME_FAILED"
    assert result.session_id is None
    assert client.calls == ["start", "describe:existing", "close"]
    assert client.cleaned_sessions == []


@pytest.mark.asyncio
async def test_request_build_failure_still_cleans_newly_allocated_session(
    monkeypatch,
) -> None:
    client = FakeClient()

    def fail_workspace(*_args, **_kwargs):
        raise RuntimeError("request build failure")

    monkeypatch.setattr(machine, "_workspace_params", fail_workspace)

    result = await _run(client)

    assert result.error.code == "RUNTIME_FAILED"
    assert result.session_id == "runtime-session"
    assert client.cleaned_sessions == [("process_cli", "runtime-session")]
    assert client.request is None
    assert "cancel" not in client.calls
    assert client.calls[-2:] == ["cleanup_session", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["start", "session", "stream"])
async def test_timeout_covers_start_allocation_and_stream_with_precise_cancel(
    stage,
) -> None:
    client = FakeClient()
    client.hang_at = stage

    result = await _run(client, OneShotRunInput(input="question", timeout_seconds=0.02))

    assert result.status == "timed_out"
    assert result.exit_code == 124
    assert result.error.code == "TIMEOUT"
    assert client.calls[-1] == "close"
    if stage == "stream":
        cancel = client.cancel_request
        assert cancel.request_id == client.request.request_id == "external-run"
        assert cancel.params["target_request_id"] == "external-run"
        assert cancel.session_id == "runtime-session"
        assert cancel.req_method == ReqMethod.CHAT_CANCEL
        assert client.calls[-4:] == [
            "cancel",
            "stream_close",
            "cleanup_session",
            "close",
        ]
    else:
        assert client.cancel_request is None
        assert client.cleaned_sessions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["start", "session", "stream"])
async def test_external_task_cancellation_is_a_terminal_result_after_cleanup(
    stage,
) -> None:
    client = FakeClient()
    client.hang_at = stage
    task = asyncio.create_task(_run(client))
    await asyncio.wait_for(client.blocked.wait(), timeout=1)

    task.cancel()
    result = await asyncio.wait_for(task, timeout=1)

    assert result.status == "cancelled"
    assert result.exit_code == 130
    assert result.error.code == "CANCELLED"
    assert client.calls[-1] == "close"
    if stage == "stream":
        assert client.cancel_request.params["target_request_id"] == "external-run"
        assert client.cleaned_sessions == [("process_cli", "runtime-session")]
    else:
        assert client.cancel_request is None
        assert client.cleaned_sessions == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    [
        "chat.ask_user_question",
        "plan.approval_required",
        "harness.activate_interaction",
    ],
)
async def test_interaction_never_grants_approval_and_stops_stream(event_type) -> None:
    client = FakeClient(
        events=[
            _event(
                event_type, request_id="permission-id", source="permission_interrupt"
            ),
            _event("chat.final", content="must not run after confirmation"),
        ]
    )
    writer, output = _writer()

    result = await _run(client, writer=writer)

    assert result.status == "failed"
    assert result.error.code == "INTERACTION_REQUIRED"
    assert result.output is None
    assert client.request.params["supports_user_interaction"] is False
    assert client.calls[-4:] == ["cancel", "stream_close", "cleanup_session", "close"]
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [record["event_type"] for record in records] == [event_type]
    # FakeClient has no answer/approval API: this test fails if the runner calls one.


class _BrokenOutput(io.StringIO):
    def __init__(self):
        super().__init__()
        self.write_calls = 0

    def write(self, text):
        self.write_calls += 1
        raise BrokenPipeError("host closed stdout")


@pytest.mark.asyncio
async def test_broken_output_cancels_and_closes_without_retrying_output() -> None:
    client = FakeClient()
    writer, output = _writer(_BrokenOutput())

    result = await _run(client, writer=writer)

    assert result.status == "failed"
    assert result.error.code == "OUTPUT_CLOSED"
    assert writer.broken is True
    assert writer.final_written is False
    assert output.write_calls == 1
    assert client.calls[-4:] == ["cancel", "stream_close", "cleanup_session", "close"]


@pytest.mark.asyncio
async def test_false_session_cleanup_is_a_successful_idempotent_noop() -> None:
    client = FakeClient()
    client.cleanup_result = False

    result = await _run(client)

    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.error is None
    assert client.calls[-1] == "close"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["stream_close", "cleanup_session", "runtime_close"])
async def test_each_cleanup_exception_turns_otherwise_successful_run_into_failure(
    stage,
) -> None:
    client = FakeClient()
    client.errors[stage] = RuntimeError("cleanup exception")

    result = await _run(client)

    assert result.status == "failed"
    assert result.exit_code != 0
    assert result.error.code == "SHUTDOWN_FAILED"
    assert result.error.details["cleanup_errors"] == (stage,)
    assert client.calls[-3:] == ["stream_close", "cleanup_session", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage", ["cancel", "stream_close", "cleanup_session", "runtime_close"]
)
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_cleanup_exceptions_preserve_primary_failure_and_attempt_remaining_stages(
    stage, error_type
) -> None:
    client = FakeClient()
    client.errors["stream"] = machine.MachineRunError("Primary error.", code="PRIMARY")
    client.errors[stage] = error_type("cleanup exception")

    result = await _run(client)

    assert result.status == "failed"
    assert result.error.code == "PRIMARY"
    assert result.error.message == "Primary error."
    assert result.error.details["cleanup_errors"] == (stage,)
    assert client.calls[-4:] == ["cancel", "stream_close", "cleanup_session", "close"]


@pytest.mark.asyncio
async def test_multiple_cleanup_failures_are_reported_without_losing_primary_error() -> (
    None
):
    client = FakeClient()
    client.errors["stream"] = machine.MachineRunError("Primary error.", code="PRIMARY")
    stages = ("cancel", "stream_close", "cleanup_session", "runtime_close")
    client.errors.update({stage: RuntimeError(stage) for stage in stages})

    result = await _run(client)

    assert result.error.code == "PRIMARY"
    assert result.error.details["cleanup_errors"] == stages
    assert client.calls[-4:] == ["cancel", "stream_close", "cleanup_session", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage", ["cancel", "stream_close", "cleanup_session", "runtime_close"]
)
async def test_every_cleanup_stage_is_bounded_and_later_stages_still_run(
    monkeypatch, stage
) -> None:
    monkeypatch.setattr(machine, "SHUTDOWN_TIMEOUT_SECONDS", 0.02)
    client = FakeClient()
    client.errors["stream"] = machine.MachineRunError("Primary error.", code="PRIMARY")
    client.hang_at = stage

    result = await asyncio.wait_for(_run(client), timeout=1)

    assert result.error.code == "PRIMARY"
    assert result.error.details["cleanup_errors"] == (stage,)
    assert client.calls[-4:] == ["cancel", "stream_close", "cleanup_session", "close"]


@pytest.mark.asyncio
async def test_late_runtime_error_overrides_apparent_chat_completion() -> None:
    client = FakeClient(
        events=[
            _event("chat.final", content="partial answer"),
            _event(
                "runtime.error", code="LATE_FAILURE", message="Finalization failed."
            ),
        ]
    )
    writer, output = _writer()

    result = await _run(client, writer=writer)

    assert result.status == "failed"
    assert result.error.code == "LATE_FAILURE"
    assert result.output == "partial answer"
    assert result.sequence == 2
    assert len(output.getvalue().splitlines()) == 2
    assert client.calls[-4:] == ["cancel", "stream_close", "cleanup_session", "close"]


@pytest.mark.asyncio
async def test_observed_runtime_failure_survives_a_later_stream_exception() -> None:
    client = FakeClient(
        events=[
            _event("runtime.error", code="FIRST_FAILURE", message="First failure."),
        ]
    )
    client.errors["stream_tail"] = RuntimeError("secondary iterator failure")

    result = await _run(client)

    assert result.status == "failed"
    assert result.error.code == "FIRST_FAILURE"
    assert result.error.message == "First failure."
    assert client.calls[-4:] == ["cancel", "stream_close", "cleanup_session", "close"]


@pytest.mark.asyncio
async def test_interrupted_async_generator_is_closed_in_its_consuming_task() -> None:
    context = ContextVar("machine-test-context", default="outside")
    consumer_task = asyncio.current_task()
    close_tasks = []

    class ContextClient(FakeClient):
        async def stream(self, request):
            self.calls.append("stream")
            self.request = request
            token = context.set("inside")
            try:
                yield _event("chat.ask_user_question", source="permission_interrupt")
            finally:
                close_tasks.append(asyncio.current_task())
                self.calls.append("stream_close")
                context.reset(token)

    client = ContextClient()

    result = await _run(client)

    assert result.error.code == "INTERACTION_REQUIRED"
    assert "cleanup_errors" not in result.error.details
    assert close_tasks == [consumer_task]
    assert context.get() == "outside"
    assert client.calls[-4:] == ["cancel", "stream_close", "cleanup_session", "close"]


@pytest.mark.asyncio
async def test_dependency_timeout_is_runtime_failure_not_command_deadline() -> None:
    client = FakeClient()
    client.errors["stream"] = TimeoutError("upstream operation timed out")

    result = await _run(client, OneShotRunInput(input="question", timeout_seconds=10))

    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.error.code == "RUNTIME_FAILED"
    assert client.calls[-4:] == ["cancel", "stream_close", "cleanup_session", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("only_delta", [False, True], ids=["empty", "delta-only"])
async def test_stream_without_completion_evidence_is_not_success(only_delta) -> None:
    client = FakeClient(
        events=[_event("chat.delta", delta="partial")] if only_delta else []
    )
    writer, output = _writer()

    result = await _run(client, writer=writer)

    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.error.code == "INCOMPLETE_RUN"
    assert result.output == ("partial" if only_delta else None)
    assert result.sequence == int(only_delta)
    assert len(output.getvalue().splitlines()) == int(only_delta)
    assert writer.final_written is False
    assert client.calls[-4:] == ["cancel", "stream_close", "cleanup_session", "close"]


@pytest.mark.asyncio
async def test_null_payload_with_explicit_completion_is_success() -> None:
    client = FakeClient(
        events=[
            RuntimeEvent(
                request_id="internal-request",
                channel_id="process_cli",
                session_id="runtime-session",
                payload=None,
                is_complete=True,
                ok=True,
            )
        ]
    )
    writer, output = _writer()

    result = await _run(client, writer=writer)

    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.error is None
    assert result.output is None
    record = json.loads(output.getvalue())
    assert record["event_type"] == "runtime.event"
    assert record["payload"] is None
    assert client.calls[-3:] == ["stream_close", "cleanup_session", "close"]
    assert "cancel" not in client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("late_error", [False, True])
async def test_chat_final_without_complete_flag_still_drains_the_tail(
    late_error,
) -> None:
    final = _event("chat.final", content="answer")
    final.is_complete = False
    tail = (
        _event("runtime.error", code="TAIL_FAILED", message="Tail failed.")
        if late_error
        else _event("chat.usage_summary", usage={"output_tokens": 2})
    )
    client = FakeClient(events=[final, tail])
    writer, output = _writer()

    result = await _run(client, writer=writer)

    assert result.output == "answer"
    assert result.sequence == 2
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert records[0]["event_type"] == "chat.final"
    assert records[1]["event_type"] == tail.event_type
    assert client.calls[-1] == "close"
    if late_error:
        assert result.status == "failed"
        assert result.error.code == "TAIL_FAILED"
        assert result.exit_code == 1
    else:
        assert result.status == "completed"
        assert result.error is None
        assert result.exit_code == 0
        assert result.usage == {"output_tokens": 2}
