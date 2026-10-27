"""Regression cases for retry, membership revocation and reply correlation."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_connect import WelinkChannel, WelinkConfig
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_poller import CursorStore, MembershipStore, WelinkPoller
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import WelinkHistoryMessage
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_assistant_link import AssistantLink, _QaSlot


def result(payload, code=0):
    return SimpleNamespace(stdout=json.dumps(payload), stderr='', returncode=code, ok=code == 0)


class MemberCli:
    def __init__(self):
        self.members = result({'respData': [{'memberAccount': 'reader', 'memberType': 'TYPE_NORMAL'}]})
        self.history_calls = 0
        self.member_calls = 0

    def query_recent_conversations(self):
        return result({'conversations': [{'groupId': 'g', 'type': 'group'}]})

    def query_group_members(self, group_id):
        self.member_calls += 1
        return self.members

    def query_history(self, group_id, cursor):
        self.history_calls += 1
        return result({'messages': [{'messageId': '12', 'senderAccount': 'human', 'text': 'hello'}]})


def test_shared_group_is_rechecked_and_removed_assistant_stops_reads(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('time.time', lambda: clock[0])
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    membership = MembershipStore(tmp_path / 'membership.json')
    membership.set('g', shared=True, assistants={'dig_reader_assistant'})
    cli = MemberCli()
    poller = WelinkPoller(cli, 'reader', CursorStore(tmp_path / 'cursor.json'), membership=membership)
    assert poller.poll()
    clock[0] += 61
    poller.poll()
    assert cli.member_calls == 1
    before = cli.history_calls
    for _ in range(3):
        poller.poll()
    assert cli.history_calls == before


def test_failed_membership_refresh_keeps_grace_then_expires_across_restart(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('time.time', lambda: clock[0])
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    membership = MembershipStore(tmp_path / 'membership.json')
    membership.set('g', shared=True, assistants={'dig_reader_assistant'})
    cli = MemberCli()
    cli.members = result({}, 429)
    poller = WelinkPoller(cli, 'reader', CursorStore(tmp_path / 'cursor.json'), membership=membership)
    clock[0] += 61
    poller.poll()
    assert poller.poll(), 'one failed refresh must not immediately revoke recent access'
    clock[0] += 600
    restarted = WelinkPoller(cli, 'reader', CursorStore(tmp_path / 'cursor.json'))
    before = cli.history_calls
    for _ in range(3):
        assert restarted.poll() == []
    assert cli.history_calls == before


def test_transient_inbound_failure_is_not_a_permanent_rejection(monkeypatch):
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
        _clock=lambda: now,
        _service=SimpleNamespace(connection_of=lambda *args: SimpleNamespace(channel="welink", connection_key="reader"), get_colleague=lambda *args: SimpleNamespace(enabled=True),
                                 current_snapshot=lambda *args: SimpleNamespace(agent_id='mate'),
                                 read_settings=lambda *args: SimpleNamespace(group_reply_mode='adaptive')),
        _groups=SimpleNamespace(resolve=lambda *args, **kwargs: SimpleNamespace(group_chat_id='group'),
                                observe_access=lambda *args, **kwargs: None,
                                access_for=lambda *args: SimpleNamespace(status="active", visible_from=now)),
        _sessions=SimpleNamespace(resolve_session=lambda *args, **kwargs: SimpleNamespace(session_id='session')),
        _agent_manager=SimpleNamespace(scheduler=scheduler,
            group_archive=SimpleNamespace(append=lambda *args, **kwargs: None, current_messages=lambda *args: (source,)),
            group_participant_roster=lambda *args: ()),
    )
    event = welink_inbound.WelinkInboundEvent(account='reader', expected_agent_id='mate', assistant_credential_id='ak', message_id='12',
        chat_id='g', sender_id='human', sender_name='Human', text='hello', received_at=now)
    assert asyncio.run(welink_inbound.accept_welink_message(fanout, event)).value == 'retryable'
    assert scheduler.accept.await_count == 1


def test_failed_delivery_retries_same_batch_before_polling_new_messages(tmp_path, monkeypatch):
    channel = WelinkChannel(WelinkConfig(account='reader', agent_id='mate', credential_dir=str(tmp_path)), None)
    channel._running = True
    channel._auth_ok_at = 10**15
    seen = []
    class Poller:
        def __init__(self):
            self.calls = 0
            self.committed = []
        def poll(self):
            self.calls += 1
            return [WelinkHistoryMessage(message_id=str(self.calls), sender_id='u', sender_name='U', text='hello', group_id='g')]
        def commit(self, group_id, message_id):
            self.committed.append(message_id)
    poller = Poller()
    async def handler(event):
        seen.append(event['message_id'])
        if len(seen) == 1:
            return 'retryable'
        channel._running = False
        return 'handled'
    async def no_sleep(*args):
        return None
    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    channel.set_inbound_handler(handler)
    asyncio.run(channel._poll_loop(poller))
    assert seen == ['1', '1']
    assert poller.committed == ['1']


def slot(message_id='mention-1'):
    return _QaSlot(message_id=message_id, group_id='g', text='@mate hello', tool_session_id='session',
        trace_id='trace', reply_message_id='reply', part_id='part', opened=True, created_at=0, sender_id='u', accepted=True)


def test_other_message_never_consumes_the_only_mention_slot():
    link = AssistantLink('ak', 'sk')
    link._slots['mention-1'] = slot()
    assert link._take_slot('other', 'g') is None
    assert link._take_slot('mention-1', 'other-group') is None
    assert link._take_slot('mention-1', 'g').reply_message_id == 'reply'


def test_history_alias_preserves_original_mention_key():
    link = AssistantLink('ak', 'sk')
    link._slots['mention-1'] = slot()
    assert link.claim_polled('g', 'history-1', 'hello', sender_id='u') is True
    assert link._take_slot('mention-1', 'g').reply_message_id == 'reply'
    link._slots['mention-1'] = slot()
    assert link._take_slot('history-1', 'g').reply_message_id == 'reply'


def test_same_text_from_another_sender_is_not_an_alias():
    link = AssistantLink('ak', 'sk')
    link._slots['mention-1'] = slot()
    assert link.claim_polled('g', 'history-1', 'hello', sender_id='other') is False


def test_inflight_mention_is_not_acknowledged_before_request_acceptance():
    link = AssistantLink('ak', 'sk')
    pending = slot()
    pending.accepted = False
    link._slots['mention-1'] = pending
    assert link.claim_polled('g', 'history-1', 'hello', sender_id='u') is False
    assert link.canonical_message_id('g', 'history-1') == 'mention-1'


def test_permanently_rejected_message_is_skipped(tmp_path):
    channel = WelinkChannel(WelinkConfig(account='reader', agent_id='mate', credential_dir=str(tmp_path)), None)
    async def reject(event):
        return 'rejected'
    channel.set_inbound_handler(reject)
    bad = WelinkHistoryMessage(message_id='bad', sender_id='u', sender_name='U', text='bad', group_id='g')
    assert asyncio.run(channel._deliver(bad)) is True


def test_unaccepted_batch_survives_gateway_restart(tmp_path, monkeypatch):
    class Poller:
        def __init__(self, identifier):
            self.identifier = identifier
            self.committed = []
        def poll(self):
            return [WelinkHistoryMessage(message_id=self.identifier, sender_id='u', sender_name='U', text='hello', group_id='g')]
        def commit(self, group, identifier):
            self.committed.append(identifier)
    async def no_sleep(*args):
        return None
    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    seen = []
    for status, identifier in [('retryable', '12'), ('handled', '30')]:
        channel = WelinkChannel(WelinkConfig(account='reader', agent_id='mate', credential_dir=str(tmp_path)), None)
        channel._running = True
        channel._auth_ok_at = 10**15
        async def handler(event):
            seen.append(event['message_id'])
            channel._running = False
            return status
        channel.set_inbound_handler(handler)
        poller = Poller(identifier)
        asyncio.run(channel._poll_loop(poller))
    assert seen == ['12', '12']
    assert poller.committed == ['12']


def test_unrelated_answer_is_active_push_while_aligned_answer_uses_qa():
    link = AssistantLink('ak', 'sk')
    link._slots['mention-1'] = slot()
    frames = []
    socket = SimpleNamespace(send=lambda raw: frames.append(json.loads(raw)))
    link._send_text(socket, 'g', 'ordinary answer', 'other-message')
    assert frames[0]['businessPushType'] == 'active_push'
    frames.clear()
    assert link.claim_polled('g', 'history-1', 'hello', sender_id='u')
    link._send_text(socket, 'g', 'mention answer', 'mention-1')
    assert frames[0]['businessPushType'] == 'q_a'
    assert frames[0]['toolSessionId'] == 'session'
