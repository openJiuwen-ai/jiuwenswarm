"""Revocation covers private imports and retained model context, not published copies."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.harness import persistent_agent as core
from jiuwenswarm.server.jiuwenmate.session_artifact_sharing import SessionArtifactSharing
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from tests.unit_tests.server.jiuwenmate.test_session_artifact_sharing import artifact_host
from tests.unit_tests.server.jiuwenmate.test_group_sharing_service import sharing, NOW, approve
from tests.unit_tests.server.jiuwenmate.test_cli_group_read import cli_host, decide_cli


async def imported(host):
    source = host.source
    host.sessions.session_artifact_candidates = lambda *args: (source,)
    hit = core.GroupArtifactSearchHit(
        group_id=source['source_group_id'], group_name='Source group', artifact_id=source['artifact_id'],
        artifact_kind='file', title='report.txt', file_name='report.txt', file_size=12,
        file_digest=source['file_ref'], source_session_id='source-session',
        source_file_path=source['file_path'], created_at=NOW - timedelta(hours=1),
    )
    result = host.result.model_copy(update={'hits': (), 'artifacts': (hit,)})
    query = core.GroupMessageQuery(group_ids=('source-group',), content_types=('files',))
    sources = host.delegate.sharing_sources.return_value
    with pytest.raises(core.GroupContentApprovalRequired):
        await host.gate.authorize(host.execution, query, result, sources)
    row, = host.repo.pending_for_request('local', 'request-1')
    host.repo.bind_native('local', 'request-1', 'native')
    host.repo.decide(row['approval_id'], namespace_id='local', config_id='config-1',
                     owner_principal='local', decision='permanent', now=NOW)
    await host.gate.authorize(host.execution, query, result, sources)
    grant, = host.repo.list_for_owner('local', 'config-1', owner_principal='local')['grants']
    host.repo.revoke(grant['grant_id'], namespace_id='local', config_id='config-1',
                    owner_principal='local', now=NOW + timedelta(seconds=1))
    host.record.request_id = 'request-2'
    return replace(host.execution, request_id='request-2', execution_id='execution-2')


@pytest.mark.parametrize('tool', ['read_file', 'bash'])
async def test_private_import_requires_new_approval_after_revoke(artifact_host, tool):
    execution = await imported(artifact_host)
    path = 'sessions/session-1/imports/art_original_report.txt'
    args = {'command': f'cat "{path}"'} if tool == 'bash' else {'file_path': path}
    guard = SessionArtifactSharing(artifact_host.gate, artifact_host.delegate)
    with pytest.raises(core.GroupContentApprovalRequired):
        await guard.prepare(execution, tool, args)
    row, = artifact_host.repo.pending_for_request('local', 'request-2')
    assert len(row['source_groups']) == 1
    artifact_host.repo.bind_native('local', 'request-2', 'native-2')
    artifact_host.repo.decide(row['approval_id'], namespace_id='local', config_id='config-1',
                             owner_principal='local', decision='reject', now=NOW)
    output = await guard.prepare(execution, tool, args)
    assert output is not None and not output.success


async def test_unknown_import_fails_closed(artifact_host):
    guard = SessionArtifactSharing(artifact_host.gate, artifact_host.delegate)
    result = await guard.prepare(artifact_host.execution, 'read_file', {'file_path': 'imports/unknown.txt'})
    assert result is not None and not result.success


async def test_once_approved_import_can_be_reused_in_the_same_request(artifact_host):
    execution = await imported(artifact_host)
    guard = SessionArtifactSharing(artifact_host.gate, artifact_host.delegate)
    args = {'file_path': 'imports/art_original_report.txt'}
    with pytest.raises(core.GroupContentApprovalRequired):
        await guard.prepare(execution, 'read_file', args)
    row, = artifact_host.repo.pending_for_request('local', 'request-2')
    artifact_host.repo.bind_native('local', 'request-2', 'native-2')
    artifact_host.repo.decide(row['approval_id'], namespace_id='local', config_id='config-1',
                             owner_principal='local', decision='once', now=NOW)
    assert await guard.prepare(execution, 'read_file', args) is None
    assert await guard.prepare(execution, 'bash', {'command': 'cp imports/art_original_report.txt output/copy.txt'}) is None
    resumed = replace(execution, execution_id='execution-3')
    assert await guard.prepare(resumed, 'read_file', args) is None
    assert not artifact_host.repo.pending_for_request('local', 'request-2')
    mixed = await guard.prepare(resumed, 'bash', {
        'command': 'cat imports/art_original_report.txt sessions/other-session/private.txt',
    })
    assert mixed is not None and not mixed.success


async def test_published_copy_stays_readable_after_revoke(artifact_host):
    execution = await imported(artifact_host)
    artifact_host.sessions.session_artifact_candidates = lambda *args: (artifact_host.source, artifact_host.cached)
    guard = SessionArtifactSharing(artifact_host.gate, artifact_host.delegate)
    assert await guard.prepare(execution, 'share_file', {'file_path': artifact_host.cached['file_path']}) is None
    assert await guard.prepare(execution, 'read_file', {'file_path': 'imports/art_original_report.txt'}) is None
    assert not artifact_host.repo.pending_for_request('local', 'request-2')


async def test_queued_copy_does_not_count_as_published(artifact_host):
    execution = await imported(artifact_host)
    queued = {**artifact_host.cached, 'published': False}
    artifact_host.sessions.session_artifact_candidates = lambda *args: (artifact_host.source, queued)
    guard = SessionArtifactSharing(artifact_host.gate, artifact_host.delegate)
    with pytest.raises(core.GroupContentApprovalRequired):
        await guard.prepare(execution, 'share_file', {'file_path': queued['file_path']})


async def test_public_file_in_command_does_not_exempt_private_import(artifact_host):
    execution = await imported(artifact_host)
    public = {**artifact_host.cached, 'file_ref': 'sha256:' + 'b' * 64, 'published': True}
    artifact_host.sessions.session_artifact_candidates = lambda *args: (artifact_host.source, public)
    guard = SessionArtifactSharing(artifact_host.gate, artifact_host.delegate)
    with pytest.raises(core.GroupContentApprovalRequired):
        await guard.prepare(execution, 'bash', {'command': 'cat output/report.txt imports/art_original_report.txt'})


async def test_import_copy_renamed_without_registration_is_still_guarded(artifact_host):
    execution = await imported(artifact_host)
    artifact_host.gate._files.current_file_fact = lambda *args: {
        'session_id': 'session-1', 'file_path': 'sessions/session-1/output/renamed.txt',
        'file_ref': artifact_host.source['file_ref'], 'file_size': 12, 'published': False,
    }
    guard = SessionArtifactSharing(artifact_host.gate, artifact_host.delegate)
    with pytest.raises(core.GroupContentApprovalRequired):
        await guard.prepare(execution, 'share_file', {'file_path': 'output/renamed.txt'})


async def test_inflight_request_cannot_publish_after_context_authority_is_revoked(sharing):
    await approve(sharing, 'permanent')
    sharing.repo.bind_request_window('local', 'agent-1', 'session-1', 'request-2')
    grant, = sharing.repo.list_for_owner('local', 'config-1', owner_principal='local')['grants']
    sharing.repo.revoke(grant['grant_id'], namespace_id='local', config_id='config-1',
                        owner_principal='local', now=NOW + timedelta(seconds=1))
    assert not await sharing.gate.can_deliver({'namespace_id': 'local', 'request_id': 'request-2'})
    sharing.repo.bind_request_window('local', 'agent-1', 'session-1', 'request-3')
    assert await sharing.gate.can_deliver({'namespace_id': 'local', 'request_id': 'request-3'})


async def test_revocation_changes_only_affected_session_window_generation(sharing):
    assert sharing.repo.window_generation('local', 'agent-1', 'session-1') == ''
    await approve(sharing, 'permanent')
    assert sharing.repo.window_generation('local', 'agent-1', 'session-1') == ''
    grant, = sharing.repo.list_for_owner('local', 'config-1', owner_principal='local')['grants']
    sharing.repo.revoke(grant['grant_id'], namespace_id='local', config_id='config-1',
                        owner_principal='local', now=NOW + timedelta(seconds=1))
    assert sharing.repo.window_generation('local', 'agent-1', 'session-1')
    assert sharing.repo.window_generation('local', 'agent-1', 'other-session') == ''


async def test_window_generation_hides_old_state_and_archives_without_deleting_audit(tmp_path):
    store = WindowBackendStore(tmp_path / 'agents.sqlite3')
    prefix = 'pa:agent-1:session-1:'
    old = store.bind('local', 'agent-1', 'session-1')
    await old.set(prefix + 'state', 'private message and derived summary')
    await old.set(prefix + 'archive:1', 'private search result')
    new = store.bind('local', 'agent-1', 'session-1', generation='revocation-1')
    assert await new.get(prefix + 'state') is None
    assert await new.get_by_prefix(prefix + 'archive:') == {}
    await new.set(prefix + 'state', 'clean state')
    await new.set(prefix + 'archive:1', 'new archive')
    assert await new.get_by_prefix('') == {prefix + 'state': 'clean state', prefix + 'archive:1': 'new archive'}
    assert await old.get(prefix + 'state') == 'private message and derived summary'
    await new.delete_by_prefix(prefix)
    assert await new.get(prefix + 'state') is None
    assert await old.get(prefix + 'archive:1') == 'private search result'


async def test_cli_import_reapproval_verifies_the_original_private_blob(cli_host):
    import json
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read

    cli_host.sessions.session_artifact_candidates = lambda *args: ()
    cli_host.raw.update(msg_type='file', content=json.dumps({'file_key': 'file_a', 'file_name': 'report.txt'}))
    command = 'lark-cli im +messages-resources-download --message-id om_source --file-key file_a --type file'
    with pytest.raises(core.GroupContentApprovalRequired):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    await decide_cli(cli_host, 'permanent')
    released = await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    path = released['data']['files'][0]['file_path']
    grant, = cli_host.repo.list_for_owner('local', 'config-1', owner_principal='local')['grants']
    cli_host.repo.revoke(grant['grant_id'], namespace_id='local', config_id='config-1',
                        owner_principal='local', now=NOW + timedelta(seconds=1))
    cli_host.record.request_id = 'request-2'
    execution = replace(cli_host.execution, request_id='request-2', execution_id='execution-2')
    guard = SessionArtifactSharing(cli_host.gate, cli_host.delegate)
    with pytest.raises(core.GroupContentApprovalRequired):
        await guard.prepare(execution, 'read_file', {'file_path': path})
    row, = cli_host.repo.pending_for_request('local', 'request-2')
    cli_host.repo.bind_native('local', 'request-2', 'native-2')
    cli_host.repo.decide(row['approval_id'], namespace_id='local', config_id='config-1',
                        owner_principal='local', decision='once', now=NOW)
    assert await guard.prepare(execution, 'read_file', {'file_path': path}) is None


async def test_new_message_search_after_revoke_requires_new_owner_decision(sharing):
    await approve(sharing, 'permanent')
    grant, = sharing.repo.list_for_owner('local', 'config-1', owner_principal='local')['grants']
    sharing.repo.revoke(grant['grant_id'], namespace_id='local', config_id='config-1',
                        owner_principal='local', now=NOW + timedelta(seconds=1))
    sharing.record.request_id = 'request-2'
    execution = replace(sharing.execution, request_id='request-2', execution_id='execution-2')
    sharing.delegate.search_messages.return_value = sharing.result.model_copy(
        update=core.InvocationRef.from_execution(execution).model_dump(),
    )
    with pytest.raises(core.GroupContentApprovalRequired):
        await sharing.port.search_messages(execution, sharing.query)
    assert len(sharing.repo.pending_for_request('local', 'request-2')) == 1


async def test_once_message_result_does_not_become_private_context_for_next_request(sharing):
    first = sharing.repo.bind_request_window('local', 'agent-1', 'session-1', 'request-1')
    await approve(sharing, 'once')
    assert sharing.repo.bind_request_window('local', 'agent-1', 'session-1', 'request-1') == first
    assert sharing.repo.request_window_is_current('local', 'request-1')
    second = sharing.repo.bind_request_window('local', 'agent-1', 'session-1', 'request-2')
    assert second != first
    assert sharing.repo.bind_request_window('local', 'agent-1', 'session-1', 'request-3') == second


def test_publication_requires_a_confirmed_non_redacted_delivery(artifact_host):
    from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository

    repo = ExecutionRepository(artifact_host.repo.db_path)
    row = {**artifact_host.cached, 'output_id': 'out-1'}
    with repo._transaction() as conn:
        columns = ','.join(row)
        placeholders = ','.join('?' for _ in row)
        conn.execute(f'INSERT INTO session_artifacts ({columns}) VALUES ({placeholders})', tuple(row.values()))
        conn.execute('INSERT INTO candidate_outputs VALUES (?,?,?,?,?,?,?,?,?)',
                     ('local', 'out-1', 'request-1', 'execution-1', 'event-1', 'target', 'attachment', '{}', NOW.isoformat()))
        conn.execute('INSERT INTO result_deliveries '
                     '(namespace_id,delivery_id,request_id,execution_id,output_id,channel,target_id,status,'
                     'idempotency_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                     ('local', 'delivery-1', 'request-1', 'execution-1', 'out-1', 'feishu', 'target', 'pending',
                      'idempotency', NOW.isoformat(), NOW.isoformat()))
    assert not repo.session_artifact_candidates('local', 'agent-1')[0]['published']
    with repo._transaction() as conn:
        conn.execute("UPDATE result_deliveries SET status='delivered', external_message_id='om-file'")
    assert repo.session_artifact_candidates('local', 'agent-1')[0]['published']
    with repo._transaction() as conn:
        conn.execute("UPDATE result_deliveries SET notice_text='redacted'")
    assert not repo.session_artifact_candidates('local', 'agent-1')[0]['published']


@pytest.mark.parametrize('saved_generation', [None, 'saved-generation'])
async def test_failure_cleanup_consumes_confirmation_with_optional_sharing(monkeypatch, saved_generation):
    from jiuwenswarm.server.jiuwenmate.execution_driver import ExecutionDriver

    consume = Mock()
    discard = AsyncMock()
    backend = object()
    driver = SimpleNamespace(
        _sessions=SimpleNamespace(record_execution_failure=Mock()), _clock=lambda: NOW,
        _permission_approvals=SimpleNamespace(
            resume_reply=Mock(return_value=SimpleNamespace(approval_id='native-1')), consume=consume,
        ),
        _group_sharing=(SimpleNamespace(repository=SimpleNamespace(
            saved_request_window=Mock(return_value=saved_generation),
        )) if saved_generation is not None else None),
        _windows=SimpleNamespace(bind=Mock(return_value=backend)),
    )
    record = SimpleNamespace(namespace_id='local', agent_id='agent-1', session_id='session-1',
                             request_id='request-1', config_fingerprint='a' * 64)
    monkeypatch.setattr(core, 'discard_persistent_confirmation', discard)
    await ExecutionDriver._record_failure(driver, record, SimpleNamespace(execution_id='execution-1'),
                                         reason='error', detail='injected failure')
    driver._windows.bind.assert_called_once_with('local', 'agent-1', 'session-1', generation=saved_generation or '')
    discard.assert_awaited_once_with(backend, agent_id='agent-1', session_id='session-1',
                                     request_id='request-1', spec_fingerprint='a' * 64)
    consume.assert_called_once_with('native-1')


async def test_later_once_approval_keeps_prior_request_delivery_and_resume_valid(sharing):
    from tests.unit_tests.server.jiuwenmate.test_group_sharing_service import delivery_job

    first = sharing.repo.bind_request_window('local', 'agent-1', 'session-1', 'request-1')
    await approve(sharing, 'once')
    assert await sharing.gate.can_deliver(delivery_job())
    sharing.repo.bind_request_window('local', 'agent-1', 'session-1', 'request-2')
    execution = replace(sharing.execution, request_id='request-2', execution_id='execution-2')
    result = sharing.result.model_copy(update=core.InvocationRef.from_execution(execution).model_dump())
    with pytest.raises(core.GroupContentApprovalRequired):
        await sharing.gate.authorize(execution, sharing.query, result, sharing.delegate.sharing_sources.return_value)
    row, = sharing.repo.pending_for_request('local', 'request-2')
    sharing.repo.bind_native('local', 'request-2', 'native-2')
    sharing.repo.decide(row['approval_id'], namespace_id='local', config_id='config-1',
                        owner_principal='local', decision='once', now=NOW + timedelta(seconds=1))
    assert sharing.repo.request_window_is_current('local', 'request-1')
    assert await sharing.gate.can_deliver(delivery_job())
    assert sharing.repo.bind_request_window('local', 'agent-1', 'session-1', 'request-1') == first
    assert sharing.repo.bind_request_window('local', 'agent-1', 'session-1', 'request-3') != first


async def test_nonempty_bound_generation_survives_later_once_and_restart_but_not_revocation(sharing):
    from jiuwenswarm.server.jiuwenmate.group_share_repository import GroupShareRepository
    from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

    await approve(sharing, 'once')
    bound = sharing.repo.bind_request_window('local', 'agent-1', 'session-1', 'request-2')
    assert bound
    execution = replace(sharing.execution, request_id='request-3', execution_id='execution-3')
    result = sharing.result.model_copy(update=core.InvocationRef.from_execution(execution).model_dump())
    with pytest.raises(core.GroupContentApprovalRequired):
        await sharing.gate.authorize(execution, sharing.query, result, sharing.delegate.sharing_sources.return_value)
    row, = sharing.repo.pending_for_request('local', 'request-3')
    sharing.repo.bind_native('local', 'request-3', 'native-3')
    sharing.repo.decide(row['approval_id'], namespace_id='local', config_id='config-1',
                        owner_principal='local', decision='once', now=NOW + timedelta(seconds=1))
    reopened = GroupShareRepository(sharing.repo.db_path)
    assert reopened.request_window_is_current('local', 'request-2')
    assert reopened.bind_request_window('local', 'agent-1', 'session-1', 'request-2') == bound
    assert reopened.bind_request_window('local', 'agent-1', 'session-1', 'request-4') != bound
    execution = replace(sharing.execution, request_id='request-5', execution_id='execution-5')
    result = sharing.result.model_copy(update=core.InvocationRef.from_execution(execution).model_dump())
    with pytest.raises(core.GroupContentApprovalRequired):
        await sharing.gate.authorize(execution, sharing.query, result, sharing.delegate.sharing_sources.return_value)
    row, = sharing.repo.pending_for_request('local', 'request-5')
    reopened.bind_native('local', 'request-5', 'native-5')
    reopened.decide(row['approval_id'], namespace_id='local', config_id='config-1',
                    owner_principal='local', decision='permanent', now=NOW + timedelta(seconds=2))
    grant, = reopened.list_for_owner('local', 'config-1', owner_principal='local')['grants']
    reopened.revoke(grant['grant_id'], namespace_id='local', config_id='config-1',
                    owner_principal='local', now=NOW + timedelta(seconds=3))
    assert not reopened.request_window_is_current('local', 'request-2')
    with pytest.raises(PersistentAgentError, match='authorization changed'):
        reopened.bind_request_window('local', 'agent-1', 'session-1', 'request-2')
