"""Contract tests between the host persistent-agent adapters and the pinned agent-core."""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from jiuwenswarm.server.jiuwenmate.execution_models import (
    AccessSnapshot,
    ExecutionRecord,
    PersistentContractError,
    RequestRecord,
    build_execution_context,
)
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.spec_provider import (
    HOST_OPEN_FEATURES,
    ConfigSnapshot,
    build_invocation,
    core_features,
    publish_spec,
)
from pydantic import ValidationError

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.session.checkpointer import NoopCheckpointer
from openjiuwen.core.single_agent import AgentCard, create_agent_session
from openjiuwen.harness import persistent_agent as core
from openjiuwen.harness.persistent_agent import ports as core_ports
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

AGENT_ID = "colleague-1"


class FakeHostPorts:
    """Commit and budget double with the exact core port signatures; never used to execute."""

    def __init__(self):
        self.events = []

    async def record(self, execution: core.ExecutionContext, event: core.AgentEvent) -> None:
        self.events.append(event)

    async def read_execution_log(
        self, execution: core.ExecutionContext, execution_id: str
    ) -> tuple[core.AgentEvent, ...]:
        return ()

    async def admit(self, execution: core.ExecutionContext, model_call_id: str) -> core.BudgetAdmission:
        ref = core.InvocationRef.from_execution(execution).model_dump()
        return core.BudgetAdmission.model_validate(
            {**ref, "model_call_id": model_call_id, "status": "admitted", "reservation_ref": "res-1"}
        )

    async def report(
        self, execution: core.ExecutionContext, model_call_id: str, usage: core.ModelUsage
    ) -> core.BudgetReport:
        ref = core.InvocationRef.from_execution(execution).model_dump()
        return core.BudgetReport.model_validate(
            {**ref, "model_call_id": model_call_id, "status": "confirmed", "receipt_ref": "rcpt-1"}
        )


def spec_fields(**changes):
    fields = {"card": {"id": AGENT_ID, "name": "研发同事"}, "enable_sys_operation": False}
    fields.update(changes)
    return fields


def direct_input(**changes) -> core.RequestInput:
    fields = {
        "kind": "direct_message",
        "text": "整理本周进展",
        "source": core.DirectMessageSource(
            message_id="msg-1",
            sender_id="user-1",
            sender_name="张三",
            received_at=datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc),
        ),
        "reply_target": "conversation-1",
    }
    fields.update(changes)
    return core.RequestInput.model_validate(fields)


def request_for(snapshot: ConfigSnapshot, **changes) -> RequestRecord:
    fields = {
        "namespace_id": "tenant-1",
        "agent_id": snapshot.agent_id,
        "session_id": "session-1",
        "request_id": "request-1",
        "source_ref": "source:msg-1",
        "config_version": snapshot.config_version,
        "config_fingerprint": snapshot.fingerprint,
        "access": AccessSnapshot(
            principal_ref="user-1",
            startup_scope=frozenset({"chat"}),
            source_scope=frozenset({"chat"}),
            policy_revision="policy-1",
        ),
        "request_input": direct_input(),
        "received_at": datetime(2026, 10, 12, 9, 0, 1, tzinfo=timezone.utc),
    }
    fields.update(changes)
    return RequestRecord(**fields)


@pytest.fixture
def host_ports(tmp_path):
    """Ports for the Session ``request_for`` uses, with the real host window backend."""
    windows = WindowBackendStore(tmp_path / "agents.sqlite3").bind("tenant-1", AGENT_ID, "session-1")

    def make(**changes) -> core.PersistentPorts:
        host = FakeHostPorts()
        return core.PersistentPorts(commit=host, budget=host, window_backend=windows, **changes)

    return make


@pytest.fixture
def snapshot() -> ConfigSnapshot:
    return publish_spec(AGENT_ID, "v1", spec_fields())


def test_published_spec_is_a_core_spec_with_fixed_single_round_fields(snapshot):
    spec = snapshot.load_spec()

    assert isinstance(spec, core.PersistentAgentSpec)
    assert spec.enable_task_loop is False
    assert spec.auto_create_workspace is False
    assert spec.enabled_features == frozenset({"construction"})
    assert snapshot.fingerprint == spec.fingerprint()
    assert ConfigSnapshot.from_dict(snapshot.to_dict()) == snapshot


def test_valid_spec_context_and_request_input_are_accepted_by_core(snapshot, host_ports):
    request = request_for(snapshot)
    spec, build_context = build_invocation(
        snapshot, request, ExecutionRecord(request_id="request-1", execution_id="execution-1"), ports=host_ports()
    )

    assert isinstance(build_context, core.PersistentBuildContext)
    assert isinstance(build_context.execution, core.ExecutionContext)
    assert build_context.execution.capability_snapshot == spec.enabled_features
    assert build_context.execution.config_fingerprint == spec.fingerprint()
    assert isinstance(build_context.session.checkpointer(), NoopCheckpointer)
    assert isinstance(request.request_input, core.RequestInput)
    assert RequestRecord.from_dict(request.to_dict()) == request


def test_scheduled_request_input_is_accepted_without_source(snapshot):
    request = request_for(
        snapshot, request_input=core.RequestInput(kind="scheduled", text="早报", reply_target="conversation-1")
    )

    assert request.request_input.source is None


@pytest.mark.parametrize(
    "fields",
    [
        spec_fields(unknown_field=True),
        spec_fields(card={"id": "another-agent", "name": "x"}),
        spec_fields(schema_version=2),
        spec_fields(enabled_features=["not-a-feature"]),
    ],
)
def test_mismatched_spec_fields_are_rejected(fields):
    with pytest.raises(PersistentContractError):
        publish_spec(AGENT_ID, "v1", fields)


@pytest.mark.parametrize(
    "fields",
    [
        spec_fields(enable_task_loop=True),
        spec_fields(auto_create_workspace=True),
        spec_fields(enable_async_subagent=True),
        spec_fields(enable_subagent_runtime=True),
        spec_fields(add_general_purpose_agent=True),
    ],
)
def test_capabilities_not_opened_by_the_host_are_rejected(fields):
    with pytest.raises(PersistentContractError, match="not opened"):
        publish_spec(AGENT_ID, "v1", fields)


def test_host_opens_only_features_core_advertises():
    assert {"workspace_read", "workspace_write"} <= core_features()
    assert "window_management" in HOST_OPEN_FEATURES
    assert HOST_OPEN_FEATURES <= core_features()


def test_office_directory_read_and_write_can_be_published():
    features = ["construction", "workspace_read", "workspace_write"]
    fields = spec_fields(enabled_features=features, enable_sys_operation=True)

    snapshot = publish_spec(AGENT_ID, "v1", fields)

    assert snapshot.enabled_features == {"construction", "workspace_read", "workspace_write"}


def test_tampered_snapshot_is_refused(snapshot):
    tampered = replace(snapshot, spec_json=snapshot.spec_json.replace("研发同事", "改名"))

    with pytest.raises(PersistentContractError, match="fingerprint"):
        tampered.load_spec()
    with pytest.raises(PersistentContractError, match="capability"):
        replace(snapshot, enabled_features=frozenset({"construction", "workspace_read"})).load_spec()


@pytest.mark.parametrize(
    "changes",
    [
        {"config_fingerprint": "0" * 64},
        {"config_version": "v2"},
        {"agent_id": "another-agent"},
    ],
)
def test_request_from_another_configuration_is_refused(snapshot, changes, host_ports):
    request = request_for(snapshot, **changes)

    with pytest.raises(PersistentContractError):
        build_invocation(
            snapshot, request, ExecutionRecord(request_id="request-1", execution_id="e-1"), ports=host_ports()
        )


def test_capability_snapshot_mismatch_is_refused_by_core(snapshot, host_ports):
    spec = snapshot.load_spec()
    context = build_execution_context(
        request_for(snapshot),
        ExecutionRecord(request_id="request-1", execution_id="e-1"),
        capability_snapshot=frozenset({"construction", "workspace_read"}),
    )
    build = core.PersistentBuildContext(
        execution=context,
        session=create_agent_session(session_id="session-1", card=spec.card, checkpointer=NoopCheckpointer()),
        ports=host_ports(),
    )

    with pytest.raises(BaseError, match="capability snapshot mismatch"):
        build.validate(spec)


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "scheduled"},
        {"extra": "x"},
        {"continue_instruction": "继续"},
    ],
)
def test_invalid_request_input_is_refused(snapshot, changes):
    with pytest.raises((PersistentContractError, ValidationError)):
        request_for(snapshot, request_input=direct_input(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"namespace_id": " "},
        {"received_at": datetime(2026, 10, 12, 9, 0)},
    ],
)
def test_incomplete_request_record_is_refused(snapshot, changes):
    with pytest.raises(PersistentContractError):
        request_for(snapshot, **changes)


def test_context_rule_violations_are_reported_as_contract_errors(snapshot):
    with pytest.raises(PersistentContractError, match="ExecutionContext"):
        build_execution_context(
            request_for(snapshot, config_fingerprint="not-hex"),
            ExecutionRecord(request_id="request-1", execution_id="e-1"),
            capability_snapshot=snapshot.enabled_features,
        )
    with pytest.raises(PersistentContractError, match="another Request"):
        build_execution_context(
            request_for(snapshot),
            ExecutionRecord(request_id="request-2", execution_id="e-1"),
            capability_snapshot=snapshot.enabled_features,
        )


def test_continuation_invocation_keeps_the_recovery_ref(snapshot, host_ports):
    spec, context = build_invocation(
        snapshot,
        request_for(snapshot),
        ExecutionRecord(request_id="request-1", execution_id="e-2", recovery_ref="e-1"),
        ports=host_ports(),
    )

    assert context.execution.recovery_ref == "e-1"
    assert spec.continuation_chain_limit == 8


async def test_core_entries_still_reject_values_the_host_cannot_publish(snapshot, host_ports):
    unsafe = core.PersistentAgentSpec(card=AgentCard(id=AGENT_ID, name="x"), enable_task_loop=True)
    _, build = build_invocation(
        snapshot, request_for(snapshot), ExecutionRecord(request_id="request-1", execution_id="e-1"), ports=host_ports()
    )

    with pytest.raises(BaseError, match="single-round"):
        async for _ in core.execute_persistent_request(unsafe, build, direct_input()):
            pass


async def test_core_rejects_group_message_without_group_chat_port(host_ports):
    capacity_snapshot = publish_spec(
        AGENT_ID,
        "capacity-v1",
        spec_fields(
            model=TeamModelConfig(
                model_client_config=ModelClientConfig(
                    client_provider="OpenAI", api_key="test-key", api_base="https://example.invalid/v1"
                ),
                model_request_config=ModelRequestConfig(model="test-model", context_window=8192, max_tokens=1024),
            ),
            model_input_limit_tokens=7000,
            capacity_safety_margin_tokens=100,
        ),
    )
    group_input = core.RequestInput(
        kind="group_message",
        text="@研发同事 看一下",
        source=core.GroupMessage(
            message_id="gm-1",
            sender_id="user-1",
            sender_name="张三",
            sender_kind="human",
            received_at=datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc),
            text="@研发同事 看一下",
            mention_ids=(AGENT_ID,),
        ),
        reply_target="group-1",
    )
    spec, build = build_invocation(
        capacity_snapshot,
        request_for(capacity_snapshot, request_input=group_input),
        ExecutionRecord(request_id="request-1", execution_id="e-1"),
        ports=host_ports(),
    )

    with pytest.raises(BaseError, match="group chat port"):
        async for _ in core.execute_persistent_request(spec, build, group_input):
            pass


@pytest.mark.parametrize(
    ("protocol", "names"),
    [
        (core_ports.CommitPort, ("record", "read_execution_log")),
        (core_ports.BudgetPort, ("admit", "report")),
    ],
)
def test_fake_ports_match_core_protocol_signatures(protocol, names):
    for name in names:
        expected = inspect.signature(getattr(protocol, name))
        actual = inspect.signature(getattr(FakeHostPorts, name))
        assert list(actual.parameters) == list(expected.parameters), name
        assert inspect.iscoroutinefunction(getattr(FakeHostPorts, name))


async def test_fake_ports_pass_core_port_validation_and_return_core_types(snapshot, host_ports):
    ports = host_ports()
    ports.validate()
    _, build = build_invocation(
        snapshot, request_for(snapshot), ExecutionRecord(request_id="request-1", execution_id="e-1"), ports=ports
    )

    admission = await ports.budget.admit(build.execution, "call-1")
    report = await ports.budget.report(
        build.execution, "call-1", core.ModelUsage(status="known", input_tokens=1, output_tokens=1)
    )

    assert isinstance(admission, core.BudgetAdmission)
    assert isinstance(report, core.BudgetReport)
