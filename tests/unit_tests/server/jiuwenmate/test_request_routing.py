"""SW-03: Session lookup, group mapping, the Request queue and Execution records."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import count

import pytest
from jiuwenswarm.server.jiuwenmate.execution_models import (
    AccessSnapshot,
    ConversationRef,
    PersistentContractError,
)
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from jiuwenswarm.server.jiuwenmate.models import (
    CapabilitySelection,
    ColleagueSettings,
    PersistentAgentError,
)
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.repository import PersistentAgentRepository, TemplateStore
from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService
from jiuwenswarm.server.jiuwenmate.spec_provider import build_invocation

from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.harness import persistent_agent as core
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

NS = "workspace-1"
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
ACCESS = AccessSnapshot(
    principal_ref="user-1",
    startup_scope=frozenset({"chat"}),
    source_scope=frozenset({"chat"}),
    policy_revision="policy-1",
)


class FakeModels:
    def __init__(self, default="deepseek#0"):
        self.default = default

    def default_selection(self):
        return self.default

    def resolve(self, selection, *, client_id):
        return TeamModelConfig(
            model_client_config=ModelClientConfig(
                client_id=client_id,
                client_provider="OpenAI",
                api_key="sk-test",
                api_base="https://example.invalid/v1",
                model_name="deepseek",
            ),
            model_request_config=ModelRequestConfig(model="deepseek"),
        )


class FakePorts:
    async def record(self, execution, event):
        return None

    async def read_execution_log(self, execution, execution_id):
        return ()

    async def admit(self, execution, model_call_id):
        raise AssertionError("not called")

    async def report(self, execution, model_call_id, usage):
        raise AssertionError("not called")


class Host:
    """Colleague service plus the SW-03 repositories, all on one database file."""

    def __init__(self, tmp_path, models=None):
        ticks = count()
        db = tmp_path / "agents.sqlite3"
        self.agents = PersistentAgentService(
            PersistentAgentRepository(db),
            TemplateStore(tmp_path / "templates"),
            models=models or FakeModels(),
            template_source=lambda _: None,
            clock=lambda: NOW + timedelta(seconds=next(ticks)),
        )
        self.sessions = ExecutionRepository(db)
        self.groups = GroupChatRepository(db)

    def colleague(self, name="研发同事", namespace_id=NS) -> str:
        settings = ColleagueSettings(name=name, capabilities=CapabilitySelection(model="deepseek#0"))
        return self.agents.create_colleague(namespace_id, settings).agent_id

    def group_session(self, agent_id, platform_chat_id, *, platform="feishu", namespace_id=NS):
        group = self.groups.resolve(namespace_id, platform, platform_chat_id, now=NOW)
        conversation = ConversationRef.group(group.group_chat_id)
        return self.sessions.resolve_session(namespace_id, agent_id, conversation, now=NOW)

    def accept(self, session, source_ref, request_input=None):
        return self.sessions.accept_request(
            session,
            snapshot=self.agents.current_snapshot(session.namespace_id, session.agent_id),
            source_ref=source_ref,
            access=ACCESS,
            request_input=request_input or group_input(source_ref),
            received_at=NOW,
        )


def group_input(message_id: str, text: str = "@研发同事 看一下") -> core.RequestInput:
    return core.RequestInput(
        kind="group_message",
        text=text,
        source=core.GroupMessage(
            message_id=message_id,
            sender_id="user-1",
            sender_name="张三",
            sender_kind="human",
            received_at=NOW,
            text=text,
        ),
        reply_target="group-reply",
    )


def direct_input(message_id: str) -> core.RequestInput:
    return core.RequestInput(
        kind="direct_message",
        text="帮我整理进展",
        source=core.DirectMessageSource(message_id=message_id, sender_id="user-1", sender_name="张三", received_at=NOW),
        reply_target="direct-reply",
    )


def test_same_colleague_in_same_group_reuses_one_session(tmp_path):
    host = Host(tmp_path)
    agent = host.colleague()

    first = host.group_session(agent, "oc_chat_1")
    second = host.group_session(agent, "oc_chat_1")
    after_restart = Host(tmp_path).group_session(agent, "oc_chat_1")

    assert first.session_id == second.session_id == after_restart.session_id
    assert first.conversation.kind == "group"


def test_one_group_is_shared_while_each_colleague_has_its_own_session(tmp_path):
    host = Host(tmp_path)
    dev, qa = host.colleague("研发同事"), host.colleague("测试同事")

    dev_session = host.group_session(dev, "oc_chat_1")
    qa_session = host.group_session(qa, "oc_chat_1")
    other_group = host.group_session(dev, "oc_chat_2")

    assert dev_session.conversation == qa_session.conversation
    assert len({dev_session.session_id, qa_session.session_id, other_group.session_id}) == 3


def test_web_and_im_private_chats_are_separate_sessions(tmp_path):
    host = Host(tmp_path)
    agent = host.colleague()

    web = host.sessions.resolve_session(NS, agent, ConversationRef.web("conv-1"), now=NOW)
    web_new = host.sessions.resolve_session(NS, agent, ConversationRef.web("conv-2"), now=NOW)
    feishu = host.sessions.resolve_session(NS, agent, ConversationRef.im_direct("feishu", "conv-1"), now=NOW)
    group = host.group_session(agent, "conv-1")

    assert len({web.session_id, web_new.session_id, feishu.session_id, group.session_id}) == 4


def test_same_platform_chat_in_different_namespaces_is_isolated(tmp_path):
    host = Host(tmp_path)
    agent_a = host.colleague(namespace_id="workspace-a")
    agent_b = host.colleague(namespace_id="workspace-b")

    group_a = host.groups.resolve("workspace-a", "feishu", "oc_chat_1", now=NOW)
    group_b = host.groups.resolve("workspace-b", "feishu", "oc_chat_1", now=NOW)
    session_a = host.group_session(agent_a, "oc_chat_1", namespace_id="workspace-a")
    session_b = host.group_session(agent_b, "oc_chat_1", namespace_id="workspace-b")

    assert group_a.group_chat_id != group_b.group_chat_id
    assert session_a.session_id != session_b.session_id
    with pytest.raises(PersistentAgentError) as caught:
        host.sessions.get_session("workspace-b", session_a.session_id)
    assert caught.value.code == "NOT_FOUND"


def test_long_or_ambiguous_conversation_ids_do_not_collide(tmp_path):
    host = Host(tmp_path)
    agent = host.colleague()
    prefix = "oc_" + "x" * 500
    refs = [
        ConversationRef.im_direct("feishu", prefix + "1"),
        ConversationRef.im_direct("feishu", prefix + "2"),
        ConversationRef.im_direct("a:b", "c"),
        ConversationRef.im_direct("a", "b:c"),
        ConversationRef.im_direct("feishu", "chat 1"),
        ConversationRef.im_direct("feishu", "chat_1"),
    ]

    sessions = [host.sessions.resolve_session(NS, agent, ref, now=NOW) for ref in refs]

    assert len({session.session_id for session in sessions}) == len(refs)
    assert [session.conversation for session in sessions] == refs
    long_groups = {host.groups.resolve(NS, "feishu", prefix + suffix, now=NOW).group_chat_id for suffix in "12"}
    assert len(long_groups) == 2


def test_supplementary_message_is_a_new_request_in_the_same_session(tmp_path):
    host = Host(tmp_path)
    session = host.group_session(host.colleague(), "oc_chat_1")

    first, created_first = host.accept(session, "om_1")
    second, created_second = host.accept(session, "om_2", group_input("om_2", "补充：只看接口部分"))

    assert created_first and created_second
    assert first.record.request_id != second.record.request_id
    assert first.record.session_id == second.record.session_id == session.session_id
    assert (first.seq, second.seq) == (1, 2)
    assert [state.record.request_id for state in host.sessions.session_requests(NS, session.session_id)] == [
        first.record.request_id,
        second.record.request_id,
    ]


def test_redelivered_source_event_returns_the_stored_request(tmp_path):
    host = Host(tmp_path)
    session = host.group_session(host.colleague(), "oc_chat_1")

    first, _ = host.accept(session, "om_1")
    again, created = host.accept(session, "om_1")

    assert not created
    assert again.record == first.record
    assert len(host.sessions.session_requests(NS, session.session_id)) == 1


def test_same_source_event_for_two_colleagues_creates_two_requests(tmp_path):
    host = Host(tmp_path)
    dev, qa = host.colleague("研发同事"), host.colleague("测试同事")

    dev_request, dev_created = host.accept(host.group_session(dev, "oc_chat_1"), "om_1")
    qa_request, qa_created = host.accept(host.group_session(qa, "oc_chat_1"), "om_1")

    assert dev_created and qa_created
    assert dev_request.record.request_id != qa_request.record.request_id


def test_request_is_pinned_to_the_published_version(tmp_path):
    host = Host(tmp_path)
    agent = host.colleague()
    session = host.group_session(agent, "oc_chat_1")
    snapshot = host.agents.current_snapshot(NS, agent)
    host.agents.update_settings(
        NS, agent, ColleagueSettings(name="草稿名称", capabilities=CapabilitySelection(model="deepseek#0"))
    )

    state, _ = host.accept(session, "om_1")

    assert state.record.config_version == snapshot.config_version == "v1"
    assert state.record.config_fingerprint == snapshot.fingerprint


def test_request_kind_must_match_the_session(tmp_path):
    host = Host(tmp_path)
    agent = host.colleague()
    group = host.group_session(agent, "oc_chat_1")
    web = host.sessions.resolve_session(NS, agent, ConversationRef.web("conv-1"), now=NOW)

    with pytest.raises(PersistentContractError) as caught:
        host.accept(group, "msg_1", direct_input("msg_1"))
    assert caught.value.code == "REQUEST_SOURCE_MISMATCH"
    with pytest.raises(PersistentContractError):
        host.accept(web, "om_1", group_input("om_1"))
    scheduled, _ = host.accept(
        group, "plan-1:2026-10-12", core.RequestInput(kind="scheduled", text="早报", reply_target="group-reply")
    )
    assert scheduled.status == "queued"


def test_snapshot_of_another_colleague_is_rejected(tmp_path):
    host = Host(tmp_path)
    dev, qa = host.colleague("研发同事"), host.colleague("测试同事")
    session = host.group_session(dev, "oc_chat_1")

    with pytest.raises(PersistentContractError):
        host.sessions.accept_request(
            session,
            snapshot=host.agents.current_snapshot(NS, qa),
            source_ref="om_1",
            access=ACCESS,
            request_input=group_input("om_1"),
            received_at=NOW,
        )


def test_session_dispatches_one_request_at_a_time(tmp_path):
    host = Host(tmp_path)
    session = host.group_session(host.colleague(), "oc_chat_1")
    first, _ = host.accept(session, "om_1")
    second, _ = host.accept(session, "om_2", group_input("om_2"))

    execution = host.sessions.start_execution(NS, first.record.request_id, now=NOW)
    with pytest.raises(PersistentAgentError) as busy:
        host.sessions.start_execution(NS, second.record.request_id, now=NOW)
    with pytest.raises(PersistentAgentError) as again:
        host.sessions.start_execution(NS, first.record.request_id, now=NOW)

    assert busy.value.code == "SESSION_BUSY"
    assert again.value.code == "CONFLICT"
    assert host.sessions.executions(NS, first.record.request_id) == [execution]

    host.sessions.finish_request(NS, first.record.request_id, "completed", now=NOW)
    host.sessions.start_execution(NS, second.record.request_id, now=NOW)
    assert host.sessions.get_request(NS, second.record.request_id).status == "dispatched"


def test_queue_and_dispatched_requests_survive_restart(tmp_path):
    host = Host(tmp_path)
    agent = host.colleague()
    session = host.group_session(agent, "oc_chat_1")
    web = host.sessions.resolve_session(NS, agent, ConversationRef.web("conv-1"), now=NOW)
    done, _ = host.accept(session, "om_0")
    running, _ = host.accept(session, "om_1", group_input("om_1"))
    waiting, _ = host.accept(session, "om_2", group_input("om_2"))
    web_waiting, _ = host.accept(web, "msg_1", direct_input("msg_1"))
    host.sessions.start_execution(NS, done.record.request_id, now=NOW)
    host.sessions.finish_request(NS, done.record.request_id, "completed", now=NOW)
    execution = host.sessions.start_execution(NS, running.record.request_id, now=NOW)

    restarted = Host(tmp_path).sessions
    pending = restarted.pending_requests(NS)

    by_id = {state.record.request_id: state for state in pending}
    assert set(by_id) == {running.record.request_id, waiting.record.request_id, web_waiting.record.request_id}
    assert by_id[running.record.request_id].status == "dispatched"
    assert by_id[waiting.record.request_id].status == "queued"
    assert by_id[waiting.record.request_id].record == waiting.record
    assert restarted.executions(NS, running.record.request_id) == [execution]
    group_order = [state.seq for state in pending if state.record.session_id == session.session_id]
    assert group_order == [2, 3]


def test_finish_request_is_idempotent_and_keeps_the_outcome(tmp_path):
    host = Host(tmp_path)
    session = host.group_session(host.colleague(), "oc_chat_1")
    state, _ = host.accept(session, "om_1")

    host.sessions.finish_request(NS, state.record.request_id, "ignored", now=NOW)
    repeated = host.sessions.finish_request(NS, state.record.request_id, "ignored", now=NOW)

    assert (repeated.status, repeated.outcome) == ("ended", "ignored")
    with pytest.raises(PersistentAgentError) as caught:
        host.sessions.finish_request(NS, state.record.request_id, "failed", now=NOW)
    assert caught.value.code == "CONFLICT"
    with pytest.raises(PersistentAgentError):
        host.sessions.finish_request(NS, state.record.request_id, "done", now=NOW)


def test_unpublished_colleague_cannot_accept_requests(tmp_path):
    host = Host(tmp_path, models=FakeModels(default=None))
    default = host.agents.ensure_default_colleague(NS)
    session = host.group_session(default.agent_id, "oc_chat_1")

    with pytest.raises(PersistentAgentError) as caught:
        host.accept(session, "om_1")

    assert caught.value.code == "NOT_PUBLISHED"
    assert host.sessions.pending_requests(NS) == []


def test_stored_records_rebuild_a_valid_core_invocation(tmp_path):
    host = Host(tmp_path)
    agent = host.colleague()
    session = host.group_session(agent, "oc_chat_1")
    state, _ = host.accept(session, "om_1")
    execution = host.sessions.start_execution(NS, state.record.request_id, now=NOW)

    restarted = Host(tmp_path)
    request = restarted.sessions.get_request(NS, state.record.request_id).record
    snapshot = restarted.agents.snapshot(NS, agent, request.config_version)
    windows = WindowBackendStore(tmp_path / "agents.sqlite3").bind(NS, agent, session.session_id)
    ports = core.PersistentPorts(commit=FakePorts(), budget=FakePorts(), window_backend=windows)
    spec, context = build_invocation(snapshot, request, execution, ports=ports)

    assert context.execution.session_id == session.session_id
    assert context.execution.request_id == request.request_id
    assert context.execution.execution_id == execution.execution_id
    assert context.execution.source_ref == "om_1"
    assert spec.card.id == agent
