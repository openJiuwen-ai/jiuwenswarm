"""Only explicitly mentioned groups are read; member identity is not routing evidence."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_reader import WelinkReader
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_subscriptions import MentionSubscriptions
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_connect import WelinkChannel, WelinkConfig


class Cli:
    def __init__(self):
        self.reads = []
        self.code = 0

    def query_recent_conversations(self):
        raise AssertionError('must not discover unrelated groups')

    def query_group_members(self, group):
        raise AssertionError('must not scan group membership')

    def query_history(self, group, cursor):
        self.reads.append(group)
        return SimpleNamespace(returncode=0, stdout=json.dumps({'resultCode': self.code, 'messages': []}))


def test_only_registered_current_bindings_are_round_robin_polled(tmp_path):
    cli = Cli()
    channel = SimpleNamespace(config=SimpleNamespace(agent_id='a', assistant_account=''))
    reader = WelinkReader(cli, 'reader', str(tmp_path), 4, {'ak': channel})
    assert reader._poller.poll() == []
    assert cli.reads == []
    reader._subscriptions.register('ak', 'a', 'a-group')
    reader._subscriptions.register('ak', 'a', 'b-group')
    reader._subscriptions.register('disabled', 'b', 'not-active')
    for _ in range(4):
        reader._poller.poll()
    assert cli.reads == ['a-group', 'b-group', 'a-group', 'b-group']
    assert reader._eligible('a-group') == {'ak': channel}
    restarted = WelinkReader(cli, 'reader', str(tmp_path), 4, {'ak': channel})
    assert restarted._eligible('a-group') == {'ak': channel}
    channel.config.agent_id = 'replacement'
    assert restarted._eligible('a-group') == {}
    assert restarted._poller.poll() == []


def test_rate_limit_retains_subscription_and_other_group_progresses(tmp_path):
    cli = Cli()
    reader = WelinkReader(cli, 'reader', str(tmp_path), 4,
                         {'ak': SimpleNamespace(config=SimpleNamespace(agent_id='a'))})
    reader._subscriptions.register('ak', 'a', 'a-group')
    reader._subscriptions.register('ak', 'a', 'b-group')
    cli.code = 429
    assert reader._poller.poll() == []
    assert reader._subscriptions.targets('a-group') == {('ak', 'a')}
    cli.code = 0
    reader._poller.poll()
    assert cli.reads == ['a-group', 'b-group']
    cli.code = 403
    reader._poller.poll()
    assert reader._subscriptions.targets('b-group') == set()


def test_real_group_mention_registers_but_private_message_does_not(tmp_path):
    channel = WelinkChannel(WelinkConfig(account='reader', agent_id='a', assistant_credential_id='ak',
        credential_dir=str(tmp_path)), None)
    channel._deliver = AsyncMock(return_value=True)
    mention = SimpleNamespace(message_id='m', sender_id='human', sender_name='Human', text='hi',
                              group_id='group', media=(), direct=False, mention_accounts=())
    asyncio.run(channel._on_mention(mention))
    store = MentionSubscriptions(tmp_path / 'mention-subscriptions.sqlite3', 'reader')
    assert store.targets('group') == {('ak', 'a')}
    mention.direct, mention.group_id = True, 'p2p:human'
    asyncio.run(channel._on_mention(mention))
    assert store.groups({'ak': 'a'}) == ['group']
    assert MentionSubscriptions(store.path, 'another-reader').groups({'ak': 'a'}) == []


def test_scan_fans_out_without_member_mapping_or_extra_history_query(tmp_path):
    class HistoryCli(Cli):
        def query_history(self, group, cursor):
            self.reads.append(group)
            return SimpleNamespace(returncode=0, stdout=json.dumps({'messages': [
                {'messageId': '10', 'senderAccount': 'human', 'text': '小九，介绍天气'},
                {'messageId': '11', 'senderAccount': 'dig_00557917_x', 'text': 'assistant echo'},
            ]}))
    cli = HistoryCli()
    channels = {key: SimpleNamespace(config=SimpleNamespace(agent_id=key, assistant_account=''),
                                    _link=None, _deliver=AsyncMock(return_value=True))
                for key in ('a', 'b', 'unmentioned')}
    reader = WelinkReader(cli, 'reader', str(tmp_path), 4, channels)
    reader._subscriptions.register('a', 'a', 'g')
    reader._subscriptions.register('b', 'b', 'g')
    asyncio.run(reader.scan())
    assert cli.reads == ['g']
    assert channels['unmentioned']._deliver.await_count == 0
    for key in ('a', 'b'):
        assert channels[key]._deliver.await_count > 0
        assert all(call.args[0].message_id == '10' for call in channels[key]._deliver.call_args_list)
    assert reader._poller._cursors.get('g') == '10'


def test_transport_failure_backs_off_only_that_group(tmp_path):
    cli = Cli()
    original = cli.query_history
    def query(group, cursor):
        if group == 'a':
            cli.reads.append(group)
            raise TimeoutError()
        return original(group, cursor)
    cli.query_history = query
    reader = WelinkReader(cli, 'reader', str(tmp_path), 4,
                         {'ak': SimpleNamespace(config=SimpleNamespace(agent_id='a'))})
    for group in ('a', 'b'):
        reader._subscriptions.register('ak', 'a', group)
    for _ in range(3):
        reader._poller.poll()
    assert cli.reads == ['a', 'b', 'b']
    assert reader._subscriptions.targets('a') == {('ak', 'a')}


def test_new_colleague_gets_latest_page_even_when_shared_cursor_is_current(tmp_path):
    calls = []
    class IncrementalCli(Cli):
        def query_history(self, group, cursor):
            calls.append(cursor)
            rows = [] if cursor else [{'messageId': '10', 'senderAccount': 'human', 'text': 'context'}]
            return SimpleNamespace(returncode=0, stdout=json.dumps({'messages': rows}))
    channels = {'a': SimpleNamespace(config=SimpleNamespace(agent_id='a'),
                                    _link=None, _deliver=AsyncMock(return_value=True))}
    reader = WelinkReader(IncrementalCli(), 'reader', str(tmp_path), 4, channels)
    reader._subscriptions.register('a', 'a', 'g')
    reader._seen.add(reader._key('a', 'g'))
    reader._poller.commit('g', '10')
    channels['b'] = SimpleNamespace(config=SimpleNamespace(agent_id='b'),
                                    _link=None, _deliver=AsyncMock(return_value=True))
    reader._subscriptions.register('b', 'b', 'g')
    asyncio.run(reader.scan())
    assert calls == [None]
    assert channels['a']._deliver.await_count == 0
    assert channels['b']._deliver.await_count == 1
    assert channels['b']._deliver.call_args.kwargs['first_observation']


def test_standalone_never_replays_legacy_or_other_owner_pending(tmp_path, monkeypatch):
    import hashlib
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import WelinkHistoryMessage
    channel = WelinkChannel(WelinkConfig(account='reader', agent_id='new-agent',
        assistant_credential_id='ak', credential_dir=str(tmp_path)), None)
    message = WelinkHistoryMessage(message_id='10', group_id='g', sender_id='human', sender_name='Human', text='old')
    old_account = hashlib.sha256(b'reader').hexdigest()[:24]
    old_owner = hashlib.sha256(json.dumps(['reader', 'old-agent', 'ak']).encode()).hexdigest()[:24]
    for name in (f'pending-{old_account}.json', f'registered-pending-{old_owner}.json'):
        channel._save_pending(tmp_path / name, [message])
    channel._running, channel._auth_ok_at = True, 10**15
    channel._deliver = AsyncMock(return_value=True)
    poller = SimpleNamespace(poll=lambda: [], _registered_groups=lambda: ['g'])
    async def stop(*args):
        channel._running = False
    monkeypatch.setattr(asyncio, 'sleep', stop)
    asyncio.run(channel._poll_loop(poller))
    channel._deliver.assert_not_awaited()


def test_standalone_drops_revoked_pending_without_delivery(tmp_path, monkeypatch):
    import hashlib
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import WelinkHistoryMessage
    channel = WelinkChannel(WelinkConfig(account='reader', agent_id='a',
        assistant_credential_id='ak', credential_dir=str(tmp_path)), None)
    owner = hashlib.sha256(json.dumps(['reader', 'a', 'ak']).encode()).hexdigest()[:24]
    channel._save_pending(tmp_path / f'registered-pending-{owner}.json', [
        WelinkHistoryMessage(message_id='10', group_id='revoked', sender_id='human', sender_name='Human', text='old')])
    channel._running, channel._auth_ok_at = True, 10**15
    channel._deliver = AsyncMock(return_value=True)
    poller = SimpleNamespace(poll=lambda: [], _registered_groups=lambda: [])
    async def stop(*args):
        channel._running = False
    monkeypatch.setattr(asyncio, 'sleep', stop)
    asyncio.run(channel._poll_loop(poller))
    channel._deliver.assert_not_awaited()
