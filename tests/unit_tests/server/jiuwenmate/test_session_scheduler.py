"""JM-07: colleague manager, Session scheduler and Execution driver."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetAccountRef, BudgetRepository
from jiuwenswarm.server.jiuwenmate.execution_driver import outcome_of
from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot, ConversationRef
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.ports.workspace_access import WorkspaceStore
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
    def __init__(self, core_fake: FakeCore, workspaces: WorkspaceStore) -> None:
        self._core = core_fake
        self.workspaces = workspaces
        self.deleted: list[tuple[str, int]] = []
        self.refuse_snapshot = False
        self.disabled_agents: set[str] = set()

    def snapshot(self, namespace_id: str, agent_id: str, config_version: str):
        return publish_spec(
            agent_id, config_version, {"card": {"id": agent_id, "name": "同事"}, "enable_sys_operation": False}
        )

    def current_snapshot(self, namespace_id: str, agent_id: str):
        if self.refuse_snapshot:
            raise PersistentAgentError("colleague has no published configuration", code="NOT_PUBLISHED")
        return self.snapshot(namespace_id, agent_id, "v1")

    def get_colleague(self, namespace_id: str, agent_id: str):
        if agent_id in self.disabled_agents:
            return type("DisabledColleague", (), {"enabled": False})()
        return None

    def list_colleagues(self, namespace_id: str):
        return []

    def list_agents(self, namespace_id: str):
        return []

    def delete_colleague(self, namespace_id: str, agent_id: str, *, record_removal: bool = False) -> None:
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
        self.specs: dict[str, object] = {}
        self.inputs: dict[str, core.RequestInput] = {}
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
        self.specs[text] = spec
        self.inputs[text] = inputs
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
    def __init__(self, tmp_path, *, limits: SchedulerLimits | None = None, clock=None, **manager_kwargs) -> None:
        db = tmp_path / "agents.sqlite3"
        self.sessions = ExecutionRepository(db)
        self.windows = WindowBackendStore(db)
        self.budgets = BudgetRepository(db)
        self.core = FakeCore()
        self.service = FakeService(self.core, WorkspaceStore(tmp_path / "workspaces"))
        self.manager = AgentManager(
            self.service,
            self.sessions,
            self.windows,
            self.budgets,
            execute=self.core.execute,
            abort=self.core.abort,
            limits=limits,
            reservation_tokens=1000,
            clock=clock or (lambda: NOW),
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


def stub_invocation_builder(monkeypatch, addenda=None):
    from jiuwenswarm.server.jiuwenmate import execution_driver
    from jiuwenswarm.server.jiuwenmate.execution_models import build_execution_context

    calls = []

    class InvocationSpec(SimpleNamespace):
        def model_copy(self, *, update):
            return InvocationSpec(
                card=self.card,
                fingerprint=self.fingerprint,
                system_prompt=self.system_prompt,
                tools=update.get("tools", self.tools),
            )

    def build(snapshot, request, execution, *, ports, system_prompt_addendum=None):
        calls.append(request.session_id)
        if addenda is not None:
            addenda.append(system_prompt_addendum)
        context = SimpleNamespace(
            execution=build_execution_context(
                request, execution, capability_snapshot=snapshot.enabled_features,
            ),
            ports=ports,
            extras={},
            system_prompt_addendum=system_prompt_addendum,
        )
        return InvocationSpec(
            card=SimpleNamespace(id=request.agent_id),
            fingerprint=lambda: request.config_fingerprint,
            system_prompt=None,
            tools=[],
        ), context

    monkeypatch.setattr(execution_driver, "build_invocation", build)
    return calls


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


async def test_session_workspace_is_prepared_before_invocation_and_isolated(host, monkeypatch):
    invocation_calls = stub_invocation_builder(monkeypatch)
    states = [
        await host.send("会话一产物", conversation="conversation-a"),
        await host.send("会话二产物", conversation="conversation-b"),
    ]
    await settle(lambda: len(host.core.started) == 2)

    session_roots = []
    for text in ("会话一产物", "会话二产物"):
        context = host.core.contexts[text]
        session_root = (
            host.service.workspaces.path(context.execution.namespace_id, context.execution.agent_id)
            / "sessions"
            / context.execution.session_id
        )
        assert session_root.is_dir()
        session_roots.append(session_root)

    assert session_roots[0] != session_roots[1]
    assert set(invocation_calls) == {root.name for root in session_roots}
    host.core.release("会话一产物", "会话二产物")
    await host.scheduler.drain()
    assert [host.status(state) for state in states] == [("ended", "completed")] * 2


async def test_session_workspace_creation_failure_prevents_invocation(host, monkeypatch):
    invocation_calls = stub_invocation_builder(monkeypatch)

    def fail_to_prepare(*_args):
        raise PersistentAgentError("session workspace unavailable")

    monkeypatch.setattr(host.service.workspaces, "ensure_session", fail_to_prepare, raising=False)
    host.core.release("目录准备失败")
    state = await host.send("目录准备失败")
    await settle(lambda: host.status(state)[0] == "ended")

    assert host.core.started == []
    assert invocation_calls == []
    assert host.status(state) == ("ended", "failed")


async def test_invocation_guides_session_drafts_and_shared_final_output(host, monkeypatch):
    addenda = []
    stub_invocation_builder(monkeypatch, addenda)
    state = await host.send("整理文件")
    session_id = state.record.session_id
    host.core.release("整理文件")
    await host.scheduler.drain()

    assert host.status(state) == ("ended", "completed")
    guidance = addenda[0]
    assert f"sessions/{session_id}" in guidance
    assert ".lark-slides/" in guidance
    assert "output/" in guidance
    assert "share_file" in guidance


async def test_group_collaboration_roster_contains_only_published_active_members_of_that_group(tmp_path):
    host = Host(tmp_path)
    first = host.manager.group_chats.resolve(NS, "feishu", "chat-first", now=NOW)
    second = host.manager.group_chats.resolve(NS, "feishu", "chat-second", now=NOW)
    host.manager.group_chats.observe_access(
        NS, "mate_peer", first.group_chat_id, "app-peer", observed_at=NOW, source_ref="joined:peer"
    )
    host.manager.group_chats.observe_access(
        NS, "mate_elsewhere", second.group_chat_id, "app-other", observed_at=NOW, source_ref="joined:other"
    )
    host.manager.group_chats.update_membership(
        NS, "mate_left", first.group_chat_id, "app-left", status="left", observed_at=NOW, source_ref="left"
    )
    host.service.list_agents = lambda namespace_id: [
        SimpleNamespace(agent_id="mate_peer", name="可用同事", enabled=True, current_version="v1"),
        SimpleNamespace(agent_id="mate_elsewhere", name="其他群同事", enabled=True, current_version="v1"),
        SimpleNamespace(agent_id="mate_left", name="已离群同事", enabled=True, current_version="v1"),
        SimpleNamespace(agent_id="mate_unpublished", name="未发布同事", enabled=True, current_version=None),
    ]

    roster = host.manager.group_collaboration_roster(NS, first.group_chat_id, "mate_sender")

    assert roster == [{"agent_id": "mate_peer", "name": "可用同事", "app_ids": ["app-peer"]}]
    await host.manager.aclose()


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


async def test_resume_refuses_a_request_that_is_not_awaiting_continuation(host):
    state = await host.send("任务")
    with pytest.raises(PersistentAgentError) as excinfo:
        host.scheduler.resume(state)
    assert excinfo.value.code == "CONTINUATION_NOT_READY"


async def test_direct_message_gets_search_port_without_group_trigger_preflight(host, monkeypatch):
    from jiuwenswarm.server.jiuwenmate.ports.group_chat import HostGroupChatPort

    trigger_calls = []
    original = HostGroupChatPort.current_trigger

    async def record_trigger(self, execution, message_id):
        trigger_calls.append(message_id)
        return await original(self, execution, message_id)

    monkeypatch.setattr(HostGroupChatPort, "current_trigger", record_trigger)
    host.core.release("请搜索产品群")
    session = host.sessions.resolve_session(NS, "mate_dev", ConversationRef.web("private-search"), now=NOW)
    state, _ = await host.scheduler.accept(
        session,
        snapshot=host.service.snapshot(NS, "mate_dev", "v1"),
        source_ref="web:dm-1",
        access=ACCESS,
        request_input=core.RequestInput(
            kind="direct_message",
            text="请搜索产品群",
            source=core.DirectMessageSource(
                message_id="dm-1", sender_id="user-1", sender_name="用户", received_at=NOW,
            ),
            reply_target="private-search",
        ),
        received_at=NOW,
    )
    await settle(lambda: "请搜索产品群" in host.core.ended)
    await host.scheduler.drain()

    port = host.core.contexts["请搜索产品群"].ports.group_chat
    from jiuwenswarm.server.jiuwenmate.group_sharing_service import GuardedGroupChatPort

    assert isinstance(port, GuardedGroupChatPort)
    assert isinstance(port._delegate, HostGroupChatPort)
    assert port._delegate._search is not None and port._delegate._cursors is not None
    assert trigger_calls == []


async def test_scheduled_execution_does_not_get_group_search_port(host):
    state = await host.send("定时任务")
    host.core.release("定时任务")
    await settle(lambda: "定时任务" in host.core.ended)
    await host.scheduler.drain()

    assert host.core.contexts["定时任务"].ports.group_chat is None
    assert host.status(state) == ("ended", "completed")


async def test_group_message_keeps_current_trigger_preflight_and_search_port(host, monkeypatch):
    from jiuwenswarm.server.jiuwenmate.ports.group_chat import HostGroupChatPort

    trigger_calls = []
    original = HostGroupChatPort.current_trigger

    async def record_trigger(self, execution, message_id):
        trigger_calls.append(message_id)
        return await original(self, execution, message_id)

    monkeypatch.setattr(HostGroupChatPort, "current_trigger", record_trigger)
    group = host.manager.group_chats.resolve(NS, "feishu", "oc_group", now=NOW)
    trigger = core.GroupMessage(
        message_id="group-trigger",
        sender_id="user-1",
        sender_name="用户",
        sender_kind="human",
        received_at=NOW,
        text="群内提问",
    )
    host.manager.group_chats.observe_access(NS, "mate_dev", group.group_chat_id, "app-mate-dev",
                                            observed_at=NOW, source_ref="joined:group")
    host.manager.group_archive.append(NS, group.group_chat_id, trigger, source_ref="event:group-trigger")

    async def active(access, current_group):
        return "active"

    host.manager.verify_group_membership = active
    session = host.sessions.resolve_session(NS, "mate_dev", ConversationRef.group(group.group_chat_id), now=NOW)
    state, _ = await host.scheduler.accept(
        session,
        snapshot=host.service.snapshot(NS, "mate_dev", "v1"),
        source_ref="feishu:group-trigger",
        access=ACCESS,
        request_input=core.RequestInput(
            kind="group_message", text="群内提问", source=trigger, reply_target="oc_group",
        ),
        received_at=NOW,
    )
    host.core.release("群内提问")
    await settle(lambda: "群内提问" in host.core.ended)
    await host.scheduler.drain()

    port = host.core.contexts["群内提问"].ports.group_chat
    from jiuwenswarm.server.jiuwenmate.group_sharing_service import GuardedGroupChatPort

    assert isinstance(port, GuardedGroupChatPort)
    assert isinstance(port._delegate, HostGroupChatPort)
    assert port._delegate._search is not None and port._delegate._cursors is not None
    assert trigger_calls == ["group-trigger"]
    assert host.status(state) == ("ended", "completed")


async def test_group_execution_preserves_verified_bot_mention_after_trigger_refresh(host, monkeypatch):
    stub_invocation_builder(monkeypatch)
    group = host.manager.group_chats.resolve(NS, "feishu", "oc_group", now=NOW)
    archived_trigger = core.GroupMessage(
        message_id="mentioned-trigger",
        sender_id="user-1",
        sender_name="用户",
        sender_kind="human",
        received_at=NOW,
        text="请处理这个问题",
        mention_ids=("ou_feishu_bot",),
    )
    host.manager.group_chats.observe_access(
        NS, "mate_dev", group.group_chat_id, "app-mate-dev", observed_at=NOW, source_ref="joined:group"
    )
    host.manager.group_chats.observe_access(
        NS, "mate_peer", group.group_chat_id, "app-peer", observed_at=NOW, source_ref="joined:peer"
    )
    host.service.list_agents = lambda namespace_id: [
        SimpleNamespace(agent_id="mate_peer", name="动态同事", enabled=True, current_version="v1")
    ]
    host.manager.group_archive.append(
        NS, group.group_chat_id, archived_trigger, source_ref="event:mentioned-trigger"
    )

    async def active(access, current_group):
        return "active"

    host.manager.verify_group_membership = active
    session = host.sessions.resolve_session(NS, "mate_dev", ConversationRef.group(group.group_chat_id), now=NOW)
    request_source = archived_trigger.model_copy(update={"mention_ids": ("ou_feishu_bot", "mate_dev")})
    state, _ = await host.scheduler.accept(
        session,
        snapshot=host.service.snapshot(NS, "mate_dev", "v1"),
        source_ref="feishu:mentioned-trigger",
        access=ACCESS,
        request_input=core.RequestInput(
            kind="group_message", text="请处理这个问题", source=request_source, reply_target="oc_group",
        ),
        received_at=NOW,
    )
    host.core.release("请处理这个问题")
    await settle(lambda: "请处理这个问题" in host.core.ended)
    await host.scheduler.drain()

    assert host.status(state) == ("ended", "completed")
    assert host.core.inputs["请处理这个问题"].source.mention_ids == ("ou_feishu_bot", "mate_dev")
    spec = host.core.specs["请处理这个问题"]
    context = host.core.contexts["请处理这个问题"]
    assert spec.fingerprint() == context.execution.config_fingerprint
    assert spec.system_prompt is None
    assert "动态同事" in context.system_prompt_addendum
    assert "职责完全无关" in context.system_prompt_addendum
    assert f"sessions/{context.execution.session_id}" in context.system_prompt_addendum
    assert "output/" in context.system_prompt_addendum
    enriched_text = host.core.inputs["请处理这个问题"].text
    assert "请处理这个问题" in enriched_text


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
