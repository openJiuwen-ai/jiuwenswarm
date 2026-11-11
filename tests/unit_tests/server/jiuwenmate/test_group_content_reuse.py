"""Once consent follows exact content within one request, never query wording."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest

from openjiuwen.harness import persistent_agent as core
from tests.unit_tests.server.jiuwenmate.test_group_share_repository import (
    NOW, approve, create, decide, repository, result, scope,
)
from tests.unit_tests.server.jiuwenmate.test_group_sharing_service import sharing
from tests.unit_tests.server.jiuwenmate.test_cli_group_read import cli_host, decide_cli


def fresh_query(repo, snapshot=None, bound_scope=None):
    bound_scope = bound_scope or scope(query={**scope()['query'], 'limit': 200})
    row = create(repo, bound_scope, 'b' * 64, snapshot or result(bound_scope))
    return repo.authorize_from_grants(row['approval_id'], now=NOW)


def test_same_request_changed_query_reuses_approved_content_without_new_owner_card(tmp_path):
    repo = repository(tmp_path)
    approve(repo, decision='once')
    row = fresh_query(repo)
    assert row['status'] == 'approved_once'
    assert row['decision'] == 'reused_once'
    assert not repo.pending_for_request('local', 'request-1')
    assert len(repo.list_for_owner('local', 'config-1')['approvals']) == 1
    assert not repo.list_for_owner('local', 'config-1')['grants']
    assert repository(tmp_path).get(row['approval_id'])['status'] == 'approved_once'


def test_partial_overlap_previews_only_new_exact_messages_but_freezes_full_result(tmp_path):
    repo = repository(tmp_path)
    approve(repo, decision='once')
    frozen = result(texts=('机密甲', '机密乙', '新增内容'))
    row = fresh_query(repo, frozen)
    assert row['status'] == 'pending'
    view = repo.list_for_owner('local', 'config-1')['approvals'][-1]
    assert [item['text'] for item in view['messages']] == ['新增内容']
    assert '新增内容' in view['summary'] and '机密甲' not in view['summary']
    assert repo.get(row['approval_id'])['result'] == frozen


@pytest.mark.parametrize('field,value', [
    ('text', '已修改'), ('sender_id', 'another-sender'), ('sender_kind', 'agent'),
    ('attachment_refs', ['different-file']), ('mention_ids', ['different-user']),
    ('received_at', '2026-10-27T08:00:01Z'), ('reply_to', 'another-message'),
])
def test_same_message_id_does_not_cover_changed_exact_content(tmp_path, field, value):
    repo = repository(tmp_path)
    approve(repo, decision='once')
    frozen = result()
    frozen['hits'][0]['message'][field] = value
    row = fresh_query(repo, frozen)
    assert row['status'] == 'pending'
    view = repo.list_for_owner('local', 'config-1')['approvals'][-1]
    assert [item['message_id'] for item in view['messages']] == ['message-0']


@pytest.mark.parametrize('dimension', ['request_id', 'agent_id', 'config_id', 'principal_ref', 'target',
                                       'source_authorizations', 'session_id'])
def test_once_reuse_cannot_cross_trusted_scope(tmp_path, dimension):
    repo = repository(tmp_path)
    approve(repo, decision='once')
    bound = scope(query={**scope()['query'], 'limit': 200})
    if dimension == 'target':
        bound['target'] = {**bound['target'], 'id': 'other-target'}
    elif dimension == 'source_authorizations':
        bound[dimension] = {'source-a': {'app_id': 'other-app'}}
    elif dimension != 'session_id':
        bound[dimension] = 'other'
    frozen = result(bound)
    if dimension == 'session_id':
        frozen['session_id'] = 'other-session'
    assert fresh_query(repo, frozen, bound)['status'] == 'pending'


@pytest.mark.parametrize('decision', [None, 'reject'])
def test_pending_or_rejected_content_cannot_be_reused(tmp_path, decision):
    repo = repository(tmp_path)
    row = create(repo)
    if decision:
        repo.bind_native('local', 'request-1', 'native-private')
        decide(repo, row, decision)
    assert fresh_query(repo)['status'] == 'pending'


def test_expired_pending_content_cannot_be_reused(tmp_path):
    repo = repository(tmp_path)
    create(repo)
    repo.expire_pending(NOW + timedelta(minutes=16))
    assert fresh_query(repo)['status'] == 'pending'


def test_revoked_permanent_grant_cannot_be_converted_to_once_reuse(tmp_path):
    repo = repository(tmp_path)
    approve(repo, decision='permanent')
    grant, = repo.list_for_owner('local', 'config-1')['grants']
    repo.revoke(grant['grant_id'], namespace_id='local', config_id='config-1',
                owner_principal='local', now=NOW)
    assert fresh_query(repo)['status'] == 'pending'


def test_approved_subset_and_presentation_changes_do_not_require_new_consent(tmp_path):
    repo = repository(tmp_path)
    approve(repo, decision='once')
    frozen = result(texts=('机密甲',))
    frozen['hits'][0]['message_ref'] = 'other-transport-reference'
    frozen['hits'][0]['message']['sender_name'] = 'updated contact display name'
    assert fresh_query(repo, frozen)['status'] == 'approved_once'


def test_concurrent_rechecks_do_not_publish_duplicate_owner_cards(tmp_path):
    repo = repository(tmp_path)
    approve(repo, decision='once')
    bound = scope(query={**scope()['query'], 'limit': 200})
    row = create(repo, bound, 'b' * 64, result(bound))
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: repo.authorize_from_grants(row['approval_id'], now=NOW), range(4)))
    assert all(item['status'] == 'approved_once' for item in rows)
    assert len(repo.list_for_owner('local', 'config-1')['approvals']) == 1


def test_internal_reuse_does_not_advance_the_context_window_boundary(tmp_path):
    repo = repository(tmp_path)
    approve(repo, decision='once')
    before = repo.window_generation('local', 'agent-1', 'session-1', request_id='next-request')
    bound = scope(query={**scope()['query'], 'limit': 200})
    row = create(repo, bound, 'b' * 64, result(bound))
    repo.authorize_from_grants(row['approval_id'], now=NOW + timedelta(minutes=1))
    assert repo.window_generation('local', 'agent-1', 'session-1', request_id='next-request') == before


def artifact_result(kind):
    frozen = result()
    frozen['hits'] = []
    item = {'group_id': 'source-a', 'group_name': '研发群', 'artifact_id': 'artifact-1',
            'artifact_kind': kind, 'title': 'report', 'created_at': NOW.isoformat()}
    if kind == 'file':
        item.update(file_name='report.txt', file_size=10, file_digest='sha256:' + 'a' * 64,
                    source_session_id='source-session', source_file_path='sessions/source-session/output/report.txt')
    else:
        item.update(url='https://example.feishu.cn/docx/report', snippet='report summary')
    frozen['artifacts'] = [item]
    return core.GroupMessageSearchResult.model_validate(frozen).model_dump(mode='json')


@pytest.mark.parametrize('kind', ['file', 'online_doc'])
def test_exact_artifacts_reuse_once_consent(tmp_path, kind):
    repo = repository(tmp_path)
    frozen = artifact_result(kind)
    row = create(repo, frozen=frozen)
    repo.bind_native('local', 'request-1', 'native-private')
    decide(repo, row, 'once')
    assert fresh_query(repo, frozen)['status'] == 'approved_once'


@pytest.mark.parametrize('kind,field,value', [
    ('file', 'file_digest', 'sha256:' + 'b' * 64), ('file', 'file_size', 11),
    ('online_doc', 'url', 'https://example.feishu.cn/docx/changed'),
    ('online_doc', 'snippet', 'changed summary'),
])
def test_changed_artifact_requires_new_consent(tmp_path, kind, field, value):
    repo = repository(tmp_path)
    frozen = artifact_result(kind)
    row = create(repo, frozen=frozen)
    repo.bind_native('local', 'request-1', 'native-private')
    decide(repo, row, 'once')
    frozen['artifacts'][0][field] = value
    assert fresh_query(repo, frozen)['status'] == 'pending'


@pytest.mark.asyncio
async def test_search_parameter_change_releases_same_approved_message(sharing):
    with pytest.raises(core.GroupContentApprovalRequired):
        await sharing.port.search_messages(sharing.execution, sharing.query)
    row, = sharing.repo.pending_for_request('local', 'request-1')
    sharing.repo.bind_native('local', 'request-1', 'native-1')
    sharing.repo.decide(row['approval_id'], namespace_id='local', config_id='config-1',
                        owner_principal='local', decision='once', now=NOW)
    query = sharing.query.model_copy(update={'limit': 200})
    released = await sharing.port.search_messages(replace(sharing.execution, execution_id='next'), query)
    assert released.hits == sharing.result.hits
    assert len(sharing.repo.list_for_owner('local', 'config-1')['approvals']) == 1


async def approve_search(host, message):
    hit = host.result.hits[0].model_copy(update={'message': message})
    host.delegate.search_messages.return_value = host.result.model_copy(update={'hits': (hit,)})
    host.archive.current_messages = lambda *args: (message,)
    with pytest.raises(core.GroupContentApprovalRequired):
        await host.port.search_messages(host.execution, host.query)
    await decide_cli(host, 'once')


@pytest.mark.asyncio
@pytest.mark.parametrize('search_first', [True, False])
async def test_cli_and_search_share_exact_once_consent(cli_host, search_first):
    from jiuwenswarm.server.jiuwenmate.cli_group_sharing import _message
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read

    message = _message(cli_host.raw)
    command = 'lark-cli im +chat-messages-list --chat-id oc_source'
    if search_first:
        await approve_search(cli_host, message)
        released = await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
        assert released['ok'] and released['data']['messages'][0]['text'] == message.text
    else:
        with pytest.raises(core.GroupContentApprovalRequired):
            await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
        await decide_cli(cli_host, 'once')
        hit = cli_host.result.hits[0].model_copy(update={'message': message})
        cli_host.delegate.search_messages.return_value = cli_host.result.model_copy(update={'hits': (hit,)})
        cli_host.archive.current_messages = lambda *args: (message,)
        released = await cli_host.port.search_messages(cli_host.execution, cli_host.query)
        assert released.hits[0].message.text == message.text
    assert len(cli_host.repo.list_for_owner('local', 'config-1')['approvals']) == 1


@pytest.mark.asyncio
async def test_cli_query_change_reuses_once_receipt(cli_host):
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read

    command = 'lark-cli im +chat-messages-list --chat-id oc_source'
    with pytest.raises(core.GroupContentApprovalRequired):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    await decide_cli(cli_host, 'once')
    changed = command + ' --page-size 10'
    released = await cli_host.handler.execute(cli_host.execution, changed, parse_group_read(changed))
    assert released['ok']
    assert len(cli_host.repo.list_for_owner('local', 'config-1')['approvals']) == 1


@pytest.mark.asyncio
async def test_cli_mention_shortcut_rehydrates_exact_raw_identity_before_reuse(cli_host):
    import json
    from unittest.mock import AsyncMock
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read

    original = core.GroupMessage(message_id='om_source', sender_id='user-1', sender_name='Alice',
                                 sender_kind='human', received_at=NOW, text='@Bob unchanged body',
                                 mention_ids=('ou_bob',))
    await approve_search(cli_host, original)
    display = {**cli_host.raw, 'content': '@_user_1 unchanged body',
               'sender': {'id': 'presentation-id', 'name': 'presentation'}}
    raw = {**cli_host.raw, 'content': json.dumps({'text': '@_user_1 unchanged body'}),
           'mentions': [{'key': '@_user_1', 'name': 'Bob', 'id': {'open_id': 'ou_bob'}}]}

    async def run(namespace, agent, argv):
        return {'ok': True, 'data': {'messages': [raw if argv[0] == 'api' else display]}}

    cli_host.cli.run_group_read = AsyncMock(side_effect=run)
    command = 'lark-cli im +chat-messages-list --chat-id oc_source'
    released = await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    assert released['ok'] and released['data']['messages'][0]['text'] == original.text
    assert len(cli_host.repo.list_for_owner('local', 'config-1')['approvals']) == 1
    assert any(call.args[2][0] == 'api' for call in cli_host.cli.run_group_read.await_args_list)
