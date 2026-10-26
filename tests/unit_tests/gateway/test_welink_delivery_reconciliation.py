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
    assert journal.reconcile('key', [message('11')]) == {'outcome': 'delivered', 'external_message_id': '11'}
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
