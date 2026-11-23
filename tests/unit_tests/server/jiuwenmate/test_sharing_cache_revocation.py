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
from tests.unit_tests.server.jiuwenmate.test_group_sharing_service import delivery_job
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


@pytest.mark.parametrize("content_type", ["files", "online_docs"])
async def test_artifact_delivery_scope_and_window_are_invalidated_on_revoke(artifact_host, content_type):
    host = artifact_host
    if content_type == "online_docs":
        document = {
            "source_group_id": "source-group", "artifact_id": "doc-1", "artifact_kind": "online_doc",
            "title": "Report", "snippet": "Report", "url": "https://example.feishu.cn/docx/report",
            "created_at": NOW.isoformat(),
        }
        host.sessions.session_artifact_candidates = lambda *_: (document,)
        host.sessions.search_session_artifacts = lambda *_, **__: (document,)
    query = core.GroupMessageQuery(group_ids=("source-group",), content_types=(content_type,))
    host.repo.bind_request_window("local", "agent-1", "session-1", "request-1")
    with pytest.raises(core.GroupContentApprovalRequired):
        await host.port.search_content(host.execution, query)
    row, = host.repo.pending_for_request("local", "request-1")
    host.repo.bind_native("local", "request-1", "native")
    host.repo.decide(row["approval_id"], namespace_id="local", config_id="config-1",
                     owner_principal="local", decision="permanent", now=NOW)

    job = delivery_job()
    assert await host.gate.can_deliver(job)
    for change in ({"agent_id": "other"}, {"channel": "dingtalk"}, {"app_id": "other-app"},
                   {"target_id": "other-group"}, {"request_kind": "direct_message"}):
        assert not await host.gate.can_deliver({**job, **change})
    before = host.repo.window_generation("local", "agent-1", "session-1")
    grant, = host.repo.list_for_owner("local", "config-1", owner_principal="local")["grants"]
    host.repo.revoke(grant["grant_id"], namespace_id="local", config_id="config-1",
                     owner_principal="local", now=NOW + timedelta(seconds=1))

    assert not await host.gate.can_deliver(job)
    assert host.repo.window_generation("local", "agent-1", "session-1") != before
    assert host.repo.window_generation("local", "agent-1", "unrelated-session") == ""


async def test_unknown_import_fails_closed(artifact_host):
    guard = SessionArtifactSharing(artifact_host.gate, artifact_host.delegate)
    result = await guard.prepare(artifact_host.execution, 'read_file', {'file_path': 'imports/unknown.txt'})
    assert result is not None and not result.success


async def test_invalid_public_import_path_does_not_skip_access_checks(artifact_host, monkeypatch):
    from jiuwenswarm.server.jiuwenmate.cached_group_content import CachedGroupContent

    monkeypatch.setattr(artifact_host.gate, "public_group_imports", lambda _: ("sessions/session-1/output/report.txt",))
    cache = CachedGroupContent(artifact_host.gate, artifact_host.delegate)
    matched, result = await cache.prepare(artifact_host.execution, "imports/unverified.txt", [])
    assert matched and not result.success


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






@pytest.mark.asyncio
async def test_retired_request_cannot_read_once_import_after_target_rejoins(artifact_host):
    from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository

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
    target = row['scope']['target']
    groups = GroupChatRepository(artifact_host.repo.db_path)
    group = groups.resolve('local', target['channel'], target['id'], now=NOW)
    groups.observe_access('local', execution.agent_id, group.group_chat_id, target['app_id'],
                          observed_at=NOW, source_ref='message:first')
    groups.update_membership('local', execution.agent_id, group.group_chat_id, target['app_id'],
                             status='left', observed_at=NOW + timedelta(seconds=2), source_ref='event:left')
    groups.observe_access('local', execution.agent_id, group.group_chat_id, target['app_id'],
                          observed_at=NOW + timedelta(seconds=3), message_time=NOW + timedelta(seconds=3),
                          source_ref='message:rejoin')
    output = await guard.prepare(execution, 'read_file', args)
    assert output is not None and not output.success




def retire_target_before(host, monkeypatch, operation, event):
    """Deliver a real lifecycle event after preflight, before the repository transaction."""
    from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository

    groups = GroupChatRepository(host.repo.db_path, id_factory=lambda: 'target-group')
    target = groups.resolve('local', 'feishu', 'chat-target', now=NOW)
    groups.observe_access('local', 'agent-1', target.group_chat_id, 'app-1',
                          observed_at=NOW, source_ref='message:first')
    original = getattr(host.repo, operation)

    def retire_then_call(*args, **kwargs):
        if event == 'dissolved':
            groups.mark_dissolved('local', target.group_chat_id,
                                  observed_at=NOW + timedelta(seconds=1), source_ref='event:disband')
        else:
            groups.update_membership('local', 'agent-1', target.group_chat_id, 'app-1',
                                     status='left', observed_at=NOW + timedelta(seconds=1), source_ref='event:left')
        return original(*args, **kwargs)

    monkeypatch.setattr(host.repo, operation, retire_then_call)




@pytest.mark.asyncio
@pytest.mark.parametrize('event', ['left', 'dissolved'])
@pytest.mark.parametrize('operation', ['create_or_get_batch', 'authorize_from_grants'])
async def test_cached_import_is_blocked_when_group_retires_during_reauthorization(
    artifact_host, monkeypatch, event, operation,
):
    execution = await imported(artifact_host)
    guard = SessionArtifactSharing(artifact_host.gate, artifact_host.delegate)
    args = {'file_path': 'imports/art_original_report.txt'}
    with pytest.raises(core.GroupContentApprovalRequired):
        await guard.prepare(execution, 'read_file', args)
    retire_target_before(artifact_host, monkeypatch, operation, event)

    output = await guard.prepare(execution, 'read_file', args)

    assert output is not None and not output.success
    assert output.data['status'] == 'forbidden'
    assert 'private file' not in str(output.data)
    assert not artifact_host.repo.request_window_is_current('local', 'request-2')
    grants = artifact_host.repo.list_for_owner('local', 'config-1', owner_principal='local')['grants']
    assert all(grant['status'] == 'revoked' for grant in grants)
