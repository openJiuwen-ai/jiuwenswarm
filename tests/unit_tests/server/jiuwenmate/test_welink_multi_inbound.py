"""WeLink colleagues share an archive but retain independent routing and visibility."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.jiuwenmate import welink_inbound as inbound
from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive
from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository

NOW = datetime(2026, 10, 27, 8, tzinfo=timezone.utc)


def config():
    return {"channels": {"welink": {"account": "reader", "cli_authorized": True,
        "reader_credential_dir": "/reader", "bindings": [
            {"persistent_agent_id": agent, "assistant_credential_id": f"ak-{agent}",
             "assistant_secret": "private-secret", "credential_dir": f"/assistant/{agent}", "enabled": True}
            for agent in ("a", "b")]}}}


def test_two_bound_colleagues_stay_active_in_a_shared_group():
    access = SimpleNamespace(status="active", namespace_id="local", agent_id="b", app_id="reader")
    assert inbound.welink_group_membership_status(access, config()) == "active"
    assert inbound.configured_welink_binding(config()) is None
    stranger = SimpleNamespace(status="active", namespace_id="local", agent_id="c", app_id="reader")
    assert inbound.welink_group_membership_status(stranger, config()) == "unknown"
    left = SimpleNamespace(status="left", namespace_id="local", agent_id="b", app_id="reader")
    assert inbound.welink_group_membership_status(left, config()) == "unknown"


def test_multiple_bindings_and_legacy_reader_are_distinct():
    bindings = inbound.configured_welink_bindings(config())
    assert [item.agent_id for item in bindings] == ["a", "b"]
    assert bindings[0].reader_credential_dir == "/reader"
    assert bindings[0].credential_dir == "/assistant/a"
    assert "private-secret" not in repr(bindings[0])
    assert inbound.configured_welink_binding(config()) is None
    assert inbound.welink_binding_for_agent("b", config()).assistant_credential_id == "ak-b"
    legacy = {"channels": {"welink": {"account": "reader", "cli_authorized": True,
        "persistent_agent_id": "a", "assistant_credential_id": "ak-a", "assistant_secret": "secret",
        "credential_dir": "/legacy", "enabled": True}}}
    assert inbound.configured_welink_binding(legacy).reader_credential_dir == "/legacy"


def event(agent="a", **changes):
    args = dict(account="reader", expected_agent_id=agent, assistant_credential_id=f"ak-{agent}",
        message_id="m1", chat_id="g", sender_id="person", sender_name="Person", text="do this",
        received_at=NOW)
    args.update(changes)
    return inbound.WelinkInboundEvent(**args)


def test_payload_round_trip_keeps_verified_mention_and_catchup_boundary():
    original = event(mentioned=True, first_observation=True, visible_from=NOW-timedelta(minutes=2))
    assert inbound.WelinkInboundEvent.from_payload(original.to_payload()) == original
    assert not inbound.WelinkInboundEvent.from_payload(event().to_payload()).mentioned


@pytest.fixture
def fanout(tmp_path, monkeypatch):
    lookup = inbound.welink_binding_for_agent
    monkeypatch.setattr(inbound, "welink_binding_for_agent", lambda agent: lookup(agent, config()))
    groups = GroupChatRepository(tmp_path / "groups.sqlite3")
    archive = ConversationArchive(tmp_path / "archive", groups)
    modes = {"a": "adaptive", "b": "adaptive"}
    return SimpleNamespace(groups=groups, clock=lambda: NOW + timedelta(minutes=1), modes=modes,
        agent_manager=SimpleNamespace(group_archive=archive, scheduler=SimpleNamespace(accept=AsyncMock()),
            group_participant_roster=lambda *args: ()),
        sessions=SimpleNamespace(resolve_session=lambda ns, agent, *args, **kwargs: SimpleNamespace(agent_id=agent)),
        service=SimpleNamespace(connection_of=lambda *args: SimpleNamespace(channel="welink", connection_key="reader"),
            get_colleague=lambda *args: SimpleNamespace(enabled=True),
            current_snapshot=lambda ns, agent: SimpleNamespace(agent_id=agent),
            read_settings=lambda ns, agent: SimpleNamespace(group_reply_mode=modes[agent])))


@pytest.mark.asyncio
async def test_private_chat_opens_a_direct_request_for_that_colleague(fanout):
    message = event(chat_type="p2p", chat_id="p2p:person", message_id="dm1", text="介绍一下你自己")
    assert (await inbound.accept_welink_message(fanout, message)).value == "handled"
    call = fanout.agent_manager.scheduler.accept.call_args
    request = call.kwargs["request_input"]
    assert request.kind == "direct_message"
    assert request.reply_target == "p2p:person"
    assert request.source.message_id == "dm1"
    assert call.kwargs["route_snapshot"].target_id == "p2p:person"
    assert call.kwargs["route_snapshot"].source_message_id == "dm1"
    echo = event(chat_type="p2p", chat_id="p2p:person", message_id="dm2", sender_kind="agent")
    assert (await inbound.accept_welink_message(fanout, echo)).value == "handled"
    assert fanout.agent_manager.scheduler.accept.call_count == 1


@pytest.mark.asyncio
async def test_shared_archive_schedules_both_colleagues_without_overwriting_time(fanout):
    assert (await inbound.accept_welink_message(fanout, event())).value == "handled"
    assert (await inbound.accept_welink_message(fanout, event("b", received_at=NOW+timedelta(seconds=10)))).value == "handled"
    calls = fanout.agent_manager.scheduler.accept.call_args_list
    assert [call.args[0].agent_id for call in calls] == ["a", "b"]
    assert [call.kwargs["route_snapshot"].app_id for call in calls] == ["ak-a", "ak-b"]
    assert calls[1].kwargs["request_input"].source.received_at == NOW
    group = fanout.groups.resolve("local", "welink", "g", now=NOW)
    assert fanout.groups.access_for("local", "b", group.group_chat_id).visible_from == NOW


@pytest.mark.asyncio
async def test_passive_archives_only_but_verified_invoke_schedules(fanout):
    fanout.modes["a"] = "passive"
    await inbound.accept_welink_message(fanout, event(text="@a do this"))
    assert fanout.agent_manager.scheduler.accept.await_count == 0
    await inbound.accept_welink_message(fanout, event(text="@a do this", mentioned=True))
    assert fanout.agent_manager.scheduler.accept.await_count == 1
    assert fanout.agent_manager.scheduler.accept.call_args.kwargs["request_input"].source.mention_ids == ("a",)


@pytest.mark.asyncio
async def test_catchup_boundary_covers_batch_without_moving_existing_agent(fanout):
    await inbound.accept_welink_message(fanout, event())
    oldest = NOW-timedelta(minutes=2)
    await inbound.accept_welink_message(fanout, event("b", first_observation=True, visible_from=oldest))
    await inbound.accept_welink_message(fanout, event("b", message_id="older", received_at=oldest))
    group = fanout.groups.resolve("local", "welink", "g", now=NOW)
    assert fanout.groups.access_for("local", "a", group.group_chat_id).visible_from == NOW
    assert fanout.groups.access_for("local", "b", group.group_chat_id).visible_from == oldest
    assert fanout.agent_manager.scheduler.accept.await_count == 3
    await inbound.accept_welink_message(fanout, event(message_id="older", received_at=oldest, mentioned=True))
    assert fanout.agent_manager.scheduler.accept.await_count == 3


@pytest.mark.asyncio
async def test_wrong_assistant_and_conflicting_content_never_schedule(fanout):
    assert (await inbound.accept_welink_message(fanout, event(assistant_credential_id="ak-b"))).value == "rejected"
    await inbound.accept_welink_message(fanout, event())
    await inbound.accept_welink_message(fanout, event("b", text="conflicting new body", received_at=NOW+timedelta(seconds=10)))
    assert fanout.agent_manager.scheduler.accept.await_count == 1


@pytest.mark.asyncio
async def test_queue_failure_remains_retryable(fanout):
    fanout.agent_manager.scheduler.accept.side_effect = RuntimeError("queue unavailable")
    assert (await inbound.accept_welink_message(fanout, event())).value == "retryable"


@pytest.mark.asyncio
async def test_confirmed_handoff_reuses_history_with_request_only_agent_identity(fanout):
    fanout.modes["b"] = "passive"
    await inbound.accept_welink_message(fanout, event("b", sender_id="assistant-account"))
    assert fanout.agent_manager.scheduler.accept.await_count == 0
    await inbound.accept_welink_message(fanout, event("b", sender_id="a", sender_name="Colleague A",
        sender_kind="agent", mentioned=True))
    source = fanout.agent_manager.scheduler.accept.call_args.kwargs["request_input"].source
    assert (source.sender_id, source.sender_kind, source.mention_ids) == ("a", "agent", ("b",))
    group = fanout.groups.resolve("local", "welink", "g", now=NOW)
    archived, = fanout.agent_manager.group_archive.current_messages("local", group.group_chat_id, ("m1",))
    assert archived.sender_id == "assistant-account" and archived.sender_kind == "human"


@pytest.mark.asyncio
async def test_office_binding_resolves_to_its_welink_connection(tmp_path, monkeypatch):
    office, connection_agent = "mate_office", "mate_conn"
    raw = {"channels": {"welink": {"account": "reader", "cli_authorized": True,
        "reader_credential_dir": "/reader", "bindings": [{
            "persistent_agent_id": office, "config_id": office,
            "assistant_credential_id": "ak-office", "assistant_secret": "private-secret",
            "credential_dir": "/assistant/office", "enabled": True}]}}}
    lookup = inbound.welink_binding_for_agent
    monkeypatch.setattr(inbound, "welink_binding_for_agent", lambda agent: lookup(agent, raw))
    groups = GroupChatRepository(tmp_path / "groups.sqlite3")
    archive = ConversationArchive(tmp_path / "archive", groups)
    seen = []

    def snapshot(namespace, agent):
        seen.append(agent)
        return SimpleNamespace(agent_id=agent)

    fanout = SimpleNamespace(
        groups=groups, clock=lambda: NOW + timedelta(minutes=1),
        agent_manager=SimpleNamespace(group_archive=archive, scheduler=SimpleNamespace(accept=AsyncMock()),
            group_participant_roster=lambda *args: ()),
        sessions=SimpleNamespace(resolve_session=lambda ns, agent, *args, **kwargs: SimpleNamespace(agent_id=agent)),
        service=SimpleNamespace(
            connection_of=lambda *args: None,
            connections=lambda *args: [SimpleNamespace(
                channel="welink", connection_key=f"reader:{office}", agent_id=connection_agent)],
            get_colleague=lambda *args: SimpleNamespace(enabled=True),
            current_snapshot=snapshot,
            read_settings=lambda ns, agent: SimpleNamespace(group_reply_mode="adaptive")))
    message = event(office, assistant_credential_id="ak-office")
    assert (await inbound.accept_welink_message(fanout, message)).value == "handled"
    assert seen == [connection_agent]
    assert fanout.agent_manager.scheduler.accept.call_args.args[0].agent_id == connection_agent


def test_transient_inbound_failure_is_not_a_permanent_rejection(monkeypatch):
    import asyncio
    from datetime import datetime, timezone
    from jiuwenswarm.server.jiuwenmate import welink_inbound
    from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
    binding = welink_inbound.WelinkBinding(account='reader', agent_id='mate', assistant_credential_id='ak')
    monkeypatch.setattr(welink_inbound, 'welink_binding_for_agent', lambda agent: binding)
    from unittest.mock import AsyncMock
    now = datetime.now(timezone.utc)
    from openjiuwen.harness.persistent_agent import GroupMessage
    source = GroupMessage(message_id="12", sender_id="human", sender_name="Human", sender_kind="human",
                          received_at=now, text="hello")
    scheduler = SimpleNamespace(accept=AsyncMock(side_effect=PersistentAgentError('queue full', code='QUEUE_FULL')))
    fanout = SimpleNamespace(
        clock=lambda: now,
        service=SimpleNamespace(connection_of=lambda *args: SimpleNamespace(channel="welink", connection_key="reader"), get_colleague=lambda *args: SimpleNamespace(enabled=True),
                                 current_snapshot=lambda *args: SimpleNamespace(agent_id='mate'),
                                 read_settings=lambda *args: SimpleNamespace(group_reply_mode='adaptive')),
        groups=SimpleNamespace(resolve=lambda *args, **kwargs: SimpleNamespace(group_chat_id='group'),
                                observe_access=lambda *args, **kwargs: None,
                                access_for=lambda *args: SimpleNamespace(status="active", visible_from=now)),
        sessions=SimpleNamespace(resolve_session=lambda *args, **kwargs: SimpleNamespace(session_id='session')),
        agent_manager=SimpleNamespace(scheduler=scheduler,
            group_archive=SimpleNamespace(append=lambda *args, **kwargs: None, current_messages=lambda *args: (source,)),
            group_participant_roster=lambda *args: ()),
    )
    event = welink_inbound.WelinkInboundEvent(account='reader', expected_agent_id='mate', assistant_credential_id='ak', message_id='12',
        chat_id='g', sender_id='human', sender_name='Human', text='hello', received_at=now)
    assert asyncio.run(welink_inbound.accept_welink_message(fanout, event)).value == 'retryable'
    assert scheduler.accept.await_count == 1

