"""JM-08: route bound Feishu events into the persistent request path only."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from itertools import count
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect import (
    FeishuChannel,
    FeishuConfig,
)
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetRepository
from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot, ConversationRef
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from jiuwenswarm.server.jiuwenmate.models import CapabilitySelection, ColleagueSettings
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.repository import PersistentAgentRepository, TemplateStore
from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService

from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.harness import persistent_agent as core
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import (
    FeishuBinding,
    FeishuInboundEvent,
    InboundDisposition,
    PersistentGroupFanout,
)

NS = "workspace-1"
NOW = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
ACCESS = AccessSnapshot(
    principal_ref="feishu:operator:trusted",
    startup_scope=frozenset({"chat"}),
    source_scope=frozenset({"chat"}),
    policy_revision="policy-1",
)


class _FakeModels:
    def default_selection(self):
        return "deepseek#0"

    def resolve(self, selection, *, client_id):
        return TeamModelConfig(
            model_client_config=ModelClientConfig(
                client_id=client_id,
                client_provider="OpenAI",
                api_key="test-only",
                api_base="https://example.invalid/v1",
                model_name="deepseek",
            ),
            model_request_config=ModelRequestConfig(model="deepseek"),
        )


async def _complete_one_request(spec, context, inputs):
    """Finish immediately so the real scheduler can exercise its acceptance path."""
    yield core.AgentEvent.create(
        context.execution,
        event_type=core.EventType.EXECUTION_FINAL,
        local_seq=1,
        occurred_at=NOW,
        payload={"status": "completed", "output_refs": ()},
    )


class _Host:
    def __init__(self, tmp_path):
        self.db = tmp_path / "jiuwenmate.sqlite3"
        self.sessions = ExecutionRepository(self.db)
        self.groups = GroupChatRepository(self.db)
        self.windows = WindowBackendStore(self.db)
        self.budgets = BudgetRepository(self.db)
        ids = count(1)
        self.service = PersistentAgentService(
            PersistentAgentRepository(self.db),
            TemplateStore(tmp_path / "templates"),
            models=_FakeModels(),
            template_source=lambda _template_id: None,
            clock=lambda: NOW + timedelta(seconds=next(ids)),
            id_factory=lambda: f"mate_{next(ids)}",
            windows=self.windows,
        )
        self.manager = AgentManager(
            self.service,
            self.sessions,
            self.windows,
            self.budgets,
            execute=_complete_one_request,
            clock=lambda: NOW,
        )
        self.manager.verify_group_membership = AsyncMock(return_value="active")

    async def start(self):
        await self.manager.start()

    async def close(self):
        await self.manager.aclose()

    def colleague(self, name: str) -> str:
        return self.service.create_colleague(
            NS,
            ColleagueSettings(name=name, capabilities=CapabilitySelection(model="deepseek#0")),
        ).agent_id

    def fanout(self, bindings, *, access_resolver=None, binding_resolver=None):
        rows = tuple(bindings)

        def resolve(platform, app_id):
            if binding_resolver is not None:
                return binding_resolver(platform, app_id)
            return tuple(item for item in rows if item.platform == platform and item.app_id == app_id)

        return PersistentGroupFanout(
            service=self.service,
            groups=self.groups,
            sessions=self.sessions,
            agent_manager=self.manager,
            resolve_bindings=resolve,
            resolve_access=access_resolver or (lambda _binding, _event: ACCESS),
            clock=lambda: NOW,
        )

    def row_counts(self) -> tuple[int, int, int]:
        with sqlite3.connect(self.db) as conn:
            return tuple(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("group_chats", "sessions", "requests")
            )

    def request_states(self, agent_id: str, event: FeishuInboundEvent):
        if event.chat_type == "group":
            group = self.groups.resolve(NS, "feishu", event.chat_id, now=NOW)
            conversation = ConversationRef.group(group.group_chat_id)
        else:
            conversation = ConversationRef.im_direct("feishu", event.chat_id)
        session = self.sessions.resolve_session(NS, agent_id, conversation, now=NOW)
        return self.sessions.session_requests(NS, session.session_id)


@pytest.fixture
async def host(tmp_path):
    instance = _Host(tmp_path)
    await instance.start()
    yield instance
    await instance.close()


def _event(**overrides) -> FeishuInboundEvent:
    fields = {
        "receiving_app_id": "app-dev",
        "message_id": "om_001",
        "chat_id": "oc_project",
        "chat_type": "group",
        "sender_type": "user",
        "sender_id": "ou_alice",
        "sender_name": "张三",
        "sender_app_id": None,
        "received_at": NOW,
        "text": "请看一下",
        "mention_ids": ("ou_dev",),
        "reply_to": "om_parent",
        "attachment_refs": (),
    }
    fields.update(overrides)
    return FeishuInboundEvent(**fields)


def _binding(app_id: str, agent_id: str, *, binding_id: str | None = None) -> FeishuBinding:
    return FeishuBinding(
        binding_id=binding_id or f"binding-{app_id}",
        platform="feishu",
        app_id=app_id,
        namespace_id=NS,
        agent_id=agent_id,
    )


@pytest.mark.asyncio
async def test_disabled_colleague_claims_a_feishu_message_without_starting_work(host):
    agent_id = host.colleague("研发同事")
    host.service.set_enabled(NS, agent_id, enabled=False)
    fanout = host.fanout([_binding("app-dev", agent_id)])

    assert await fanout.handle(_event()) is InboundDisposition.HANDLED
    assert host.row_counts() == (0, 0, 0)


@pytest.mark.asyncio
async def test_unbound_event_is_left_for_the_personal_route_without_creating_state(host):
    fanout = host.fanout([])

    disposition = await fanout.handle(_event())

    assert disposition is InboundDisposition.UNCLAIMED
    assert host.row_counts() == (0, 0, 0)


@pytest.mark.asyncio
async def test_jm11_archives_own_echo_before_suppressing_dispatch(host):
    from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive

    agent_id = host.colleague("研发同事")
    fanout = host.fanout([_binding("app-dev", agent_id)])
    source = _event(sender_type="bot", sender_app_id="app-dev")
    assert await fanout.handle(source) is InboundDisposition.HANDLED
    group = host.groups.resolve(NS, "feishu", source.chat_id, now=NOW)
    archive = ConversationArchive(host.db.parent / "group_messages", host.groups)
    page = archive.recent(NS, group.group_chat_id, cursor=None, limit=20, visible_from=NOW)
    assert page.messages[0].message_id == "om_001" and page.messages[0].sender_kind == "agent"
    assert not host.request_states(agent_id, source)


@pytest.mark.asyncio
async def test_jm11_two_agents_share_archive_but_not_request_dedup(host):
    from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive

    first, second = host.colleague("A"), host.colleague("B")
    fanout = host.fanout([_binding("app-a", first), _binding("app-b", second)])
    await fanout.handle(_event(receiving_app_id="app-a"))
    await fanout.handle(_event(receiving_app_id="app-b"))
    group = host.groups.resolve(NS, "feishu", "oc_project", now=NOW)
    archive = ConversationArchive(host.db.parent / "group_messages", host.groups)
    page = archive.recent(NS, group.group_chat_id, cursor=None, limit=20, visible_from=NOW)
    assert [item.message_id for item in page.messages] == ["om_001"]
    assert len(host.request_states(first, _event())) == len(host.request_states(second, _event())) == 1


@pytest.mark.asyncio
async def test_jm11_recall_before_original_does_not_create_a_request(host):
    agent_id = host.colleague("A")
    fanout = host.fanout([_binding("app-dev", agent_id)])
    await fanout.handle(_event(event_kind="recalled", event_id="recall-first", changed_at=NOW))
    assert await fanout.handle(_event()) is InboundDisposition.HANDLED
    assert not host.request_states(agent_id, _event())


@pytest.mark.asyncio
async def test_jm11_rebinding_retires_only_unconfigured_bot_access(host):
    agent_id = host.colleague("A")
    bindings = {"app-dev": _binding("app-dev", agent_id)}
    fanout = host.fanout([], binding_resolver=lambda platform, app: (bindings[app],) if app in bindings else ())
    await fanout.handle(_event())
    bindings.clear()
    bindings["app-new"] = _binding("app-new", agent_id)
    await fanout.handle(_event(receiving_app_id="app-new", message_id="om_new"))
    group = host.groups.resolve(NS, "feishu", "oc_project", now=NOW)
    assert host.groups.access_for(NS, agent_id, group.group_chat_id).app_id == "app-new"
    bindings.clear()
    bindings["app-dev"] = _binding("app-dev", agent_id)
    assert await fanout.handle(_event(message_id="om_back")) is InboundDisposition.HANDLED
    assert host.groups.access_for(NS, agent_id, group.group_chat_id).app_id == "app-dev"


@pytest.mark.asyncio
async def test_jm11_recall_and_left_events_update_records_without_requests(host):
    agent_id = host.colleague("A")
    fanout = host.fanout([_binding("app-dev", agent_id)])
    await fanout.handle(_event())
    group = host.groups.resolve(NS, "feishu", "oc_project", now=NOW)
    for kind in ("recalled", "left"):
        changed = _event(event_kind=kind, event_id=f"event-{kind}", changed_at=NOW + timedelta(seconds=1))
        assert await fanout.handle(changed) is InboundDisposition.HANDLED
    page = host.manager.group_archive.recent(NS, group.group_chat_id, cursor=None, limit=20, visible_from=NOW)
    assert page.messages[0].text is None and page.messages[0].status == "recalled"
    assert host.groups.access_for(NS, agent_id, group.group_chat_id).status == "left"
    assert len(host.request_states(agent_id, _event())) == 1


@pytest.mark.asyncio
async def test_group_event_maps_core_fields_and_persists_one_request(host):
    agent_id = host.colleague("研发同事")
    fanout = host.fanout([_binding("app-dev", agent_id)])
    source = _event(attachment_refs=("feishu:attachment:om_001:file_key_1",))

    disposition = await fanout.handle(source)
    states = host.request_states(agent_id, source)
    request = states[0].record.request_input

    assert disposition is InboundDisposition.HANDLED
    assert len(states) == 1
    assert request.kind == "group_message"
    assert request.text == "请看一下"
    assert request.reply_target == "oc_project"
    assert request.attachment_refs == ("feishu:attachment:om_001:file_key_1",)
    assert request.source.message_id == "om_001"
    assert request.source.sender_id == "ou_alice"
    assert request.source.sender_name == "张三"
    assert request.source.sender_kind == "human"
    assert request.source.received_at == NOW
    assert request.source.text == "请看一下"
    assert request.source.mention_ids == ("ou_dev",)
    assert request.source.reply_to == "om_parent"
    assert states[0].record.source_ref == "feishu:message:om_001"
    route = states[0].record.route_snapshot
    assert (route.channel, route.app_id, route.target_id) == ("feishu", "app-dev", "oc_project")
    assert (route.source_message_id, route.source_reply_to) == ("om_001", "om_parent")


@pytest.mark.asyncio
async def test_receiver_bot_mention_adds_internal_agent_id_only_to_request_source(host):
    agent_id = host.colleague("研发同事")
    fanout = host.fanout([_binding("app-dev", agent_id)])
    source = _event(receiver_bot_mentioned=True)

    assert await fanout.handle(source) is InboundDisposition.HANDLED

    request = host.request_states(agent_id, source)[0].record.request_input
    group = host.groups.resolve(NS, "feishu", source.chat_id, now=NOW)
    archived, = host.manager.group_archive.current_messages(NS, group.group_chat_id, (source.message_id,))
    assert request.source.mention_ids == ("ou_dev", agent_id)
    assert archived.mention_ids == ("ou_dev",), "shared archive keeps Feishu's original mention IDs"


@pytest.mark.asyncio
async def test_later_named_colleague_waits_for_the_first_public_reply(host):
    life = host.colleague("生活小助手")
    study = host.colleague("agent学习小助手")
    fanout = host.fanout([_binding("app-life", life), _binding("app-study", study)])
    group = host.groups.resolve(NS, "feishu", "oc_project", now=NOW)
    for agent_id, app_id in ((life, "app-life"), (study, "app-study")):
        host.groups.update_membership(
            NS, agent_id, group.group_chat_id, app_id,
            status="active", observed_at=NOW, source_ref=f"test:{app_id}",
        )
    text = "生活小助手你再想一个生活中的痛点，然后让Agent学习助手想一下怎么用Agent解决"
    life_event = _event(receiving_app_id="app-life", text=text, mention_ids=(), reply_to=None)
    study_event = _event(receiving_app_id="app-study", text=text, mention_ids=(), reply_to=None)

    assert await fanout.handle(life_event) is InboundDisposition.HANDLED
    assert await fanout.handle(study_event) is InboundDisposition.HANDLED

    assert len(host.request_states(life, life_event)) == 1
    assert host.request_states(study, study_event) == []

    mentioned = _event(
        receiving_app_id="app-study",
        message_id="om_reply",
        text="这个痛点交给你",
        mention_ids=("ou_study",),
        receiver_bot_mentioned=True,
        reply_to="om_001",
        sender_type="bot",
        sender_id=life,
        sender_name="生活小助手",
        sender_app_id="app-life",
    )
    assert await fanout.handle(mentioned) is InboundDisposition.HANDLED
    assert len(host.request_states(study, mentioned)) == 1


@pytest.mark.asyncio
async def test_a_pure_greeting_is_accepted_only_for_one_daily_colleague(host):
    from dataclasses import replace

    life = host.colleague("生活小助手")
    study = host.colleague("agent学习小助手")
    fanout = host.fanout([_binding("app-life", life), _binding("app-study", study)])
    group = host.groups.resolve(NS, "feishu", "oc_project", now=NOW)
    for agent_id, app_id in ((life, "app-life"), (study, "app-study")):
        host.groups.update_membership(
            NS, agent_id, group.group_chat_id, app_id,
            status="active", observed_at=NOW, source_ref=f"test:{app_id}",
        )
    life_event = _event(receiving_app_id="app-life", text="大家好", mention_ids=(), reply_to=None)
    study_event = _event(receiving_app_id="app-study", text="大家好", mention_ids=(), reply_to=None)

    assert await fanout.handle(study_event) is InboundDisposition.HANDLED
    assert await fanout.handle(life_event) is InboundDisposition.HANDLED
    assert len(host.request_states(life, life_event)) == 1
    assert host.request_states(study, study_event) == []

    task = _event(
        receiving_app_id="app-study", message_id="om_task", text="你好，推荐一个南京的景点",
        mention_ids=(), reply_to=None,
    )
    assert await fanout.handle(task) is InboundDisposition.HANDLED
    assert len(host.request_states(study, task)) == 1

    settings = host.service.read_settings(NS, life)
    host.service.update_settings(NS, life, replace(settings, group_reply_mode="passive"))
    greeted = _event(
        receiving_app_id="app-study", message_id="om_again", text="在吗", mention_ids=(), reply_to=None,
    )
    life_again = _event(
        receiving_app_id="app-life", message_id="om_again", text="在吗", mention_ids=(), reply_to=None,
    )
    assert await fanout.handle(greeted) is InboundDisposition.HANDLED
    assert await fanout.handle(life_again) is InboundDisposition.HANDLED
    assert any(state.record.source_ref == "feishu:message:om_again" for state in host.request_states(study, greeted))
    assert all(state.record.source_ref != "feishu:message:om_again" for state in host.request_states(life, life_again))


@pytest.mark.asyncio
async def test_delivered_handoff_starts_only_the_named_colleague(host):
    life = host.colleague("生活小助手")
    study = host.colleague("agent学习小助手")
    fanout = host.fanout([_binding("app-life", life), _binding("app-study", study)])

    await fanout.continue_handoff(
        {
            "namespace_id": NS,
            "agent_id": life,
            "is_direct_message": False,
            "target_id": "oc_project",
            "app_id": "app-life",
            "source_message_id": "om_user",
            "text": f"到期日记不住，交给你 [[agent:{study}]]",
        },
        [{"agent_id": study, "name": "agent学习小助手", "app_ids": ["app-study"]}],
        "om_reply",
    )

    reply = _event(receiving_app_id="app-study", message_id="om_reply", chat_id="oc_project")
    states = host.request_states(study, reply)
    assert len(states) == 1
    request = states[0].record.request_input
    assert study in request.source.mention_ids
    assert request.source.reply_to == "om_user"
    assert "到期日记不住" in request.text
    assert f"[[agent:{study}]]" not in request.text
    assert host.request_states(life, reply) == []


@pytest.mark.asyncio
async def test_delivered_handoff_starts_when_delivery_already_archived_the_reply(host):
    life = host.colleague("生活小助手")
    study = host.colleague("agent学习小助手")
    fanout = host.fanout([_binding("app-life", life), _binding("app-study", study)])
    seeded = _event(
        receiving_app_id="app-life",
        message_id="om_reply",
        sender_type="bot",
        sender_id=life,
        sender_name="生活小助手",
        sender_app_id="app-life",
        text=f"到期日记不住，交给你 [[agent:{study}]]",
        mention_ids=(),
        reply_to="om_user",
    )

    assert await fanout.handle(seeded) is InboundDisposition.HANDLED
    assert host.request_states(life, seeded) == []

    await fanout.continue_handoff(
        {
            "namespace_id": NS,
            "agent_id": life,
            "is_direct_message": False,
            "target_id": "oc_project",
            "app_id": "app-life",
            "source_message_id": "om_user",
            "text": f"到期日记不住，交给你 [[agent:{study}]]",
        },
        [{"agent_id": study, "name": "agent学习小助手", "app_ids": ["app-study"]}],
        "om_reply",
    )

    reply = _event(receiving_app_id="app-study", message_id="om_reply", chat_id="oc_project")
    states = host.request_states(study, reply)
    assert len(states) == 1
    request = states[0].record.request_input
    assert study in request.source.mention_ids
    assert "到期日记不住" in request.text
    assert f"[[agent:{study}]]" not in request.text
    assert "@agent学习小助手" in request.text


@pytest.mark.asyncio
async def test_configured_agent_mismatch_is_rejected_without_personal_fallback(host):
    agent_id = host.colleague("研发同事")
    fanout = host.fanout([_binding("app-dev", agent_id)])

    disposition = await fanout.handle(_event(expected_agent_id="mate_stale"))

    assert disposition is InboundDisposition.REJECTED
    assert host.row_counts() == (0, 0, 0)


@pytest.mark.asyncio
async def test_attachment_only_request_uses_placeholder_without_fabricating_source_text(host):
    agent_id = host.colleague("研发同事")
    fanout = host.fanout([_binding("app-dev", agent_id)])
    source = _event(text=None, attachment_refs=("feishu:attachment:om_001:image_key_1",))

    assert await fanout.handle(source) is InboundDisposition.HANDLED
    request = host.request_states(agent_id, source)[0].record.request_input

    assert request.text == "[附件消息]"
    assert request.source.text is None
    assert request.attachment_refs == ("feishu:attachment:om_001:image_key_1",)


@pytest.mark.asyncio
async def test_direct_message_uses_an_isolated_im_session(host):
    agent_id = host.colleague("研发同事")
    fanout = host.fanout([_binding("app-dev", agent_id)])
    source = _event(chat_type="p2p", chat_id="ou_alice", mention_ids=(), reply_to=None)

    assert await fanout.handle(source) is InboundDisposition.HANDLED
    states = host.request_states(agent_id, source)
    request = states[0].record.request_input
    assert request.kind == "direct_message"
    assert request.reply_target == "ou_alice"
    assert request.source.message_id == "om_001"
    assert request.source.sender_id == "ou_alice"
    assert request.source.received_at == NOW
    assert states[0].record.source_ref == "feishu:message:om_001"


@pytest.mark.asyncio
async def test_redelivery_is_idempotent_per_agent_but_shared_event_reaches_each_bot(host):
    dev_id = host.colleague("研发同事")
    qa_id = host.colleague("测试同事")
    dev_event = _event(receiving_app_id="app-dev")
    qa_event = _event(receiving_app_id="app-qa")
    fanout = host.fanout([_binding("app-dev", dev_id), _binding("app-qa", qa_id)])

    first = await fanout.handle(dev_event)
    duplicate = await fanout.handle(dev_event)
    other_bot = await fanout.handle(qa_event)

    assert (first, duplicate, other_bot) == (
        InboundDisposition.HANDLED,
        InboundDisposition.HANDLED,
        InboundDisposition.HANDLED,
    )
    assert len(host.request_states(dev_id, dev_event)) == 1
    assert len(host.request_states(qa_id, qa_event)) == 1
    assert host.request_states(dev_id, dev_event)[0].record.request_id != host.request_states(
        qa_id, qa_event
    )[0].record.request_id
    assert host.row_counts() == (1, 2, 2)


@pytest.mark.asyncio
async def test_concurrent_redelivery_creates_only_one_request_for_the_same_agent(host):
    agent_id = host.colleague("研发同事")
    fanout = host.fanout([_binding("app-dev", agent_id)])
    source = _event()

    dispositions = await asyncio.gather(*(fanout.handle(source) for _ in range(4)))

    assert dispositions == [InboundDisposition.HANDLED] * 4
    assert len(host.request_states(agent_id, source)) == 1
    assert host.row_counts() == (1, 1, 1)


@pytest.mark.asyncio
async def test_binding_ambiguity_and_lookup_error_fail_closed(host):
    agent_id = host.colleague("研发同事")
    duplicate_bindings = [_binding("app-dev", agent_id, binding_id="one"),
                          _binding("app-dev", agent_id, binding_id="two")]
    ambiguous = host.fanout(duplicate_bindings)
    broken = host.fanout(
        [], binding_resolver=lambda _platform, _app_id: (_ for _ in ()).throw(RuntimeError("private detail"))
    )

    assert await ambiguous.handle(_event()) is InboundDisposition.REJECTED
    assert await broken.handle(_event()) is InboundDisposition.REJECTED
    assert host.row_counts() == (0, 0, 0)


@pytest.mark.asyncio
async def test_missing_access_snapshot_rejects_before_group_or_session_creation(host):
    agent_id = host.colleague("研发同事")
    fanout = host.fanout(
        [_binding("app-dev", agent_id)], access_resolver=lambda _binding, _event: None
    )

    disposition = await fanout.handle(_event())

    assert disposition is InboundDisposition.REJECTED
    assert host.row_counts() == (0, 0, 0)


@pytest.mark.asyncio
async def test_missing_published_configuration_rejects_before_group_or_session_creation(host):
    fanout = host.fanout([_binding("app-dev", "deleted-or-unpublished-agent")])

    disposition = await fanout.handle(_event())

    assert disposition is InboundDisposition.REJECTED
    assert host.row_counts() == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"receiving_app_id": " "},
        {"message_id": None},
        {"received_at": None},
        {"chat_id": None},
        {"chat_type": "unknown"},
        {"sender_type": None},
        {"sender_type": "system"},
        {"sender_id": None},
        {"sender_name": None},
        {"text": None, "attachment_refs": ()},
    ],
)
async def test_malformed_bound_events_are_rejected_without_persisting_state(host, overrides):
    agent_id = host.colleague("研发同事")
    fanout = host.fanout([_binding("app-dev", agent_id)])

    disposition = await fanout.handle(_event(**overrides))

    assert disposition is InboundDisposition.REJECTED
    assert host.row_counts() == (0, 0, 0)


@pytest.mark.asyncio
async def test_current_bot_echo_is_suppressed_but_unidentified_bot_is_not_guessed_to_be_self(host):
    agent_id = host.colleague("研发同事")
    fanout = host.fanout([_binding("app-dev", agent_id)])

    own_echo = await fanout.handle(
        _event(sender_type="bot", sender_id="ou_own_bot", sender_app_id="app-dev")
    )
    unidentified_bot = await fanout.handle(
        _event(message_id="om_other_bot", sender_type="bot", sender_id="ou_other_bot", sender_app_id=None)
    )

    assert own_echo is InboundDisposition.HANDLED
    assert unidentified_bot is InboundDisposition.HANDLED
    request = host.request_states(agent_id, _event(message_id="om_other_bot"))[0].record.request_input
    assert request.source.sender_kind == "agent"
    assert len(host.request_states(agent_id, _event(message_id="om_other_bot"))) == 1
    assert host.row_counts() == (1, 1, 1)


def _feishu_channel(app_id="app-dev", agent_id="") -> FeishuChannel:
    channel = FeishuChannel(
        FeishuConfig(enabled=True, app_id=app_id, agent_id=agent_id, channel_id="test-feishu"),
        MagicMock(),
    )
    channel._add_reaction = AsyncMock()
    channel._parse_message_content_with_file = AsyncMock(return_value=("legacy text", None))
    channel._process_batched_message = AsyncMock()
    return channel


@pytest.mark.asyncio
async def test_jm09_feishu_result_replies_to_source_with_stable_uuid_and_receipt():
    channel = _feishu_channel()
    response = SimpleNamespace(
        success=lambda: True,
        data=SimpleNamespace(message=SimpleNamespace(message_id="om_sent")),
    )
    api_method = MagicMock(return_value=response)
    channel._api_client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(reply=api_method)))
    )

    result = await channel.send_persistent_result(
        chat_id="oc_project",
        text="已完成",
        source_message_id="om_source",
        idempotency_key="delivery-key",
    )

    request = api_method.call_args.args[0]
    assert request.message_id == "om_source"
    assert request.request_body.uuid == "delivery-key"
    assert request.request_body.msg_type == "interactive"
    card = json.loads(request.request_body.content)
    assert any(
        element.get("text", {}).get("content") == "已完成"
        and element.get("text", {}).get("tag") == "lark_md"
        for element in card["elements"]
    )
    assert result == {"outcome": "delivered", "external_message_id": "om_sent"}


@pytest.mark.asyncio
async def test_jm09_feishu_result_only_contains_the_answer():
    channel = _feishu_channel(agent_id="mate_dev")
    channel.config.bot_open_id = "ou_self"
    response = SimpleNamespace(
        success=lambda: True,
        data=SimpleNamespace(message=SimpleNamespace(message_id="om_sent")),
    )
    api_method = MagicMock(return_value=response)
    channel._api_client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(reply=api_method)))
    )

    await channel.send_persistent_result(
        chat_id="oc_project",
        text="请核对这个数字",
        source_message_id="om_source",
        idempotency_key="delivery-key",
    )

    content = json.loads(api_method.call_args.args[0].request_body.content)
    assert content["elements"][0]["text"]["content"] == "请核对这个数字"
    assert content["elements"][0]["text"]["tag"] == "lark_md"


@pytest.mark.asyncio
async def test_jm09_feishu_result_renders_a_native_bot_mention_for_group_handoff():
    channel = _feishu_channel(agent_id="agent-sender")
    response = SimpleNamespace(
        success=lambda: True,
        data=SimpleNamespace(message=SimpleNamespace(message_id="om_sent")),
    )
    api_method = MagicMock(return_value=response)
    channel._api_client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(reply=api_method)))
    )

    await channel.send_persistent_result(
        chat_id="oc_project",
        text="请继续 [[agent:agent-target]]",
        source_message_id="om_source",
        idempotency_key="delivery-key",
        mention_targets=[{"agent_id": "agent-target", "name": "研究同事", "open_id": "ou_target"}],
    )

    content = json.loads(api_method.call_args.args[0].request_body.content)
    assert content["elements"][0]["text"]["content"] == "请继续 <at id=ou_target></at>"


@pytest.mark.asyncio
async def test_jm09_feishu_result_keeps_a_readable_name_when_target_bot_is_unavailable():
    channel = _feishu_channel(agent_id="agent-sender")
    response = SimpleNamespace(
        success=lambda: True,
        data=SimpleNamespace(message=SimpleNamespace(message_id="om_sent")),
    )
    api_method = MagicMock(return_value=response)
    channel._api_client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(reply=api_method)))
    )

    await channel.send_persistent_result(
        chat_id="oc_project",
        text="请继续 [[agent:agent-target]]",
        source_message_id="om_source",
        idempotency_key="delivery-key",
        mention_targets=[{"agent_id": "agent-target", "name": "研究同事"}],
    )

    content = json.loads(api_method.call_args.args[0].request_body.content)
    assert content["elements"][0]["text"]["content"] == "请继续 @研究同事"


@pytest.mark.asyncio
async def test_jm09_feishu_result_uses_chat_create_when_no_source_message():
    channel = _feishu_channel()
    response = SimpleNamespace(
        success=lambda: True,
        data=SimpleNamespace(message=SimpleNamespace(message_id="om_new")),
    )
    api_method = MagicMock(return_value=response)
    channel._api_client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(create=api_method)))
    )

    result = await channel.send_persistent_result(
        chat_id="oc_project",
        text="已完成",
        source_message_id=None,
        idempotency_key="delivery-key",
    )

    request = api_method.call_args.args[0]
    assert request.receive_id_type == "chat_id"
    assert request.request_body.receive_id == "oc_project"
    assert request.request_body.uuid == "delivery-key"
    assert request.request_body.msg_type == "interactive"
    card = json.loads(request.request_body.content)
    assert card["elements"][0]["text"]["content"] == "已完成"
    assert card["elements"][0]["text"]["tag"] == "lark_md"
    assert result == {"outcome": "delivered", "external_message_id": "om_new"}


def _raw_feishu_event(
    *, sender_type="user", message_type="text", content=None, message_id="om_001", mention_ids=("ou_dev",)
):
    message = SimpleNamespace(
        message_id=message_id,
        chat_id="oc_project",
        chat_type="group",
        create_time=str(int(NOW.timestamp() * 1000)),
        message_type=message_type,
        content=content if content is not None else json.dumps({"text": "请看一下"}),
        mentions=[SimpleNamespace(id=SimpleNamespace(open_id=item), name="研发同事") for item in mention_ids],
        parent_id="om_parent",
        root_id="om_root",
    )
    sender = SimpleNamespace(
        sender_type=sender_type,
        sender_id=SimpleNamespace(open_id="ou_alice", user_id="u_alice", union_id="un_alice"),
        app_id=None,
    )
    return SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))


@pytest.mark.asyncio
async def test_bound_attachment_event_is_claimed_before_personal_download_and_dedup():
    channel = _feishu_channel()
    channel.config.bot_open_id = "ou_dev"
    received = []

    async def handle(event):
        received.append(event)
        return InboundDisposition.HANDLED

    channel.set_persistent_group_fanout_callback(handle)
    raw = _raw_feishu_event(message_type="image", content=json.dumps({"image_key": "img_key_1"}))

    await channel._on_message(raw)
    await channel._on_message(raw)

    channel._parse_message_content_with_file.assert_not_awaited()
    channel._process_batched_message.assert_not_awaited()
    assert len(received) == 2  # JM-03, not the personal in-memory cache, owns persistent dedup.
    assert received[0].receiving_app_id == "app-dev"
    assert received[0].message_id == "om_001"
    assert received[0].chat_type == "group"
    assert received[0].sender_type == "user"
    assert received[0].sender_id == "ou_alice"
    assert received[0].sender_name == "ou_alice"
    assert received[0].sender_app_id is None
    assert received[0].received_at == NOW
    assert received[0].text is None
    assert received[0].mention_ids == ("ou_dev",)
    assert received[0].receiver_bot_mentioned is True
    assert received[0].reply_to == "om_parent"
    assert received[0].attachment_refs == ("feishu:attachment:om_001:img_key_1",)


@pytest.mark.asyncio
async def test_persistent_route_resolves_bot_open_id_before_normalizing_mention(monkeypatch):
    channel = _feishu_channel(agent_id="mate_dev")
    order = []

    def fetch_bot_open_id():
        order.append("resolve")
        channel.config.bot_open_id = "ou_dev"

    async def handle(event):
        order.append("callback")
        assert event.receiver_bot_mentioned is True
        return InboundDisposition.HANDLED

    monkeypatch.setattr(channel, "_fetch_bot_open_id", fetch_bot_open_id)
    channel.set_persistent_group_fanout_callback(handle)

    await channel._on_message(_raw_feishu_event())

    assert order == ["resolve", "callback"]


@pytest.mark.asyncio
async def test_mention_of_another_bot_does_not_mark_receiving_bot_mentioned():
    channel = _feishu_channel(agent_id="mate_dev")
    channel.config.bot_open_id = "ou_dev"
    received = []

    async def handle(event):
        received.append(event)
        return InboundDisposition.HANDLED

    channel.set_persistent_group_fanout_callback(handle)
    await channel._on_message(_raw_feishu_event(mention_ids=("ou_other",)))

    assert received[0].receiver_bot_mentioned is False


@pytest.mark.asyncio
async def test_unclaimed_human_continues_personal_path_and_keeps_local_dedup():
    channel = _feishu_channel()
    calls = []

    async def unclaimed(_event):
        calls.append("route")
        return InboundDisposition.UNCLAIMED

    channel.set_persistent_group_fanout_callback(unclaimed)
    raw = _raw_feishu_event()

    await channel._on_message(raw)
    await channel._on_message(raw)

    assert calls == ["route", "route"]
    channel._parse_message_content_with_file.assert_awaited_once()
    channel._process_batched_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_unclaimed_bot_stays_ignored_and_rejected_or_failed_routes_never_fall_back():
    bot_channel = _feishu_channel()
    bot_route = AsyncMock(return_value=InboundDisposition.UNCLAIMED)
    bot_channel.set_persistent_group_fanout_callback(bot_route)
    await bot_channel._on_message(_raw_feishu_event(sender_type="bot"))
    bot_channel._parse_message_content_with_file.assert_not_awaited()
    bot_channel._process_batched_message.assert_not_awaited()

    rejected_channel = _feishu_channel()
    rejected_channel.set_persistent_group_fanout_callback(
        AsyncMock(return_value=InboundDisposition.REJECTED)
    )
    await rejected_channel._on_message(_raw_feishu_event())
    rejected_channel._parse_message_content_with_file.assert_not_awaited()
    rejected_channel._process_batched_message.assert_not_awaited()

    failed_channel = _feishu_channel()

    async def fail_closed(_event):
        raise RuntimeError("sensitive event detail")

    failed_channel.set_persistent_group_fanout_callback(fail_closed)
    await failed_channel._on_message(_raw_feishu_event())
    failed_channel._parse_message_content_with_file.assert_not_awaited()
    failed_channel._process_batched_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_manager_wires_feishu_to_message_handler_and_unconfigured_handler_is_unclaimed():
    handler = object.__new__(MessageHandler)
    handler.set_persistent_group_fanout(None)
    channel = _feishu_channel()
    manager = ChannelManager(handler)

    manager.register_channel(channel)
    await channel._on_message(_raw_feishu_event())

    assert handler._persistent_group_fanout is None
    channel._parse_message_content_with_file.assert_awaited_once()
    channel._process_batched_message.assert_awaited_once()


def test_channel_manager_wires_only_feishu_apps_with_a_persistent_agent_binding():
    handler = object.__new__(MessageHandler)
    handler.set_persistent_group_fanout(None)
    manager = ChannelManager(handler)
    personal_channel = _feishu_channel(agent_id="")
    bound_channel = _feishu_channel(agent_id="mate_bound")

    manager.register_channel(personal_channel)
    assert personal_channel._persistent_group_fanout_callback is None

    manager.register_channel(bound_channel)
    assert bound_channel._persistent_group_fanout_callback == handler.handle_persistent_feishu_event


@pytest.mark.asyncio
async def test_message_handler_delegates_to_configured_fanout():
    handler = object.__new__(MessageHandler)
    fanout = MagicMock()
    fanout.handle = AsyncMock(return_value=InboundDisposition.HANDLED)
    handler.set_persistent_group_fanout(fanout)
    event = _event()

    result = await handler.handle_persistent_feishu_event(event)

    assert result is InboundDisposition.HANDLED
    fanout.handle.assert_awaited_once_with(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,mentioned,chat_type,expected", [
    ("passive", False, "group", 0),
    ("passive", True, "group", 1),
    ("adaptive", False, "group", 1),
    ("passive", False, "p2p", 1),
])
async def test_group_reply_mode_controls_requests_and_preserves_context(host, mode, mentioned, chat_type, expected):
    from dataclasses import replace

    agent_id = host.colleague("研发同事")
    settings = host.service.read_settings(NS, agent_id)
    host.service.update_settings(NS, agent_id, replace(settings, group_reply_mode=mode))
    assert host.service.read_settings(NS, agent_id).group_reply_mode == mode
    assert host.service.get_colleague(NS, agent_id).to_dict()["group_reply_mode"] == mode
    fanout = host.fanout([_binding("app-dev", agent_id)])
    event = _event(chat_type=chat_type, receiver_bot_mentioned=mentioned)

    assert await fanout.handle(event) is InboundDisposition.HANDLED
    assert len(host.request_states(agent_id, event)) == expected
    if chat_type == "group":
        group = host.groups.resolve(NS, "feishu", event.chat_id, now=NOW)
        archived = host.manager.group_archive.current_messages(NS, group.group_chat_id, (event.message_id,))
        assert len(archived) == 1


def test_group_reply_mode_defaults_and_validation():
    from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

    assert ColleagueSettings.from_dict({"name": "Test"}).group_reply_mode == "adaptive"
    with pytest.raises(PersistentAgentError, match="group_reply_mode"):
        ColleagueSettings.from_dict({"name": "Test", "group_reply_mode": "invalid"})
