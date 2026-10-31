"""Multi-assistant channel identity and lifecycle boundaries."""
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_connect import WelinkChannel, WelinkConfig


def test_assistants_share_reader_but_have_distinct_channel_keys(tmp_path):
    first = WelinkChannel(WelinkConfig(account='reader', agent_id='a', assistant_credential_id='ak-a', credential_dir=str(tmp_path/'a')), object())
    second = WelinkChannel(WelinkConfig(account='reader', agent_id='b', assistant_credential_id='ak-b', credential_dir=str(tmp_path/'b')), object())
    assert first.app_id == 'ak-a'
    assert second.app_id == 'ak-b'


def test_history_keeps_original_time():
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import parse_history_messages
    messages = parse_history_messages(
        [{'messageId': '1', 'senderAccount': 'human', 'content': 'hello', 'serverSendTime': 1767225600000}],
        group_id='g',
    )
    assert messages[0].received_at == '2026-01-01T00:00:00+00:00'
    ignored = parse_history_messages(
        [{'messageId': '2', 'senderAccount': 'human', 'content': 'hello', 'sendTime': '2026-10-27T06:51:36Z'}],
        group_id='g',
    )
    assert ignored[0].received_at == ''


@pytest.mark.asyncio
async def test_binding_update_preserves_existing_socket_and_reader(tmp_path, monkeypatch):
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_runtime import WelinkRuntime
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_reader import WelinkReader
    import asyncio

    async def wait_forever(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(WelinkChannel, 'start', wait_forever)
    monkeypatch.setattr(WelinkReader, 'run', wait_forever)
    manager = ChannelManager(AsyncMock())
    runtime = WelinkRuntime(manager, object(), AsyncMock())
    conf = {'account':'reader', 'cli_authorized':True, 'reader_credential_dir':str(tmp_path/'reader'),
            'bindings':[{'persistent_agent_id':'a', 'assistant_credential_id':'ak-a', 'assistant_secret':'secret-a',
                         'credential_dir':str(tmp_path/'a'), 'enabled':True}]}
    try:
        await runtime.apply(conf)
        first = runtime.channels['ak-a']
        task = first.start_task
        reader = runtime.reader
        conf['bindings'].append({'persistent_agent_id':'b', 'assistant_credential_id':'ak-b', 'assistant_secret':'secret-b',
                                 'credential_dir':str(tmp_path/'b'), 'enabled':True})
        await runtime.apply(conf)
        assert runtime.channels['ak-a'] is first and first.start_task is task
        assert runtime.reader is reader
        conf['bindings'].pop()
        await runtime.apply(conf)
        assert runtime.channels['ak-a'] is first and runtime.reader is reader
        assert not task.cancelled()
        conf['bindings'][0]['assistant_secret'] = 'rotated'
        await runtime.apply(conf)
        assert runtime.channels['ak-a'] is not first
        assert runtime.reader is reader
    finally:
        await runtime.stop()
    assert task.done()


@pytest.mark.asyncio
async def test_shared_reader_retries_each_colleague_and_catches_up_new_binding(tmp_path):
    from types import SimpleNamespace
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_reader import WelinkReader
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import WelinkHistoryMessage
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_poller import MembershipStore
    from unittest.mock import Mock

    message = WelinkHistoryMessage(message_id='10', group_id='g', sender_id='human', sender_name='Human', text='hello', received_at='2026-10-27T06:00:00+00:00')
    def channel(identity):
        return SimpleNamespace(config=SimpleNamespace(agent_id=identity), _own_assistants=lambda accounts: {identity} & accounts,
                               _history=Mock(return_value=[message]), _deliver=AsyncMock(return_value=True), _link=None)
    a = channel('dig_1_a')
    channels = {'ak-a':a}
    reader = WelinkReader(object(), 'h1', str(tmp_path), 4, channels)
    reader._subscriptions.register('ak-a', a.config.agent_id, 'g')
    members = MembershipStore(tmp_path/'membership.json')
    members.set('g', shared=True, assistants={'dig_1_a','dig_1_b'})
    reader._poller = SimpleNamespace(poll=Mock(return_value=[message]), _snapshot=('g',), last_page=('g', [message]), _membership=members,
                                    _is_shared=lambda group: True, commit=Mock())
    await reader.scan()
    assert a._deliver.call_args_list[0].kwargs['first_observation']
    assert a._deliver.call_args_list[0].kwargs['visible_from'] == message.received_at
    b = channel('dig_1_b')
    b._deliver.side_effect = [False, True]
    channels['ak-b'] = b
    reader._subscriptions.register('ak-b', b.config.agent_id, 'g')
    reader._poller.poll.return_value = []
    await reader.scan()
    assert reader._pending
    await reader.scan()
    assert not reader._pending
    assert b._history.call_count == 0
    assert a._history.call_count == 0
    assert b._deliver.call_count == 2


@pytest.mark.asyncio
async def test_retry_after_restart_preserves_invoke_alias_and_completed_targets(tmp_path):
    from dataclasses import asdict
    from types import SimpleNamespace
    from unittest.mock import Mock
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_reader import WelinkReader
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import WelinkHistoryMessage
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_poller import MembershipStore

    message = WelinkHistoryMessage(message_id='10', group_id='g', sender_id='human', sender_name='Human', text='hello')
    first = SimpleNamespace(config=SimpleNamespace(agent_id='a'), _own_assistants=lambda accounts: {'member-a'} & accounts,
        _deliver=AsyncMock(return_value=True),
        _link=SimpleNamespace(claim_polled=Mock(return_value=True),
                              canonical_message_id=Mock(return_value='invoke-10')))
    second = SimpleNamespace(config=SimpleNamespace(agent_id='b'), _own_assistants=lambda accounts: {'member-b'} & accounts,
        _deliver=AsyncMock(side_effect=[False, True]), _link=None)
    channels = {'ak-a': first, 'ak-b': second}
    members = MembershipStore(tmp_path / 'membership.json')
    members.set('g', shared=True, assistants={'member-a', 'member-b'})

    def poller():
        return SimpleNamespace(poll=Mock(return_value=[]), _snapshot=('g',), last_page=('g', [message]), _membership=members,
            _is_shared=lambda group: True, commit=Mock())

    reader = WelinkReader(object(), 'reader', str(tmp_path), 4, channels)
    for credential, item in channels.items():
        reader._subscriptions.register(credential, item.config.agent_id, 'g')
    reader._poller = poller()
    reader._pending = [{'message': asdict(message), 'targets': ['ak-a', 'ak-b'], 'target_agents': {'ak-a': 'a', 'ak-b': 'b'}, 'commit': True}]
    reader._save(reader._pending_path, reader._pending)
    await reader.scan()
    assert reader._pending
    assert first._deliver.call_args.args[0].message_id == 'invoke-10'
    assert second._deliver.call_args.args[0].message_id == 'invoke-10'

    # New links have lost their process-local invoke-to-history aliases.
    first._link = None
    restarted = WelinkReader(object(), 'reader', str(tmp_path), 4, channels)
    restarted._poller = poller()
    await restarted.scan()
    assert not restarted._pending
    assert first._deliver.await_count == 1
    assert second._deliver.await_count == 2
    assert second._deliver.call_args.args[0].message_id == 'invoke-10'
    restarted._poller.commit.assert_called_once_with('g', '10')


@pytest.mark.asyncio
async def test_reassigned_credential_drops_old_jobs_and_catches_up_new_colleague(tmp_path):
    from dataclasses import asdict
    from types import SimpleNamespace
    from unittest.mock import Mock
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_reader import WelinkReader
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import WelinkHistoryMessage
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_poller import MembershipStore

    message = WelinkHistoryMessage(message_id='10', group_id='g', sender_id='human', sender_name='Human', text='hello')
    def channel(agent):
        return SimpleNamespace(config=SimpleNamespace(agent_id=agent),
            _own_assistants=lambda accounts: {'member'} & accounts,
            _deliver=AsyncMock(return_value=True), _history=Mock(return_value=[message]), _link=None)

    first, second = channel('a'), channel('b')
    channels = {'ak': first}
    reader = WelinkReader(object(), 'reader', str(tmp_path), 4, channels)
    members = MembershipStore(tmp_path / 'membership.json')
    members.set('g', shared=True, assistants={'member'})
    reader._poller = SimpleNamespace(poll=Mock(return_value=[]), _snapshot=('g',), last_page=('g', [message]), _membership=members,
        _is_shared=lambda group: True, commit=Mock())
    old_key = reader._key('ak', 'g')
    reader._seen.add(old_key)
    reader._pending = [
        {'prepare_catchup': True, 'group': 'g', 'credential': 'ak', 'seen_key': old_key,
         'target_agents': {'ak': 'a'}},
        {'message': asdict(message), 'targets': ['ak'], 'target_agents': {'ak': 'a'}, 'commit': True},
    ]
    reader._subscriptions.register('ak', 'a', 'g')
    channels['ak'] = second
    await reader.scan()
    assert second._deliver.await_count == 0 and second._history.call_count == 0
    reader._subscriptions.register('ak', 'b', 'g')
    await reader.scan()
    assert second._history.call_count == 0 and second._deliver.await_count == 1
    assert second._deliver.call_args.kwargs['first_observation']
    assert reader._key('ak', 'g') != old_key


@pytest.mark.asyncio
async def test_canonical_alias_survives_completed_job_and_restart(tmp_path):
    from dataclasses import asdict
    from types import SimpleNamespace
    from unittest.mock import Mock
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_reader import WelinkReader
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import WelinkHistoryMessage
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_poller import MembershipStore

    message = WelinkHistoryMessage(message_id='10', group_id='g', sender_id='human', sender_name='Human', text='hello')
    channel = SimpleNamespace(config=SimpleNamespace(agent_id='a'),
        _own_assistants=lambda accounts: {'member'} & accounts, _deliver=AsyncMock(return_value=True),
        _link=SimpleNamespace(claim_polled=Mock(return_value=True), canonical_message_id=Mock(return_value='invoke-10')))
    channels = {'ak': channel}
    members = MembershipStore(tmp_path / 'membership.json')
    members.set('g', shared=True, assistants={'member'})
    def poller():
        return SimpleNamespace(poll=Mock(return_value=[]), _snapshot=('g',), last_page=('g', [message]), _membership=members,
            _is_shared=lambda group: True, commit=Mock())
    def job():
        return {'message': asdict(message), 'targets': ['ak'], 'target_agents': {'ak': 'a'}, 'commit': True}

    reader = WelinkReader(object(), 'reader', str(tmp_path), 4, channels)
    for credential, item in channels.items():
        reader._subscriptions.register(credential, item.config.agent_id, 'g')
    reader._poller = poller()
    reader._pending = [job()]
    await reader.scan()
    assert not reader._pending
    # A later ordinary poll can still encounter a catchup message after its first job completed.
    reader._save(reader._pending_path, [job()])
    channel._link = None
    restarted = WelinkReader(object(), 'reader', str(tmp_path), 4, channels)
    restarted._poller = poller()
    await restarted.scan()
    assert [call.args[0].message_id for call in channel._deliver.call_args_list] == ['invoke-10', 'invoke-10']


def test_member_identity_does_not_select_another_assistant(tmp_path):
    channel = WelinkChannel(WelinkConfig(account='h1', agent_id='a', assistant_credential_id='ak-a',
        assistant_account='dig_1_a', credential_dir=str(tmp_path), shared_reader=True), object())
    assert channel._own_assistants({'dig_1_a', 'dig_1_b'}) == {'dig_1_a'}
    assert channel._own_assistants({'dig_1_b'}) == set()
    channel.config.assistant_account = ''
    assert channel._own_assistants({'dig_1_a'}) == set()
    channel.config.legacy_identity_fallback = True
    channel._shared_binding_count = 1
    assert channel._own_assistants({'dig_1_a'}) == {'dig_1_a'}
    channel._shared_binding_count = 2
    assert channel._own_assistants({'dig_1_a'}) == set()


@pytest.mark.asyncio
async def test_handoff_validation_is_read_only_and_transient_lookup_stays_unknown(tmp_path):
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
    from unittest.mock import Mock
    channel = WelinkChannel(WelinkConfig(account='h1', agent_id='a', assistant_credential_id='ak-a',
        credential_dir=str(tmp_path)), object())
    channel._allowed_assistants = Mock(side_effect=OSError('rate limited'))
    channel.send_group_text = AsyncMock(side_effect=AssertionError('must not send'))
    manager = ChannelManager(AsyncMock())
    manager.register_channel(channel)
    payload = {'channel':'welink', 'app_id':'sender-ak', 'chat_id':'g', 'target_id':'g',
               'operation':'validate_handoff', 'mention_targets':[{'agent_id':'a'}]}
    # Public route names the group target, matching the wire payload.
    payload['chat_id'] = 'g'
    result = await manager.deliver_persistent_result(payload)
    assert result['handoff_target_valid'] is None
    channel._allowed_assistants.side_effect = None
    channel._allowed_assistants.return_value = set()
    assert (await manager.deliver_persistent_result(payload))['handoff_target_valid'] is False
    channel.send_group_text.assert_not_awaited()
