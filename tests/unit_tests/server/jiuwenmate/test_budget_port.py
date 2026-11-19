"""JM-06: model budget admission, settlement and the evidence of a restored limit."""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone

import pytest
from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetAccountRef, BudgetRepository
from jiuwenswarm.server.jiuwenmate.execution_models import (
    AccessSnapshot,
    ConversationRef,
    ExecutionRecord,
    RequestRecord,
    SessionRecord,
    build_execution_context,
)
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.ports.budget import HostBudgetPort
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.spec_provider import publish_spec

from openjiuwen.harness import persistent_agent as core
from openjiuwen.harness.persistent_agent import ports as core_ports

NS = "workspace-1"
NOW = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)
GROUP = BudgetAccountRef("group", "grp_1")
KNOWN = core.ModelUsage(status="known", input_tokens=300, output_tokens=200)
UNKNOWN = core.ModelUsage(status="unknown", reason="model call failed")


class Host:
    def __init__(self, tmp_path, reservation_tokens=1000):
        db = tmp_path / "agents.sqlite3"
        self.budgets = BudgetRepository(db)
        self.sessions = ExecutionRepository(db)
        self.port = HostBudgetPort(
            self.budgets, self.sessions, reservation_tokens=reservation_tokens, clock=lambda: NOW
        )

    def session(self, agent_id: str, conversation: ConversationRef) -> SessionRecord:
        return self.sessions.resolve_session(NS, agent_id, conversation, now=NOW)

    def execution(self, session: SessionRecord, n: int = 1) -> core.ExecutionContext:
        snapshot = publish_spec(
            session.agent_id, "v1", {"card": {"id": session.agent_id, "name": "同事"}, "enable_sys_operation": False}
        )
        request = RequestRecord(
            namespace_id=NS,
            agent_id=session.agent_id,
            session_id=session.session_id,
            request_id=f"req-{n}",
            source_ref=f"source:{n}",
            config_version=snapshot.config_version,
            config_fingerprint=snapshot.fingerprint,
            access=AccessSnapshot(
                principal_ref="user-1",
                startup_scope=frozenset({"chat"}),
                source_scope=frozenset({"chat"}),
                policy_revision="policy-1",
            ),
            request_input=core.RequestInput(kind="scheduled", text="早报", reply_target="conversation-1"),
            received_at=NOW,
        )
        return build_execution_context(
            request,
            ExecutionRecord(request_id=f"req-{n}", execution_id=f"exe-{n}"),
            capability_snapshot=snapshot.enabled_features,
        )

    def limit(self, account: BudgetAccountRef, tokens: int | None) -> int:
        return self.budgets.adjust_limit(NS, account, tokens, reason="设定额度", actor_ref="admin-1", now=NOW)


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


# JM-06-V-01 shared group account and atomic admission


async def test_group_colleagues_share_one_account_without_overselling(host):
    host.limit(GROUP, 5000)
    executions = [host.execution(host.session(f"mate_{i}", ConversationRef.group("grp_1")), i) for i in range(8)]

    admissions = await asyncio.gather(*(host.port.admit(e, f"mc_{i}") for i, e in enumerate(executions)))

    assert sum(a.status == "admitted" for a in admissions) == 5
    assert all(a.reason for a in admissions if a.status == "denied")
    view = host.budgets.account(NS, GROUP)
    assert view.reserved_tokens == 5000
    assert view.used_tokens + view.reserved_tokens <= view.limit_tokens


async def test_private_sessions_are_charged_per_colleague(host):
    dev, ops = BudgetAccountRef("agent", "mate_dev"), BudgetAccountRef("agent", "mate_ops")
    host.limit(dev, 1000)
    first = host.execution(host.session("mate_dev", ConversationRef.web("conv-1")), 1)
    second = host.execution(host.session("mate_dev", ConversationRef.web("conv-2")), 2)
    other = host.execution(host.session("mate_ops", ConversationRef.web("conv-1")), 3)

    assert (await host.port.admit(first, "mc_1")).status == "admitted"
    assert (await host.port.admit(second, "mc_2")).status == "denied"
    assert (await host.port.admit(other, "mc_3")).status == "admitted"
    assert host.budgets.account(NS, ops).reserved_tokens == 1000


async def test_account_without_limit_admits_and_records_usage(host):
    execution = host.execution(host.session("mate_dev", ConversationRef.group("grp_1")))

    admission = await host.port.admit(execution, "mc_1")
    await host.port.report(execution, "mc_1", KNOWN)

    assert admission.status == "admitted"
    view = host.budgets.account(NS, GROUP)
    assert (view.limit_tokens, view.used_tokens, view.reserved_tokens) == (None, 500, 0)


async def test_denied_admission_is_a_valid_core_result(host):
    host.limit(GROUP, 0)
    execution = host.execution(host.session("mate_dev", ConversationRef.group("grp_1")))

    admission = await host.port.admit(execution, "mc_1")

    checked = core.BudgetAdmission.model_validate(admission)
    checked.assert_call(execution, "mc_1")
    assert (checked.status, checked.reservation_ref) == ("denied", None)
    assert "exhausted" in checked.reason


# JM-06-V-02 idempotent settlement and unknown usage


async def test_known_usage_is_charged_and_reservation_released(host):
    host.limit(GROUP, 5000)
    execution = host.execution(host.session("mate_dev", ConversationRef.group("grp_1")))
    admission = await host.port.admit(execution, "mc_1")

    report = await host.port.report(execution, "mc_1", KNOWN)

    core.BudgetReport.model_validate(report).assert_call(execution, "mc_1")
    assert (admission.reservation_ref, report.status, report.receipt_ref) == ("res:mc_1", "confirmed", "rcpt:mc_1")
    view = host.budgets.account(NS, GROUP)
    assert (view.used_tokens, view.reserved_tokens, view.unreconciled_calls) == (500, 0, 0)


async def test_unknown_usage_charges_the_reservation_and_is_flagged(host):
    execution = host.execution(host.session("mate_dev", ConversationRef.group("grp_1")))
    await host.port.admit(execution, "mc_1")

    report = await host.port.report(execution, "mc_1", UNKNOWN)

    assert report.status == "confirmed"
    view = host.budgets.account(NS, GROUP)
    assert (view.used_tokens, view.reserved_tokens, view.unreconciled_calls) == (1000, 0, 1)


async def test_repeated_report_is_charged_once(host):
    execution = host.execution(host.session("mate_dev", ConversationRef.group("grp_1")))
    await host.port.admit(execution, "mc_1")

    first = await host.port.report(execution, "mc_1", KNOWN)
    second = await host.port.report(execution, "mc_1", KNOWN)

    assert first == second
    assert host.budgets.account(NS, GROUP).used_tokens == 500


async def test_report_with_other_usage_is_rejected(host):
    execution = host.execution(host.session("mate_dev", ConversationRef.group("grp_1")))
    await host.port.admit(execution, "mc_1")
    await host.port.report(execution, "mc_1", KNOWN)

    report = await host.port.report(execution, "mc_1", UNKNOWN)

    assert (report.status, report.receipt_ref) == ("rejected", None)
    assert "already settled" in report.reason
    assert host.budgets.account(NS, GROUP).used_tokens == 500


async def test_report_for_unadmitted_or_foreign_call_is_rejected(host):
    host.limit(GROUP, 0)
    mine = host.execution(host.session("mate_dev", ConversationRef.group("grp_1")), 1)
    other = host.execution(host.session("mate_ops", ConversationRef.group("grp_1")), 2)
    host.limit(GROUP, None)
    await host.port.admit(other, "mc_other")
    host.limit(GROUP, 0)
    await host.port.admit(mine, "mc_denied")

    for model_call_id in ("mc_missing", "mc_denied", "mc_other"):
        report = await host.port.report(mine, model_call_id, KNOWN)
        assert report.status == "rejected", model_call_id
    assert host.budgets.account(NS, GROUP).used_tokens == 0


async def test_repeated_admit_returns_the_first_decision(host):
    host.limit(GROUP, 1000)
    execution = host.execution(host.session("mate_dev", ConversationRef.group("grp_1")))

    first = await host.port.admit(execution, "mc_1")
    again = await host.port.admit(execution, "mc_1")

    assert first == again
    assert host.budgets.account(NS, GROUP).reserved_tokens == 1000


# JM-06-V-03 evidence of a restored limit


async def test_adjustment_after_denial_is_evidence_for_continuation(host):
    host.limit(GROUP, 0)
    execution = host.execution(host.session("mate_dev", ConversationRef.group("grp_1")))
    assert (await host.port.admit(execution, "mc_1")).status == "denied"

    assert host.budgets.adjustments_since_denial(NS, "mc_1") == []
    revision = host.budgets.adjust_limit(NS, GROUP, 20_000, reason="追加本月额度", actor_ref="admin-2", now=NOW)

    [adjustment] = host.budgets.adjustments_since_denial(NS, "mc_1")
    assert (adjustment.revision, adjustment.limit_tokens, adjustment.actor_ref) == (revision, 20_000, "admin-2")
    assert (await host.port.admit(execution, "mc_2")).status == "admitted"


async def test_budget_state_survives_restart(tmp_path):
    host = Host(tmp_path)
    host.limit(GROUP, 0)
    execution = host.execution(host.session("mate_dev", ConversationRef.group("grp_1")))
    await host.port.admit(execution, "mc_1")

    restarted = Host(tmp_path)

    assert restarted.budgets.account(NS, GROUP).revision == 1
    assert restarted.budgets.adjustments_since_denial(NS, "mc_1") == []
    assert (await restarted.port.admit(execution, "mc_1")).status == "denied"


def test_port_satisfies_the_core_contract(tmp_path, host):
    class Commit:
        async def record(self, execution, event): ...

        async def read_execution_log(self, execution, execution_id): ...

    for name in ("admit", "report"):
        expected = inspect.signature(getattr(core_ports.BudgetPort, name)).parameters
        assert list(inspect.signature(getattr(HostBudgetPort, name)).parameters) == list(expected)
    windows = WindowBackendStore(tmp_path / "agents.sqlite3").bind(NS, "mate_dev", "sess_1")
    core.PersistentPorts(commit=Commit(), budget=host.port, window_backend=windows).validate()


def test_reservation_must_be_positive(host):
    for tokens in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            HostBudgetPort(host.budgets, host.sessions, reservation_tokens=tokens)


def test_adjustment_needs_a_valid_limit_and_reason(host):
    for limit, reason in ((-1, "x"), (True, "x"), (10, " ")):
        with pytest.raises(PersistentAgentError):
            host.budgets.adjust_limit(NS, GROUP, limit, reason=reason, actor_ref="admin-1", now=NOW)
