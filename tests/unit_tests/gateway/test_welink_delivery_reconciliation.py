"""Only observed assistant history may confirm a durable WeLink send."""
import asyncio
import json
from types import SimpleNamespace

from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_connect import WelinkChannel, WelinkConfig
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import WelinkHistoryMessage
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_assistant_link import AssistantLink


def message(identifier, text='answer', sender='dig_reader_assistant'):
    return WelinkHistoryMessage(message_id=identifier, text=text, sender_id=sender, sender_name=sender, group_id='g')


def channel_at(tmp_path):
    channel = WelinkChannel(WelinkConfig(account='reader', agent_id='mate', credential_dir=str(tmp_path),
        assistant_credential_id='ak', assistant_secret='sk'), None)
    class Cli:
        def query_history(self, group, cursor):
            return SimpleNamespace(ok=True, stdout=json.dumps({'messages': [
                {'messageId': '10', 'senderAccount': 'human', 'text': 'question'}]}), returncode=0)
        def query_group_members(self, group):
            return SimpleNamespace(ok=True, returncode=0, stdout=json.dumps({'respData': [
                {'memberAccount': 'reader', 'memberType': 'TYPE_NORMAL'},
                {'memberAccount': 'dig_reader_assistant', 'memberType': 'TYPE_AGENT_ASSISTANT'}]}))
    channel._cli = Cli()
    return channel


def test_socket_write_is_unknown_and_duplicate_or_restart_never_resends(tmp_path):
    channel = channel_at(tmp_path)
    sends = []
    class Link:
        def send_text(self, group, text, source_message_id='', *, on_prepared=None):
            sends.append(text)
            if on_prepared:
                on_prepared('wire-1', 'session-1', 'trace-1')
            return 'wire-1'
    channel._link = Link()
    async def run():
        first = await channel.send_group_text(chat_id='g', text='answer', idempotency_key='key', source_message_id='10')
        assert first['outcome'] == 'unknown'
        assert (await channel.send_group_text(chat_id='g', text='answer', idempotency_key='key'))['outcome'] == 'unknown'
        restarted = channel_at(tmp_path)
        restarted._link = Link()
        assert (await restarted.send_group_text(chat_id='g', text='answer', idempotency_key='key'))['outcome'] == 'unknown'
    asyncio.run(run())
    assert sends == ['answer']


def test_history_confirmation_requires_sender_content_and_new_id(tmp_path):
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_delivery import DeliveryJournal
    journal = DeliveryJournal(tmp_path / 'receipts.sqlite3')
    assert journal.prepare('key', 'g', 'answer', '10', {'dig_reader_assistant'})
    journal.mark_wire('key', 'wire-1', 'session-1', 'trace-1')
    assert journal.reconcile('key', [message('9'), message('11', sender='human')])['outcome'] == 'unknown'
    assert journal.reconcile('key', [message('11'), message('12')])['outcome'] == 'unknown'
    confirmed = journal.reconcile('key', [message('11')])
    assert confirmed == {'outcome': 'delivered', 'external_message_id': '11', 'learned_sender': 'dig_reader_assistant'}
    restored = DeliveryJournal(tmp_path / 'receipts.sqlite3')
    assert restored.outcome('key')['outcome'] == 'delivered'


def test_correlated_tool_error_resolves_unknown_without_a_resend(tmp_path):
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_delivery import DeliveryJournal
    journal = DeliveryJournal(tmp_path / 'receipts.sqlite3')
    journal.prepare('key', 'g', 'answer', '10', {'dig_reader_assistant'})
    journal.mark_wire('key', 'wire-1', 'session-1', 'trace-1')
    link = AssistantLink('ak', 'sk', on_delivery_error=journal.record_error)
    link._on_frame(None, {'type': 'tool_error', 'toolSessionId': 'other', 'traceId': 'other'})
    assert journal.reconcile('key', [])['outcome'] == 'unknown'
    link._on_frame(None, {'type': 'tool_error', 'toolSessionId': 'session-1', 'traceId': 'trace-1'})
    assert journal.reconcile('key', [])['outcome'] == 'failed'


def test_missing_history_is_not_proof_of_failure_and_identical_inflight_text_waits(tmp_path):
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_delivery import DeliveryJournal
    journal = DeliveryJournal(tmp_path / 'receipts.sqlite3')
    journal.prepare('first', 'g', 'answer', '10', {'dig_reader_assistant'})
    assert journal.reconcile('first', [])['outcome'] == 'unknown'
    assert not journal.prepare('second', 'g', 'answer', '10', {'dig_reader_assistant'})
    assert journal.get('second') is None


def test_multiple_candidates_require_exact_wire_id_to_identify_sender(tmp_path):
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_delivery import DeliveryJournal
    journal = DeliveryJournal(tmp_path / 'receipts.sqlite3')
    journal.prepare('key', 'g', 'answer', '10', {'dig_reader_a', 'dig_reader_b'})
    journal.mark_wire('key', 'wire-1', 'session-1', 'trace-1')
    ambiguous = journal.reconcile('key', [
        message('11', sender='dig_reader_a'), message('12', sender='dig_reader_b')])
    assert ambiguous['outcome'] == 'unknown'
    assert 'learned_sender' not in ambiguous
    unrelated = journal.reconcile('key', [message('11', sender='dig_reader_a')])
    assert unrelated['outcome'] == 'unknown'
    assert 'learned_sender' not in unrelated
    confirmed = journal.reconcile('key', [message('wire-1', sender='dig_reader_a')])
    assert confirmed['outcome'] == 'delivered'
    assert confirmed['learned_sender'] == 'dig_reader_a'


def test_unmapped_send_keeps_every_reader_assistant_as_a_candidate(tmp_path):
    channel = channel_at(tmp_path)
    channel.config.shared_reader = True
    channel.config.account = 'h00557917'
    channel._shared_binding_count = 2

    class Cli:
        def query_history(self, group, cursor):
            return SimpleNamespace(ok=True, stdout=json.dumps({'messages': [
                {'messageId': '10', 'senderAccount': 'h00557917', 'text': 'question'}]}), returncode=0)
        def query_group_members(self, group):
            return SimpleNamespace(ok=True, returncode=0, stdout=json.dumps({'respData': [
                {'memberAccount': 'h00557917', 'memberType': 'TYPE_NORMAL'},
                {'memberAccount': 'dig_00557917_a', 'memberType': 'TYPE_AGENT_ASSISTANT'},
                {'memberAccount': 'dig_00557917_b', 'memberType': 'TYPE_AGENT_ASSISTANT'}]}))
    channel._cli = Cli()
    sent = []

    class Link:
        def send_text(self, group, text, source_message_id='', *, on_prepared=None):
            sent.append(text)
            if on_prepared:
                on_prepared('wire-1', 'session-1', 'trace-1')
            return 'wire-1'
    channel._link = Link()
    outcome = asyncio.run(channel.send_group_text(chat_id='g', text='answer', idempotency_key='key', source_message_id='10'))
    assert outcome['outcome'] == 'unknown'
    assert sent == ['answer']
    stored = set(json.loads(channel._delivery_journal().get('key')['assistants']))
    assert stored == {'dig_00557917_a', 'dig_00557917_b'}


def test_reconcile_records_the_unique_sender_on_the_channel(tmp_path):
    channel = channel_at(tmp_path)
    channel.config.shared_reader = True
    channel.config.account = 'h00557917'
    channel._shared_binding_count = 2
    remembered = []
    channel.remember_account = lambda account: remembered.append(account) or True

    class Cli:
        def query_history(self, group, cursor):
            return SimpleNamespace(ok=True, stdout=json.dumps({'messages': [
                {'messageId': 'wire-1', 'senderAccount': 'dig_00557917_a', 'text': 'answer'}]}), returncode=0)
        def query_group_members(self, group):
            return SimpleNamespace(ok=True, returncode=0, stdout=json.dumps({'respData': [
                {'memberAccount': 'h00557917', 'memberType': 'TYPE_NORMAL'},
                {'memberAccount': 'dig_00557917_a', 'memberType': 'TYPE_AGENT_ASSISTANT'},
                {'memberAccount': 'dig_00557917_b', 'memberType': 'TYPE_AGENT_ASSISTANT'}]}))
    channel._cli = Cli()
    journal = channel._delivery_journal()
    journal.prepare('key', 'g', 'answer', '10', {'dig_00557917_a', 'dig_00557917_b'})
    journal.mark_wire('key', 'wire-1', 'session-1', 'trace-1')
    outcome = asyncio.run(channel.reconcile_group_text(chat_id='g', idempotency_key='key'))
    assert outcome == {'outcome': 'delivered', 'external_message_id': 'wire-1'}
    assert channel.config.assistant_account == 'dig_00557917_a'
    assert remembered == ['dig_00557917_a']


def test_reconciliation_does_not_read_a_revoked_group(tmp_path):
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_poller import MembershipStore
    channel = channel_at(tmp_path)
    membership = MembershipStore(tmp_path / 'membership.json')
    membership.set('g', shared=False, assistants=set())
    journal = channel._delivery_journal()
    journal.prepare('key', 'g', 'answer', '10', {'dig_reader_assistant'})
    journal.mark_wire('key', 'wire', 'session', 'trace')
    reads = []
    def history(*args):
        reads.append(args)
        return SimpleNamespace(ok=True, stdout='{"messages": []}')
    channel._cli.query_history = history
    outcome = asyncio.run(channel.reconcile_group_text(chat_id='g', idempotency_key='key'))
    assert reads == []
    assert outcome['outcome'] == 'unknown'


def test_another_assistant_same_text_cannot_hide_correlated_rejection(tmp_path):
    journal = channel_at(tmp_path)._delivery_journal()
    journal.prepare('key', 'g', 'answer', '10', {'dig_reader_a', 'dig_reader_b'})
    journal.mark_wire('key', 'wire-a', 'session-a', 'trace-a')
    journal.record_error('session-a', 'trace-a')
    result = journal.reconcile('key', [message('11', sender='dig_reader_b')])
    assert result['outcome'] == 'failed'
    assert 'learned_sender' not in result


def test_private_delivery_is_durable_unknown_and_preserves_source(tmp_path):
    channel = channel_at(tmp_path)
    sends = []
    class Link:
        def send_text(self, chat, text, source_message_id='', *, on_prepared=None, finish_task=True):
            sends.append((chat, text, source_message_id, finish_task))
            on_prepared('wire-private', 'session-private', 'trace-private')
            return 'wire-private'
    channel._link = Link()
    async def run():
        args = dict(chat_id='p2p:reader', text='answer', source_message_id='source', idempotency_key='private')
        assert (await channel.send_group_text(**args))['outcome'] == 'unknown'
        assert (await channel.send_group_text(**args))['outcome'] == 'unknown'
        restarted = channel_at(tmp_path)
        restarted._link = Link()
        assert (await restarted.send_group_text(**args))['outcome'] == 'unknown'
        restarted._delivery_journal().record_error('session-private', 'trace-private')
        assert (await restarted.reconcile_group_text(chat_id='p2p:reader', idempotency_key='private'))['outcome'] == 'failed'
    asyncio.run(run())
    assert sends == [('p2p:reader', 'answer', 'source', True)]


def test_private_notice_is_journaled_without_closing_task(tmp_path):
    channel = channel_at(tmp_path)
    sends = []
    class Link:
        def send_text(self, chat, text, source_message_id='', *, on_prepared=None, finish_task=True):
            sends.append(finish_task)
            on_prepared('notice', 'session', 'trace')
            return 'notice'
    channel._link = Link()
    result = asyncio.run(channel.send_persistent_result(
        chat_id='p2p:reader', text='approval', source_message_id='source', idempotency_key='notice'))
    assert result['outcome'] == 'unknown'
    assert channel._delivery_journal().get('notice')['wire_id'] == 'notice'
    assert sends == [False]
    # A different private task may legitimately send the same notice.
    result = asyncio.run(channel.send_persistent_result(
        chat_id='p2p:reader', text='approval', source_message_id='next-source', idempotency_key='next-notice'))
    assert result['outcome'] == 'unknown'
    assert sends == [False, False]


def test_manager_passes_private_idempotency_key(tmp_path):
    from unittest.mock import AsyncMock
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
    channel = channel_at(tmp_path)
    channel.send_direct_text = AsyncMock(return_value={'outcome': 'unknown'})
    manager = object.__new__(ChannelManager)
    class Key:
        channel_id = 'welink'
        app_id = 'ak'
    manager._channels = {Key(): channel}
    result = asyncio.run(manager._deliver_welink_result(
        app_id='ak', chat_id='p2p:reader', text='answer', idempotency_key='key',
        file=None, _payload={'is_direct_message': True, 'source_message_id': 'source'}))
    assert result['outcome'] == 'unknown'
    assert channel.send_direct_text.call_args.kwargs['idempotency_key'] == 'key'
    assert channel.send_direct_text.call_args.kwargs['source_message_id'] == 'source'
