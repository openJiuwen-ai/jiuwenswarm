"""Lost organizations are closed without touching a live/reused team."""

import asyncio
import copy
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.agents.swarm import group_org_bootstrap as boot, group_org_recovery as recovery
from jiuwenswarm.server.runtime import group_execution as ge, group_service as gs
from tests.unit_tests.server.test_group_service import service, create


_original_publish = ge.GroupExecutions.publish


@pytest.fixture
async def case(service, monkeypatch):
    group = await create(service, members=[dict(kind='agent_group', id='one', member_name='owner'),
                                          dict(kind='agent_group', id='two', member_name='other')])
    sid = group['group_id']
    plan = boot.record_organization_plan(sid, mentions=['owner', 'other'], body='Task', plan_id='old')
    gs.update_group_organization_plan(sid, 'old', status='running')
    monkeypatch.setattr(ge.GroupExecutions, 'publish', AsyncMock())
    store = ge.GroupExecutions(sid)
    args = dict(title='Task', task='Task', organization_id=plan['organization_id'], owner_member='owner',
                team_id='reused', root_task_id='root', status='running', phase='executing',
                organization_plan_id='old', organization_run_id=plan['run_id'],
                tasks={'finished': 'completed', 'working': 'in_progress', 'review': 'in_review'},
                task_details={'working': {'status': 'in_progress', 'expert_tasks': [
                    {'task_id': 'a', 'status': 'completed'}, {'task_id': 'b', 'status': 'in_progress'}]}})
    await store.upsert_organization_card('card', **args)
    gs.upsert_group_organization_team(sid, plan['organization_id'], dict(member_name='owner', team_id='reused'))
    monkeypatch.setattr(recovery, '_enabled', True)
    monkeypatch.setattr(recovery.lifecycle, 'runtime_owner_alive', lambda _: False)
    pool = SimpleNamespace(get_summary_team=AsyncMock(return_value=None), dissolve_organization=AsyncMock())
    runtime = SimpleNamespace(pool=SimpleNamespace(get=AsyncMock(return_value=None)))
    monkeypatch.setattr(boot, '_organization_runtime', lambda: (runtime, None, None))
    monkeypatch.setattr(recovery, '_pool_for', AsyncMock(return_value=pool))
    yield SimpleNamespace(sid=sid, plan=plan, args=args, store=store, pool=pool, runtime=runtime)
    boot._member_turns.clear()


@pytest.mark.asyncio
async def test_lost_run_closes_nested_rows_keeps_completed_and_rejects_late_writes(case):
    c = case
    await recovery.reconcile_group(c.sid)
    plan = boot._plan_record(c.sid, 'old')
    assert plan['status'] == 'failed' and plan['runtime_interrupted']
    assert plan['cleanup_status'] == 'done'
    card = c.store.get('card')
    assert card['status'] == 'interrupted'
    assert card['tasks'] == {'finished': 'completed', 'working': 'interrupted', 'review': 'interrupted'}
    assert [v['status'] for v in card['task_details']['working']['expert_tasks']] == ['completed', 'interrupted']
    assert not gs.group_organization_teams(c.sid)
    assert (await c.store.upsert_organization_card('card', **c.args)) == card
    gs.update_group_organization_plan(c.sid, 'old', status='running')
    assert boot._plan_record(c.sid, 'old')['status'] == 'failed'
    await recovery.reconcile_group(c.sid)
    assert c.store.get('card') == card
    c.pool.dissolve_organization.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('protection', ['alive', 'unknown', 'owner', 'queue', 'member', 'bound', 'summary', 'disabled'])
async def test_live_or_unverifiable_execution_is_never_interrupted(case, monkeypatch, protection):
    c = case
    before = c.store.read()
    task = asyncio.get_running_loop().create_future()
    mapping, key = None, None
    if protection == 'alive':
        monkeypatch.setattr(recovery.lifecycle, 'runtime_owner_alive', lambda _: True)
    elif protection == 'unknown':
        def unavailable(_):
            raise lifecycle_error
        lifecycle_error = recovery.lifecycle.LifecycleError('UNKNOWN', 'permission denied')
        monkeypatch.setattr(recovery.lifecycle, 'runtime_owner_alive', unavailable)
    elif protection in {'owner', 'queue', 'member'}:
        mapping = {'owner': boot._owner_runs, 'queue': boot._queued_plans, 'member': boot._member_runs}[protection]
        key = (c.sid, 'old') if protection == 'owner' else (c.sid, 'old', 'owner')
        mapping[key] = task
    elif protection == 'bound':
        c.runtime.pool.get.return_value = SimpleNamespace(current_session_id=c.sid,
            agent=SimpleNamespace(team_backend=SimpleNamespace(org_task_manager=SimpleNamespace(
                organization_id=c.plan['organization_id']))))
    elif protection == 'summary':
        c.pool.get_summary_team.return_value = SimpleNamespace(summary_team_id='summary')
        c.runtime.pool.get.side_effect = lambda name: SimpleNamespace(current_session_id=c.sid,
            agent=SimpleNamespace(team_backend=None)) if name == 'summary' else None
    else:
        monkeypatch.setattr(recovery, '_enabled', False)
    try:
        await recovery.reconcile_group(c.sid)
        assert c.store.read() == before
        c.pool.dissolve_organization.assert_not_awaited()
    finally:
        if mapping is not None:
            mapping.pop(key)
        task.cancel()


@pytest.mark.asyncio
async def test_reused_team_and_other_organization_are_untouched(case):
    c = case
    gs.add_group_organization_plan(c.sid, dict(plan_id='new', organization_id='new-org', status='running'))
    gs.upsert_group_organization_team(c.sid, 'new-org', dict(member_name='owner', team_id='reused'))
    backend = SimpleNamespace(org_task_manager=SimpleNamespace(organization_id='new-org'))
    c.runtime.pool.get.return_value = SimpleNamespace(current_session_id=c.sid, agent=SimpleNamespace(team_backend=backend))
    await recovery.reconcile_group(c.sid)
    assert backend.org_task_manager.organization_id == 'new-org'
    assert gs.group_organization_teams(c.sid) == [dict(member_name='owner', team_id='reused', organization_id='new-org')]
    assert boot._plan_record(c.sid, 'new')['status'] == 'running'


@pytest.mark.asyncio
async def test_partial_cleanup_is_durable_and_retryable(case):
    c = case
    c.pool.dissolve_organization.side_effect = [RuntimeError('database unavailable'), None]
    await recovery.reconcile_group(c.sid)
    assert c.store.get('card')['status'] == 'interrupted'
    assert boot._plan_record(c.sid, 'old')['cleanup_status'] == 'pending'
    assert gs.group_organization_teams(c.sid)
    await recovery.reconcile_group(c.sid)
    assert boot._plan_record(c.sid, 'old')['cleanup_status'] == 'done'
    assert not gs.group_organization_teams(c.sid)


@pytest.mark.asyncio
async def test_generation_and_legacy_owner_fallback(case):
    c = case
    before = c.store.get('card')
    await c.store.upsert_organization_card('card', **{**c.args, 'organization_run_id': 'obsolete', 'status': 'completed'})
    assert c.store.get('card') == before
    state = gs.read_group(c.sid)
    state['organization_plans'][0].pop('run_owner')
    state['organization_plans'][0].pop('run_id')
    gs._save(state)
    await recovery.reconcile_group(c.sid)
    assert c.store.get('card')['status'] == 'interrupted'


@pytest.mark.asyncio
async def test_missing_owner_and_corrupt_runtime_lookup_do_not_authorize_cleanup(case):
    c = case
    c.runtime.pool.get.side_effect = RuntimeError('runtime unavailable')
    await recovery.reconcile_group(c.sid)
    assert c.store.get('card')['status'] == 'running'
    state = gs.read_group(c.sid)
    state['organization_plans'][0].pop('run_owner')
    gs._save(state)
    records = c.store.read()
    records['card'].pop('owner')
    c.store.save(records)
    await recovery.reconcile_group(c.sid)
    c.pool.dissolve_organization.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_core_cleanup_deletes_only_target_organization(case, tmp_path, monkeypatch):
    from openjiuwen.agent_teams.tools.database import TeamDatabase
    from openjiuwen.agent_teams.tools.database.config import DatabaseConfig
    from openjiuwen.agent_teams.organization.task_pool import OrgTaskManager

    db = TeamDatabase(DatabaseConfig(connection_string=str(tmp_path / 'team.db')))
    await db.initialize()
    old = OrgTaskManager(db=db, organization_id=case.plan['organization_id'])
    new = OrgTaskManager(db=db, organization_id='other-org')
    try:
        await old.ensure_organization()
        await new.ensure_organization()
        monkeypatch.setattr(recovery, '_pool_for', AsyncMock(return_value=old))
        await recovery.reconcile_group(case.sid)
        assert await old.get_organization() is None
        assert await new.get_organization() is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_terminal_run_can_retry_cleanup_in_same_worker(case, monkeypatch):
    gs.update_group_organization_plan(case.sid, 'old', status='failed')
    monkeypatch.setattr(recovery.lifecycle, 'runtime_owner_alive', lambda _: True)
    await recovery.reconcile_group(case.sid)
    assert boot._plan_record(case.sid, 'old')['cleanup_status'] == 'done'
    assert case.store.get('card')['tasks']['working'] == 'failed'


@pytest.mark.asyncio
async def test_worker_scan_runs_on_start_and_disables_on_shutdown(case, monkeypatch):
    from jiuwenswarm.common import utils

    monkeypatch.setattr(utils, 'get_agent_sessions_dir', gs.get_agent_sessions_dir)
    monkeypatch.setattr(recovery, '_enabled', False)
    stop = asyncio.Event()
    scan = asyncio.create_task(recovery.recovery_loop(stop))
    try:
        for _ in range(100):
            if boot._plan_record(case.sid, 'old').get('cleanup_status') == 'done':
                break
            await asyncio.sleep(0.01)
        assert boot._plan_record(case.sid, 'old')['cleanup_status'] == 'done'
    finally:
        stop.set()
        await asyncio.wait_for(scan, 2)
    assert not recovery._enabled


@pytest.mark.asyncio
async def test_slow_glob_keeps_business_loop_responsive(monkeypatch):
    from jiuwenswarm.common import utils

    entered, release = threading.Event(), threading.Event()
    stop = asyncio.Event()
    loop_thread = threading.get_ident()
    calls = []

    class SlowSessions:
        def glob(self, pattern):
            # Deliberately block iteration, not creation of the generator.
            calls.append(threading.get_ident())
            entered.set()
            assert release.wait(3)
            yield SimpleNamespace(parent=SimpleNamespace(name='unused'))

    monkeypatch.setattr(utils, 'get_agent_sessions_dir', lambda: SlowSessions())
    reconcile = AsyncMock()
    monkeypatch.setattr(recovery, 'reconcile_group', reconcile)
    scan = asyncio.create_task(recovery.recovery_loop(stop))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # A different request can run and be cancelled while stat is blocked.
        request = asyncio.create_task(asyncio.sleep(100))
        await asyncio.sleep(0)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert len(calls) == 1 and calls[0] != loop_thread
        stop.set()
        release.set()
        await asyncio.wait_for(scan, 2)
        reconcile.assert_not_awaited()
    finally:
        stop.set()
        release.set()
        await asyncio.wait_for(scan, 4)
    assert not recovery._enabled


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['group', 'plans', 'records', 'save', 'update', 'binding', 'owner', 'plan'])
async def test_recovery_file_operations_do_not_block_business_loop(case, monkeypatch, target):
    targets = {
        'group': (gs, 'read_group'),
        'plans': (gs, 'group_organization_plans'),
        'records': (ge.GroupExecutions, 'read'),
        'save': (ge.GroupExecutions, 'save'),
        'update': (gs, 'update_group_organization_plan'),
        'binding': (gs, 'clear_group_organization_binding'),
        'owner': (recovery.lifecycle, 'runtime_owner_alive'),
        'plan': (boot, '_plan_record'),
    }
    obj, name = targets[target]
    original = getattr(obj, name)
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    threads = []

    def slow(*args, **kwargs):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(obj, name, slow)
    job = asyncio.create_task(recovery.reconcile_group(case.sid))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.sleep(0)
        assert not job.done()
        assert all(thread != loop_thread for thread in threads)
    finally:
        release.set()
        await asyncio.wait_for(job, 5)
    assert case.pool.dissolve_organization.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_recovery_write_keeps_group_locked_until_finished(case, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = ge.GroupExecutions.save
    written = []

    def slow_save(store, records):
        entered.set()
        assert release.wait(3)
        original(store, records)
        written.append(True)

    monkeypatch.setattr(ge.GroupExecutions, 'save', slow_save)
    recovered = asyncio.create_task(recovery.reconcile_group(case.sid))
    acquired = asyncio.Event()

    async def next_writer():
        async with gs.group_lock(case.sid):
            assert written
            acquired.set()

    writer = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        recovered.cancel()
        await asyncio.sleep(0)
        recovered.cancel()
        writer = asyncio.create_task(next_writer())
        await asyncio.sleep(0)
        assert not recovered.done()
        assert not acquired.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(recovered, 3)
        await asyncio.wait_for(writer, 3)
        assert acquired.is_set()
        case.pool.dissolve_organization.assert_not_awaited()
        assert boot._plan_record(case.sid, 'old')['cleanup_status'] == 'pending'
        await recovery.reconcile_group(case.sid)
        assert boot._plan_record(case.sid, 'old')['cleanup_status'] == 'done'
    finally:
        release.set()
        if not recovered.done():
            recovered.cancel()
            with pytest.raises(asyncio.CancelledError):
                await recovered
        if writer is not None and not writer.done():
            await writer


@pytest.mark.asyncio
async def test_scan_failure_retries_without_killing_recovery(case, monkeypatch):
    stop = asyncio.Event()
    attempts = []

    def scan():
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError('slow or temporarily unavailable storage')
        return [case.sid]

    original_wait = asyncio.wait_for

    async def fast_retry(awaitable, timeout):
        return await original_wait(awaitable, 0.001 if timeout == 30 else timeout)

    async def recovered(session_id):
        assert session_id == case.sid
        stop.set()

    monkeypatch.setattr(recovery, '_scan_sessions', scan)
    monkeypatch.setattr(recovery.asyncio, 'wait_for', fast_retry)
    monkeypatch.setattr(recovery, 'reconcile_group', recovered)
    await original_wait(recovery.recovery_loop(stop), 3)
    assert len(attempts) == 2
    assert not recovery._enabled


@pytest.mark.asyncio
@pytest.mark.parametrize('live', [False, True])
async def test_recovery_releases_direct_reply_without_reading_files_on_loop(case, monkeypatch, live):
    job = {'member': 'owner', 'started': False}
    monkeypatch.setattr(boot, '_deferred_direct', {(case.sid, 'direct'): job})
    schedule = Mock()
    monkeypatch.setattr(boot, '_schedule_direct_member', schedule)
    gs.add_group_organization_plan(case.sid, dict(
        plan_id='next', organization_id='next-org', status='running',
        members=['owner'], created_at=boot._now_ms() if live else 1,
        run_owner={'pid': 111, 'started_at': 1},
    ))
    original = gs.read_group
    loop_thread = threading.get_ident()
    owner = {'pid': 111, 'started_at': 1}
    monkeypatch.setattr(recovery.lifecycle, 'current_runtime_owner', lambda: owner)
    monkeypatch.setattr(recovery.lifecycle, 'runtime_owner_alive', lambda value: value == owner)

    def read(group_id):
        assert threading.get_ident() != loop_thread
        return original(group_id)

    monkeypatch.setattr(gs, 'read_group', read)
    await recovery.reconcile_group(case.sid)
    if live:
        schedule.assert_not_called()
        assert not job['started']
    else:
        schedule.assert_called_once_with(case.sid, job)
        assert job['started']
        # Read persisted state in a thread, just like recovery.
        assert (await asyncio.to_thread(boot._plan_record, case.sid, 'next'))['status'] == 'expired'


@pytest.mark.asyncio
async def test_recovery_notification_reads_group_outside_business_loop(case, monkeypatch):
    from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport

    record = case.store.get('card')
    original = ge.read_group
    loop_thread = threading.get_ident()
    reads = []

    def read(session_id):
        assert threading.get_ident() != loop_thread
        reads.append(session_id)
        return original(session_id)

    push = AsyncMock()
    monkeypatch.setattr(ge, 'read_group', read)
    monkeypatch.setattr(WebSocketGatewayPushTransport, 'send_push', push)
    # The fixture suppresses publication elsewhere; exercise the real method here.
    await _original_publish(case.store, record, 'updated')
    assert reads == [case.sid]
    push.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('protection', ['other_alive', 'owner_unknown', 'member', 'missing_owner', 'changed'])
async def test_recovery_never_expires_a_possibly_live_plan(case, monkeypatch, protection):
    owner = {'pid': 111, 'started_at': 1}
    own = {'pid': 222, 'started_at': 1}
    gs.add_group_organization_plan(case.sid, dict(
        plan_id='next', organization_id='next-org', status='running',
        members=['owner'], created_at=1, run_owner=None if protection == 'missing_owner' else owner,
    ))
    job = {'member': 'owner', 'started': False}
    monkeypatch.setattr(boot, '_deferred_direct', {(case.sid, 'direct'): job})
    schedule = Mock()
    monkeypatch.setattr(boot, '_schedule_direct_member', schedule)
    monkeypatch.setattr(recovery.lifecycle, 'current_runtime_owner', lambda: own)
    member = None
    if protection == 'other_alive':
        monkeypatch.setattr(recovery.lifecycle, 'runtime_owner_alive', lambda value: value == owner)
    elif protection == 'owner_unknown':
        def check(value):
            if value == owner:
                raise RuntimeError('owner unavailable')
            return False
        monkeypatch.setattr(recovery.lifecycle, 'runtime_owner_alive', check)
    elif protection == 'member':
        member = asyncio.get_running_loop().create_future()
        boot._member_runs[(case.sid, 'next', 'owner')] = member
    elif protection == 'changed':
        original = boot._plan_record
        def changed(session_id, plan_id):
            plan = original(session_id, plan_id)
            if plan_id == 'next':
                return {**plan, 'run_owner': own}
            return plan
        monkeypatch.setattr(boot, '_plan_record', changed)
    try:
        await recovery.reconcile_group(case.sid)
        assert gs.group_organization_plans(case.sid)[1]['status'] == 'running'
        assert not job['started']
        schedule.assert_not_called()
    finally:
        if member is not None:
            boot._member_runs.pop((case.sid, 'next', 'owner'), None)
            member.cancel()


@pytest.mark.asyncio
async def test_stale_plan_owned_by_this_worker_can_expire(case, monkeypatch):
    owner = {'pid': 111, 'started_at': 1}
    gs.add_group_organization_plan(case.sid, dict(
        plan_id='next', organization_id='next-org', status='running',
        members=['owner'], created_at=1, run_owner=owner,
    ))
    job = {'member': 'owner', 'started': False}
    monkeypatch.setattr(boot, '_deferred_direct', {(case.sid, 'direct'): job})
    schedule = Mock()
    monkeypatch.setattr(boot, '_schedule_direct_member', schedule)
    monkeypatch.setattr(recovery.lifecycle, 'current_runtime_owner', lambda: owner)
    monkeypatch.setattr(recovery.lifecycle, 'runtime_owner_alive', lambda value: value == owner)
    await recovery.reconcile_group(case.sid)
    assert boot._plan_record(case.sid, 'next')['status'] == 'expired'
    schedule.assert_called_once_with(case.sid, job)
