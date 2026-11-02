"""Conservative history polling uses authorized groups without replaying work."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.jiuwenmate import dingtalk_group_polling
from jiuwenswarm.server.jiuwenmate.dingtalk_inbound import DingTalkBinding
from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import InboundDisposition
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError


def _fixture(tmp_path, monkeypatch, *, agents=2, groups=1):
    now = [datetime(2026, 10, 30, 12, tzinfo=timezone.utc)]
    bindings = [DingTalkBinding(app_id=f'app-{i}', agent_id=f'agent-{i}', robot_code=f'app-{i}')
                for i in range(agents)]
    modes = {item.agent_id: 'adaptive' for item in bindings}
    enabled = {item.agent_id: True for item in bindings}
    identities = {item.agent_id: 'a' * 64 for item in bindings}
    accesses = {
        item.agent_id: [SimpleNamespace(group_id=f'group-{j}', app_id=item.app_id) for j in range(groups)]
        for item in bindings
    }
    connections = {item.agent_id: SimpleNamespace(channel='dingtalk', connection_key=item.app_id)
                   for item in bindings}
    service = SimpleNamespace(
        get_colleague=lambda ns, agent: SimpleNamespace(enabled=enabled[agent]),
        read_settings=lambda ns, agent: SimpleNamespace(group_reply_mode=modes[agent]),
        connection_of=lambda ns, agent: connections.get(agent),
    )
    reads = []
    delivered = []
    membership_updates = []
    response = {'readerKey': 'a' * 64, 'messages': [], 'complete': True, 'hasMore': False, 'nextCursor': None,
                'botCodes': [item.robot_code for item in bindings]}
    hooks = SimpleNamespace(before_read=None, disposition=InboundDisposition.HANDLED, error=None)

    async def identity(scope):
        return identities.get(scope.agent_id)

    async def read(scope, *, conversation_id, since):
        reads.append((scope.agent_id, conversation_id, since))
        if hooks.error:
            raise hooks.error
        if hooks.before_read:
            await hooks.before_read()
        return {**response, 'readerKey': identities[scope.agent_id]}

    async def handle(binding, message, *, group_id, dispatch_after):
        delivered.append((binding.agent_id, message['messageId'], group_id, dispatch_after))
        return hooks.disposition

    def update_membership(ns, agent, group, app, *, status, observed_at, source_ref):
        membership_updates.append((ns, agent, group, app, status, observed_at, source_ref))
        accesses[agent] = [item for item in accesses[agent] if (item.group_id, item.app_id) != (group, app)]

    manager = SimpleNamespace(
        _service=service,
        group_chats=SimpleNamespace(
            active_accesses=lambda ns, agent: accesses[agent],
            get=lambda ns, group: SimpleNamespace(platform='dingtalk', platform_chat_id=f'cid-{group}'),
            update_membership=update_membership,
        ),
        dingtalk_cli_runtime=SimpleNamespace(group_history_identity=identity, read_group_history=read),
        _sessions=SimpleNamespace(db_path=tmp_path / 'sessions.sqlite'),
    )
    monkeypatch.setattr(dingtalk_group_polling, 'configured_dingtalk_bindings', lambda config=None: tuple(bindings))
    inbound = SimpleNamespace(handle_history_message=handle)
    poller = dingtalk_group_polling.DingTalkGroupPoller(manager, inbound, clock=lambda: now[0], config_loader=lambda: {})
    return SimpleNamespace(**locals())


def _message(message_id='message-1', time='2026-10-30 20:00:01'):
    return {'messageId': message_id, 'openMessageId': message_id, 'openConversationId': 'cid-group-0',
            'senderOpenDingTalkId': 'person', 'sendType': 'user', 'text': 'ordinary message', 'createTime': time}


@pytest.mark.asyncio
async def test_same_account_reads_once_and_fans_out_to_authorized_employees(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    f.response['messages'] = [_message()]
    assert await f.poller.poll_once() == 1
    assert len(f.reads) == 1
    assert {item[0] for item in f.delivered} == {'agent-0', 'agent-1'}
    assert all(item[3] == f.now[0] for item in f.delivered)


@pytest.mark.asyncio
async def test_shared_reader_does_not_advance_past_a_temporarily_unavailable_employee(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    messages = []
    available = {'agent-0': True, 'agent-1': True}

    async def identity(scope):
        if not available[scope.agent_id]:
            raise PersistentAgentError('temporary identity failure', code='DINGTALK_CLI_GROUP_HISTORY_UNAVAILABLE')
        return f.identities[scope.agent_id]

    async def read(scope, *, conversation_id, since):
        boundary = datetime.fromisoformat(since)
        return {**f.response, 'messages': [m for stamp, m in messages if stamp > boundary]}

    f.manager.dingtalk_cli_runtime.group_history_identity = identity
    f.manager.dingtalk_cli_runtime.read_group_history = read
    await f.poller.poll_once()
    start = f.now[0]
    available['agent-1'] = False
    f.now[0] = start + timedelta(seconds=10)
    await f.poller.poll_once()
    messages.append((start + timedelta(seconds=20), _message(time='2026-10-30 20:00:20')))
    f.now[0] = start + timedelta(seconds=30)
    await f.poller.poll_once()
    f.now[0] = start + timedelta(seconds=60)
    await f.poller.poll_once()
    f.now[0] = start + timedelta(seconds=90)
    await f.poller.poll_once()
    available['agent-1'] = True
    f.now[0] = start + timedelta(seconds=200)
    await f.poller.poll_once()
    assert {item[0] for item in f.delivered if item[1] == 'message-1'} == {'agent-0', 'agent-1'}


@pytest.mark.asyncio
async def test_new_user_authorization_is_rechecked_on_the_next_cycle(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch, agents=1)
    f.identities['agent-0'] = None
    await f.poller.poll_once()
    f.identities['agent-0'] = 'a' * 64
    f.now[0] += timedelta(seconds=10)
    assert await f.poller.poll_once() == 1


@pytest.mark.asyncio
async def test_recovered_employee_pages_forward_even_when_shared_checkpoint_is_ahead(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    await f.poller.poll_once()
    key = next(iter(f.poller._progress))
    recipient_key = (*key, 'agent-1', 'app-1')
    start = f.now[0]
    f.now[0] += timedelta(minutes=5)
    f.poller._progress[key] = f.now[0] - timedelta(seconds=30)
    f.poller._recipient_progress[(*key, 'agent-0', 'app-0')] = f.poller._progress[key]
    old_boundary = f.poller._recipient_progress[recipient_key]
    f.response.update(messages=[_message(time='2026-10-30 20:00:20')], complete=False, hasMore=True,
                      nextCursor=int((start + timedelta(seconds=20)).timestamp() * 1000))
    await f.poller.poll_once()
    partial_boundary = start + timedelta(seconds=20) - timedelta(milliseconds=1)
    assert f.reads[-1][2] == old_boundary.isoformat()
    assert f.poller._recipient_progress[recipient_key] == partial_boundary
    assert f.poller._progress[key] == f.now[0] - timedelta(seconds=30)
    f.response.update(messages=[_message('message-2', time='2026-10-30 20:00:40')], complete=True, hasMore=False)
    await f.poller.poll_once()
    assert f.reads[-1][2] == partial_boundary.isoformat()
    assert ('agent-1', 'message-2') in {(row[0], row[1]) for row in f.delivered}
    assert f.poller._recipient_progress[recipient_key] == f.now[0] - timedelta(seconds=30)


@pytest.mark.asyncio
async def test_verified_bot_senders_reconcile_existing_archive_even_without_new_messages(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    reconciled = []
    f.manager.group_archive = SimpleNamespace(
        reconcile_dingtalk_deliveries=lambda ns, group, senders, **coverage: reconciled.append((ns, group, senders)),
    )
    f.response['botSenders'] = {'app-0': 'open-bot-0', 'unbound-bot': 'open-other'}
    assert await f.poller.poll_once() == 1
    assert reconciled == [('local', 'group-0', {'app-0': 'dingtalk-open:open-bot-0'})]


@pytest.mark.asyncio
@pytest.mark.parametrize('complete,has_more', [(False, True), (False, False), (True, True)])
async def test_partial_history_never_merges_temporarily_unique_echoes(tmp_path, monkeypatch, complete, has_more):
    f = _fixture(tmp_path, monkeypatch)
    f.response.update(botSenders={'app-0': 'open-bot'}, complete=complete, hasMore=has_more)
    reconciled = []
    f.manager.group_archive = SimpleNamespace(
        reconcile_dingtalk_deliveries=lambda *args, **kwargs: reconciled.append(args),
    )
    await f.poller.poll_once()
    assert not reconciled


@pytest.mark.asyncio
async def test_different_accounts_are_never_coalesced(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    f.identities['agent-1'] = 'b' * 64
    assert await f.poller.poll_once() == 2
    assert {item[0] for item in f.reads} == {'agent-0', 'agent-1'}


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['passive', 'disabled', 'unbound', 'revoked', 'wrong_app', 'no_auth'])
async def test_ineligible_employees_never_read_history(tmp_path, monkeypatch, invalid):
    f = _fixture(tmp_path, monkeypatch, agents=1)
    if invalid == 'passive':
        f.modes['agent-0'] = 'passive'
    elif invalid == 'disabled':
        f.enabled['agent-0'] = False
    elif invalid == 'unbound':
        f.bindings.clear()
    elif invalid == 'revoked':
        f.accesses['agent-0'].clear()
    elif invalid == 'wrong_app':
        f.accesses['agent-0'][0].app_id = 'other-app'
    else:
        f.identities['agent-0'] = None
    assert await f.poller.poll_once() == 0
    assert not f.reads


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['revoked', 'passive', 'account', 'unbound'])
async def test_access_and_account_are_revalidated_after_read(tmp_path, monkeypatch, change):
    f = _fixture(tmp_path, monkeypatch, agents=1)
    f.response['messages'] = [_message()]

    async def revoke():
        if change == 'revoked':
            f.accesses['agent-0'].clear()
        elif change == 'passive':
            f.modes['agent-0'] = 'passive'
        elif change == 'account':
            f.identities['agent-0'] = 'b' * 64
        else:
            f.bindings.clear()

    f.hooks.before_read = revoke
    await f.poller.poll_once()
    assert not f.delivered


@pytest.mark.asyncio
@pytest.mark.parametrize('disposition', [InboundDisposition.REJECTED, InboundDisposition.RETRYABLE])
async def test_rejected_batch_never_advances_checkpoint(tmp_path, monkeypatch, disposition):
    f = _fixture(tmp_path, monkeypatch, agents=1)
    f.response['messages'] = [_message()]
    f.hooks.disposition = disposition
    await f.poller.poll_once()
    f.now[0] += timedelta(minutes=2)
    await f.poller.poll_once()
    assert len(f.reads) == 2
    assert f.reads[0][2] == f.reads[1][2]


@pytest.mark.asyncio
async def test_restart_restores_checkpoint_but_uses_new_dispatch_floor_and_private_state(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch, agents=1)
    await f.poller.poll_once()
    f.now[0] += timedelta(minutes=5)
    f.response['messages'] = [_message()]
    restored = dingtalk_group_polling.DingTalkGroupPoller(
        f.manager, f.inbound, clock=lambda: f.now[0], config_loader=lambda: {},
    )
    await restored.poll_once()
    assert f.reads[1][2] == '2026-10-30T11:59:30+00:00'
    assert f.delivered[0][3] == datetime(2026, 10, 30, 12, 5, tzinfo=timezone.utc)
    root = tmp_path / 'dingtalk_group_polling'
    files = list(root.glob('*.json'))
    assert len(files) == 1
    assert 'ordinary message' not in files[0].read_text()
    assert 'person' not in files[0].read_text()
    assert json.loads(files[0].read_text())
    if os.name != 'nt':
        assert root.stat().st_mode & 0o777 == 0o700
        assert files[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_new_group_floor_is_observation_time(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch, agents=1, groups=0)
    await f.poller.poll_once()
    f.now[0] += timedelta(minutes=5)
    f.accesses['agent-0'].append(SimpleNamespace(group_id='group-0', app_id='app-0'))
    f.response['messages'] = [_message()]
    await f.poller.poll_once()
    assert f.delivered[0][3] == f.now[0]


@pytest.mark.asyncio
async def test_partial_response_advances_only_to_safe_message_boundary(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch, agents=1)
    f.response.update(messages=[_message()], complete=False, hasMore=True)
    await f.poller.poll_once()
    f.now[0] += timedelta(seconds=10)
    await f.poller.poll_once()
    boundary = datetime.fromisoformat(f.reads[1][2])
    assert datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc) <= boundary
    assert boundary < datetime(2026, 10, 30, 12, 0, 1, tzinfo=timezone.utc)
    assert boundary < f.now[0] - timedelta(seconds=1)


@pytest.mark.asyncio
async def test_stalled_cursor_and_request_errors_back_off_without_killing_poller(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch, agents=1)
    f.response.update(complete=False, hasMore=True)
    await f.poller.poll_once()
    await f.poller.poll_once()
    assert len(f.reads) == 1
    f.now[0] += timedelta(minutes=10)
    f.hooks.error = RuntimeError('sensitive response body')
    await f.poller.poll_once()
    await f.poller.poll_once()
    assert len(f.reads) == 2
    f.hooks.error = None
    f.now[0] += timedelta(minutes=10)
    assert await f.poller.poll_once() == 1


@pytest.mark.asyncio
async def test_budget_is_fair_to_groups_after_first_eight(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch, agents=1, groups=12)
    assert await f.poller.poll_once() == 8
    assert await f.poller.poll_once() == 8
    assert len({item[1] for item in f.reads}) == 12


@pytest.mark.asyncio
async def test_shutdown_cancels_inflight_read_and_start_is_idempotent(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch, agents=1)
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def block():
        entered.set()
        await hold.wait()

    f.hooks.before_read = block
    f.response['messages'] = [_message()]
    await f.poller.start()
    await f.poller.start()
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(f.poller.aclose(), 2)
    hold.set()
    assert await f.poller.poll_once() == 0
    assert len(f.reads) == 1
    assert not f.delivered


@pytest.mark.asyncio
async def test_explicit_partial_cursor_retains_boundary_overlap(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch, agents=1)
    cursor = datetime(2026, 10, 30, 11, 59, 50, tzinfo=timezone.utc)
    f.response.update(complete=False, hasMore=True, nextCursor=int(cursor.timestamp() * 1000))
    await f.poller.poll_once()
    await f.poller.poll_once()
    assert f.reads[1][2] == '2026-10-30T11:59:49.999000+00:00'
    await f.poller.poll_once()
    assert len(f.reads) == 2


@pytest.mark.asyncio
async def test_missing_authorization_recovers_without_an_error_backoff(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch, agents=1)
    f.identities['agent-0'] = None
    assert await f.poller.poll_once() == 0
    f.identities['agent-0'] = 'a' * 64
    assert await f.poller.poll_once() == 1
    f.now[0] += timedelta(minutes=5)
    assert await f.poller.poll_once() == 1


@pytest.mark.asyncio
async def test_one_group_failure_does_not_prevent_other_groups_or_leak_log_text(tmp_path, monkeypatch, caplog):
    f = _fixture(tmp_path, monkeypatch, agents=1, groups=2)
    original_read = f.manager.dingtalk_cli_runtime.read_group_history

    async def read(scope, *, conversation_id, since):
        if conversation_id == 'cid-group-0':
            raise RuntimeError('SECRET message text and credential')
        return await original_read(scope, conversation_id=conversation_id, since=since)

    f.manager.dingtalk_cli_runtime.read_group_history = read
    assert await f.poller.poll_once() == 2
    assert len(f.reads) == 1
    assert f.reads[0][1] == 'cid-group-1'
    assert 'SECRET' not in caplog.text
    assert list((tmp_path / 'dingtalk_group_polling').glob('*.json'))


@pytest.mark.asyncio
async def test_mismatched_reader_response_is_never_distributed(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch, agents=1)

    async def foreign_response(scope, *, conversation_id, since):
        return {**f.response, 'readerKey': 'another-account', 'messages': [_message()]}

    f.manager.dingtalk_cli_runtime.read_group_history = foreign_response
    await f.poller.poll_once()
    assert not f.delivered
    assert not (tmp_path / 'dingtalk_group_polling').exists()


@pytest.mark.asyncio
async def test_slow_failed_request_backs_off_from_failure_time(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch, agents=1)
    attempts = []

    async def slow_failure(scope, *, conversation_id, since):
        attempts.append(conversation_id)
        f.now[0] += timedelta(minutes=5)
        raise RuntimeError('network failure')

    f.manager.dingtalk_cli_runtime.read_group_history = slow_failure
    await f.poller.poll_once()
    await f.poller.poll_once()
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_temporary_identity_failure_and_backoff_preserve_dispatch_floor(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch, agents=1)
    floor = f.now[0]
    await f.poller.poll_once()
    original_identity = f.manager.dingtalk_cli_runtime.group_history_identity

    async def identity_failure(scope):
        raise RuntimeError('temporary network outage')

    f.manager.dingtalk_cli_runtime.group_history_identity = identity_failure
    f.now[0] += timedelta(seconds=10)
    assert await f.poller.poll_once() == 0
    f.manager.dingtalk_cli_runtime.group_history_identity = original_identity
    f.now[0] += timedelta(seconds=10)
    assert await f.poller.poll_once() == 0
    f.now[0] += timedelta(minutes=1)
    f.response['messages'] = [_message()]
    assert await f.poller.poll_once() == 1
    assert f.delivered[0][3] == floor


@pytest.mark.asyncio
@pytest.mark.parametrize('revocation', ['disabled', 'revoked', 'unbound', 'no_auth'])
async def test_confirmed_revocation_still_resets_dispatch_floor(tmp_path, monkeypatch, revocation):
    f = _fixture(tmp_path, monkeypatch, agents=1)
    await f.poller.poll_once()
    f.now[0] += timedelta(minutes=1)
    binding = f.bindings[0]
    access = f.accesses['agent-0'][0]
    if revocation == 'disabled':
        f.enabled['agent-0'] = False
    elif revocation == 'revoked':
        f.accesses['agent-0'].clear()
    elif revocation == 'unbound':
        f.bindings.clear()
    else:
        f.identities['agent-0'] = None
    assert await f.poller.poll_once() == 0
    f.enabled['agent-0'] = True
    f.accesses['agent-0'] = [access]
    f.bindings[:] = [binding]
    f.identities['agent-0'] = 'a' * 64
    f.now[0] += timedelta(minutes=1)
    f.response['messages'] = [_message()]
    assert await f.poller.poll_once() == 1
    assert f.delivered[0][3] == f.now[0]


@pytest.mark.asyncio
async def test_bot_list_excludes_absent_bot_revokes_cached_access_and_resets_floor(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    f.response['messages'] = [_message()]
    await f.poller.poll_once()
    f.delivered.clear()
    f.now[0] += timedelta(minutes=1)
    f.response['botCodes'] = ['app-0']
    await f.poller.poll_once()
    assert {row[0] for row in f.delivered} == {'agent-0'}
    assert not f.accesses['agent-1']
    assert f.membership_updates[0][:5] == ('local', 'agent-1', 'group-0', 'app-1', 'left')
    assert f.membership_updates[0][5] == f.now[0]
    assert f.membership_updates[0][6].startswith('dingtalk:bot-list:')
    f.accesses['agent-1'] = [SimpleNamespace(group_id='group-0', app_id='app-1')]
    f.response['botCodes'] = ['app-0', 'app-1']
    f.now[0] += timedelta(minutes=1)
    f.delivered.clear()
    await f.poller.poll_once()
    assert next(row[3] for row in f.delivered if row[0] == 'agent-1') == f.now[0]


@pytest.mark.asyncio
async def test_absent_reader_falls_back_to_same_account_and_remembers_success(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    original_read = f.manager.dingtalk_cli_runtime.read_group_history
    attempts = []

    async def read(scope, *, conversation_id, since):
        attempts.append(scope.agent_id)
        if scope.agent_id == 'agent-0':
            raise PersistentAgentError('absent', code='DINGTALK_CLI_GROUP_BOT_NOT_PRESENT')
        return await original_read(scope, conversation_id=conversation_id, since=since)

    f.manager.dingtalk_cli_runtime.read_group_history = read
    f.response.update(botCodes=['app-1'], messages=[_message()])
    assert await f.poller.poll_once() == 2
    assert attempts == ['agent-0', 'agent-1']
    assert {row[0] for row in f.delivered} == {'agent-1'}
    assert f.membership_updates[0][1:5] == ('agent-0', 'group-0', 'app-0', 'left')
    f.accesses['agent-0'] = [SimpleNamespace(group_id='group-0', app_id='app-0')]
    assert await f.poller.poll_once() == 1
    assert attempts == ['agent-0', 'agent-1', 'agent-1']


@pytest.mark.asyncio
async def test_unavailable_reader_does_not_retry_other_scopes_or_revoke_access(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    f.hooks.error = PersistentAgentError('network failure', code='DINGTALK_CLI_GROUP_HISTORY_UNAVAILABLE')
    assert await f.poller.poll_once() == 1
    assert len(f.reads) == 1
    assert not f.membership_updates
    assert all(f.accesses.values())


@pytest.mark.asyncio
async def test_all_absent_bots_stop_polling_without_advancing_checkpoint(tmp_path, monkeypatch, caplog):
    f = _fixture(tmp_path, monkeypatch)
    f.hooks.error = PersistentAgentError('SECRET', code='DINGTALK_CLI_GROUP_BOT_NOT_PRESENT')
    assert await f.poller.poll_once() == 2
    assert await f.poller.poll_once() == 0
    assert len(f.membership_updates) == 2
    assert not f.delivered
    assert not (tmp_path / 'dingtalk_group_polling').exists()
    assert 'SECRET' not in caplog.text


@pytest.mark.asyncio
async def test_reader_failover_shares_strict_global_eight_attempt_budget(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch, agents=10, groups=2)
    f.hooks.error = PersistentAgentError('absent', code='DINGTALK_CLI_GROUP_BOT_NOT_PRESENT')
    assert await f.poller.poll_once() == 8
    assert len(f.reads) == 8
    assert len({(row[0], row[1]) for row in f.reads}) == 8
    assert await f.poller.poll_once() == 8
    assert len(f.reads) == 16
    assert len({row[1] for row in f.reads}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('bot_codes', [None, 'app-0', [123]])
async def test_unverified_bot_list_is_never_distributed_or_used_to_revoke(tmp_path, monkeypatch, bot_codes):
    f = _fixture(tmp_path, monkeypatch)
    f.response.update(botCodes=bot_codes, messages=[_message()])
    assert await f.poller.poll_once() == 1
    assert not f.delivered
    assert not f.membership_updates


@pytest.mark.asyncio
async def test_removed_binding_during_bot_read_does_not_change_other_app_membership(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch, agents=1)

    async def replace_binding():
        f.bindings.clear()
        f.connections['agent-0'] = SimpleNamespace(channel='dingtalk', connection_key='replacement-app')

    f.hooks.before_read = replace_binding
    f.response.update(botCodes=[], messages=[_message()])
    assert await f.poller.poll_once() == 1
    assert not f.membership_updates
    assert not f.delivered


@pytest.mark.asyncio
async def test_verified_empty_bot_list_retires_all_recipients_without_checkpoint(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    f.response.update(botCodes=[], messages=[_message()])
    assert await f.poller.poll_once() == 1
    assert len(f.membership_updates) == 2
    assert not any(f.accesses.values())
    assert not f.delivered
    assert not (tmp_path / 'dingtalk_group_polling').exists()
