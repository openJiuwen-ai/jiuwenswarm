"""JM-07: colleague manager, Session scheduler and Execution driver."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetAccountRef, BudgetRepository
from jiuwenswarm.server.jiuwenmate.execution_driver import outcome_of
from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot, ConversationRef
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.session_scheduler import SchedulerLimits
from jiuwenswarm.server.jiuwenmate.spec_provider import publish_spec

from openjiuwen.harness import persistent_agent as core

NS = "workspace-1"
NOW = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
ACCESS = AccessSnapshot(
    principal_ref="user-1",
    startup_scope=frozenset({"chat"}),
    source_scope=frozenset({"chat"}),
    policy_revision="policy-1",
)


class FakeService:
    def __init__(self, core_fake: FakeCore) -> None:
        self._core = core_fake
        self.deleted: list[tuple[str, int]] = []

    def snapshot(self, namespace_id: str, agent_id: str, config_version: str):
        return publish_spec(
            agent_id, config_version, {"card": {"id": agent_id, "name": "同事"}, "enable_sys_operation": False}
        )

    def delete_colleague(self, namespace_id: str, agent_id: str) -> None:
        self.deleted.append((agent_id, self._core.live))


class FakeCore:
    """Stand-in for core's entries; each Request runs until released or aborted.

    ``modes`` maps a request text to how it ends: a final status, ``paused``,
    ``stopped``, ``raise`` or ``silent`` (no terminal event).
    """

    def __init__(self) -> None:
        self.modes: dict[str, str] = {}
        self.admit: set[str] = set()
        self.started: list[str] = []
        self.ended: list[str] = []
        self.aborts: list[tuple[str, str]] = []
        self.contexts: dict[str, core.PersistentBuildContext] = {}
        self.abort_gate: asyncio.Event | None = None
        self.live = 0
        self.peak = 0
        self._released: set[str] = set()
        self._aborted: dict[str, str] = {}
        self._wake: dict[str, asyncio.Event] = {}
        self._rid: dict[str, str] = {}

    def _event(self, request_id: str) -> asyncio.Event:
        return self._wake.setdefault(request_id, asyncio.Event())

    def release(self, *texts: str) -> None:
        for text in texts:
            self._released.add(text)
            if text in self._rid:
                self._event(self._rid[text]).set()

    async def abort(self, context, *, reason):
        request_id = context.execution.request_id
        self.aborts.append((request_id, reason))
        if self.abort_gate is not None:
            await self.abort_gate.wait()
        self._aborted.setdefault(request_id, reason)
        self._event(request_id).set()

    async def execute(self, spec, context, inputs):
        text, execution = inputs.text, context.execution
        self._rid[text] = execution.request_id
        self.contexts[text] = context
        self.started.append(text)
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            if text in self.admit:
                await context.ports.budget.admit(execution, f"mc-{text}")
            await context.ports.window_backend.set(f"pa:{spec.card.id}:{execution.session_id}:note", text)
            if text not in self._released and execution.request_id not in self._aborted:
                await self._event(execution.request_id).wait()
            mode = self.modes.get(text, "completed")
            reason = self._aborted.get(execution.request_id)
            if mode == "raise":
                raise RuntimeError("model crashed")
            if mode == "silent":
                return
            if reason == "cancel":
                event_type = core.EventType.EXECUTION_FINAL
                payload = {"status": "cancelled", "reason": "用户取消", "output_refs": ()}
            elif reason == "stop" or mode == "stopped":
                event_type, payload = core.EventType.EXECUTION_PAUSED, {"reason": "stopped"}
            elif mode == "paused":
                event_type, payload = core.EventType.EXECUTION_PAUSED, {"reason": "budget_denied", "detail": "额度不足"}
            else:
                payload = {"status": mode, "output_refs": ()}
                if mode != "completed":
                    payload["reason"] = "无需回复"
                event_type = core.EventType.EXECUTION_FINAL
            event = core.AgentEvent.create(
                execution, event_type=event_type, local_seq=1, occurred_at=NOW, payload=payload
            )
            await context.ports.commit.record(execution, event)
            yield event
        finally:
            self.live -= 1
            self.ended.append(text)


class Host:
    def __init__(self, tmp_path, *, limits: SchedulerLimits | None = None, **manager_kwargs) -> None:
        db = tmp_path / "agents.sqlite3"
        self.sessions = ExecutionRepository(db)
        self.windows = WindowBackendStore(db)
        self.budgets = BudgetRepository(db)
        self.core = FakeCore()
        self.service = FakeService(self.core)
        self.manager = AgentManager(
            self.service,
            self.sessions,
            self.windows,
            self.budgets,
            execute=self.core.execute,
            abort=self.core.abort,
            limits=limits,
            reservation_tokens=1000,
            clock=lambda: NOW,
            **manager_kwargs,
        )
        self.scheduler = self.manager.scheduler

    def session(self, agent_id: str = "mate_dev", conversation: str = "conv-1"):
        return self.sessions.resolve_session(NS, agent_id, ConversationRef.web(conversation), now=NOW)

    async def send(self, text: str, agent_id: str = "mate_dev", conversation: str = "conv-1"):
        session = self.session(agent_id, conversation)
        state, _ = await self.scheduler.accept(
            session,
            snapshot=self.service.snapshot(NS, agent_id, "v1"),
            source_ref=f"source:{text}",
            access=ACCESS,
            request_input=core.RequestInput(kind="scheduled", text=text, reply_target=conversation),
            received_at=NOW,
        )
        return state

    def status(self, state) -> tuple[str, str | None]:
        stored = self.sessions.get_request(NS, state.record.request_id)
        return stored.status, stored.outcome


async def settle(predicate) -> None:
    for _ in range(400):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


@pytest.fixture
async def host(tmp_path):
    host = Host(tmp_path)
    await host.manager.start()
    yield host
    host.core.release(*host.core.started)
    await host.manager.aclose()


# JM-07-V-01 one Request at a time per Session; Sessions run in parallel within capacity, each on its own instance


async def test_requests_of_one_session_run_one_at_a_time_in_order(host):
    states = [await host.send(text) for text in ("一", "二", "三")]

    await settle(lambda: host.core.started == ["一"])
    assert [host.status(s)[0] for s in states] == ["dispatched", "queued", "queued"]
    for text in ("一", "二", "三"):
        host.core.release(text)
    await host.scheduler.drain()

    assert host.core.started == ["一", "二", "三"]
    assert host.core.peak == 1
    assert [host.status(s) for s in states] == [("ended", "completed")] * 3


async def test_running_executions_respect_global_and_per_colleague_limits(tmp_path):
    host = Host(tmp_path, limits=SchedulerLimits(max_running=3, max_running_per_agent=2))
    await host.manager.start()
    try:
        for i in range(3):
            await host.send(f"dev-{i}", "mate_dev", f"conv-{i}")
            await host.send(f"ops-{i}", "mate_ops", f"conv-{i}")
        await settle(lambda: len(host.core.started) == 3)
        await asyncio.sleep(0.05)

        assert len(host.core.started) == 3
        assert sum(t.startswith("dev") for t in host.core.started) <= 2
        assert sum(t.startswith("ops") for t in host.core.started) <= 2

        host.core.release(*(f"{agent}-{i}" for agent in ("dev", "ops") for i in range(3)))
        await host.scheduler.drain()
        assert len(host.core.ended) == 6
        assert host.core.peak == 3
        assert len({id(context) for context in host.core.contexts.values()}) == 6
    finally:
        await host.manager.aclose()


# JM-07-V-02 controls reach only their target; a Session is released only after the stream has ended


async def test_next_request_waits_until_the_stream_has_ended(host):
    first = await host.send("一")
    await host.send("二")
    await settle(lambda: host.core.started == ["一"])

    host.core.release("一")
    await settle(lambda: host.core.started == ["一", "二"])

    assert host.core.ended[0] == "一"
    assert host.status(first) == ("ended", "completed")


async def test_cancel_reaches_only_the_target_execution(host):
    target = await host.send("目标", conversation="conv-1")
    other = await host.send("旁路", conversation="conv-2")
    await settle(lambda: len(host.core.started) == 2)

    await host.scheduler.abort(NS, target.record.request_id, "cancel")
    await settle(lambda: host.status(target)[0] == "ended")

    assert host.core.aborts == [(target.record.request_id, "cancel")]
    assert host.status(target) == ("ended", "cancelled")
    assert host.status(other) == ("dispatched", None)


async def test_stop_of_a_running_execution_ends_as_stopped(host):
    state = await host.send("任务")
    await settle(lambda: host.core.started == ["任务"])

    await host.scheduler.abort(NS, state.record.request_id, "stop")
    await host.scheduler.drain()

    assert host.status(state) == ("ended", "stopped")


async def test_aborting_a_queued_request_ends_it_without_running(host):
    await host.send("一")
    queued = await host.send("二")

    await host.scheduler.abort(NS, queued.record.request_id, "cancel")
    host.core.release("一")
    await host.scheduler.drain()

    assert host.status(queued) == ("ended", "cancelled")
    assert host.core.started == ["一"]


async def test_abort_given_before_core_starts_is_handed_over(host):
    handle = host.manager.scheduler._driver.new_handle()
    calls = []

    async def record(context, *, reason):
        calls.append(reason)

    handle._abort = record
    await handle.abort("stop")
    await handle.abort("cancel")
    assert calls == []

    await handle._attach(object())

    assert calls == ["stop"]
    assert handle.reason == "stop"


# JM-07-V-03 one scheduling entry with queue limits; continuation not opened yet


async def test_full_session_queue_is_refused(tmp_path):
    host = Host(tmp_path, limits=SchedulerLimits(max_queued_per_session=2))
    await host.manager.start()
    try:
        await host.send("一")
        await host.send("二")
        await host.send("三")
        with pytest.raises(PersistentAgentError) as excinfo:
            await host.send("四")
        assert excinfo.value.code == "QUEUE_FULL"
    finally:
        host.core.release("一", "二", "三")
        await host.manager.aclose()


def test_limits_must_be_positive():
    with pytest.raises(ValueError):
        SchedulerLimits(max_running=0)


async def test_closed_scheduler_refuses_requests(tmp_path):
    host = Host(tmp_path)
    with pytest.raises(PersistentAgentError) as excinfo:
        await host.send("任务")
    assert excinfo.value.code == "SCHEDULER_CLOSED"


async def test_resume_is_not_opened_yet(host):
    state = await host.send("任务")
    with pytest.raises(PersistentAgentError) as excinfo:
        host.scheduler.resume(state)
    assert excinfo.value.code == "PERSISTENT_NOT_OPENED"


# JM-07-V-04 lifecycle, outcomes and cleanup


@pytest.mark.parametrize(
    ("mode", "outcome"),
    [("completed", "completed"), ("ignored", "ignored"), ("paused", "paused"), ("stopped", "stopped")],
)
async def test_terminal_event_maps_to_the_request_outcome(host, mode, outcome):
    host.core.modes["任务"] = mode
    state = await host.send("任务")
    host.core.release("任务")
    await host.scheduler.drain()

    assert host.status(state) == ("ended", outcome)


@pytest.mark.parametrize("mode", ["raise", "silent"])
async def test_execution_without_a_terminal_event_fails_and_the_session_moves_on(host, mode):
    host.core.modes["一"] = mode
    first = await host.send("一")
    second = await host.send("二")
    host.core.release("一", "二")
    await host.scheduler.drain()

    assert host.status(first) == ("ended", "failed")
    assert host.status(second) == ("ended", "completed")


def test_outcome_of_no_terminal_event_is_failed():
    assert outcome_of(None) == "failed"


async def test_unreported_model_calls_are_settled_when_the_execution_ends(host):
    host.core.modes["任务"] = "raise"
    host.core.admit.add("任务")
    await host.send("任务")
    host.core.release("任务")
    await host.scheduler.drain()

    view = host.budgets.account(NS, BudgetAccountRef("agent", "mate_dev"))
    assert (view.used_tokens, view.reserved_tokens) == (1000, 0)


async def test_candidate_copies_are_forgotten_when_the_execution_ends(host, monkeypatch):
    forgotten = []
    monkeypatch.setattr(host.manager.commit_queue, "forget", lambda ns, eid: forgotten.append((ns, eid)))
    host.core.modes["任务"] = "raise"
    state = await host.send("任务")
    host.core.release("任务")
    await host.scheduler.drain()

    executions = host.sessions.executions(NS, state.record.request_id)
    assert forgotten == [(NS, executions[0].execution_id)]


async def test_window_backend_is_bound_to_the_requests_session(host):
    await host.send("私聊一", conversation="conv-1")
    await host.send("私聊二", conversation="conv-2")
    host.core.release("私聊一", "私聊二")
    await host.scheduler.drain()

    for conversation, text in (("conv-1", "私聊一"), ("conv-2", "私聊二")):
        session_id = host.session(conversation=conversation).session_id
        backend = host.windows.bind(NS, "mate_dev", session_id)
        assert await backend.get(f"pa:mate_dev:{session_id}:note") == text


async def test_start_and_close_run_the_commit_queue(tmp_path):
    host = Host(tmp_path)
    await host.manager.start()
    assert host.manager.commit_queue.running
    await host.manager.aclose()
    assert not host.manager.commit_queue.running


async def test_close_stops_running_executions_and_waits_for_them(tmp_path):
    host = Host(tmp_path)
    await host.manager.start()
    state = await host.send("任务")
    await settle(lambda: host.core.started == ["任务"])

    await host.manager.aclose()

    assert host.core.live == 0
    assert host.status(state) == ("ended", "stopped")


async def test_recover_runs_queued_requests_and_reports_dispatched_ones(tmp_path):
    before = Host(tmp_path)
    session = before.session()
    for text in ("中断", "排队"):
        before.sessions.accept_request(
            session,
            snapshot=before.service.snapshot(NS, "mate_dev", "v1"),
            source_ref=f"source:{text}",
            access=ACCESS,
            request_input=core.RequestInput(kind="scheduled", text=text, reply_target="conv-1"),
            received_at=NOW,
        )
    other = before.session(conversation="conv-2")
    queued, _ = before.sessions.accept_request(
        other,
        snapshot=before.service.snapshot(NS, "mate_dev", "v1"),
        source_ref="source:另一会话",
        access=ACCESS,
        request_input=core.RequestInput(kind="scheduled", text="另一会话", reply_target="conv-2"),
        received_at=NOW,
    )
    interrupted = before.sessions.session_requests(NS, session.session_id)[0]
    before.sessions.start_execution(NS, interrupted.record.request_id, now=NOW)

    host = Host(tmp_path)
    await host.manager.start()
    try:
        left = await host.scheduler.recover(NS)
        host.core.release("另一会话")
        await host.scheduler.drain()

        assert [s.record.request_id for s in left] == [interrupted.record.request_id]
        assert host.core.started == ["另一会话"]
        assert host.status(queued) == ("ended", "completed")
    finally:
        await host.manager.aclose()


async def test_deleting_a_colleague_ends_its_work_before_removing_it(host):
    running = await host.send("运行中", "mate_dev", "conv-1")
    queued = await host.send("排队中", "mate_dev", "conv-1")
    other = await host.send("其他同事", "mate_ops", "conv-1")
    await settle(lambda: len(host.core.started) == 2)

    await host.manager.delete_colleague(NS, "mate_dev")

    assert host.service.deleted == [("mate_dev", 1)]
    assert host.status(running) == ("ended", "cancelled")
    assert host.status(queued) == ("ended", "cancelled")
    assert host.status(other) == ("dispatched", None)
    assert "排队中" not in host.core.started


async def test_requests_are_refused_while_a_colleague_is_deleted(host):
    await host.send("运行中")
    await settle(lambda: host.core.started == ["运行中"])
    host.core.abort_gate = asyncio.Event()
    deleting = asyncio.create_task(host.manager.delete_colleague(NS, "mate_dev"))
    await settle(lambda: host.core.aborts)

    with pytest.raises(PersistentAgentError) as excinfo:
        await host.send("新消息")
    assert excinfo.value.code == "AGENT_DELETING"

    host.core.abort_gate.set()
    await deleting
    accepted = await host.send("删除后")
    assert host.status(accepted)[0] in ("queued", "dispatched")


async def test_expired_window_data_is_purged_periodically(tmp_path):
    host = Host(tmp_path, archive_retention=timedelta(0), purge_interval=timedelta(milliseconds=10))
    backend = host.windows.bind(NS, "mate_dev", "sess-1")
    await backend.set("pa:mate_dev:sess-1:archive:1", "旧档案")
    await backend.set("pa:mate_dev:sess-1:summary", "摘要")

    await host.manager.start()
    try:
        for _ in range(100):
            if await backend.get("pa:mate_dev:sess-1:archive:1") is None:
                break
            await asyncio.sleep(0.01)
        assert await backend.get("pa:mate_dev:sess-1:archive:1") is None
        assert await backend.get("pa:mate_dev:sess-1:summary") == "摘要"
    finally:
        await host.manager.aclose()
