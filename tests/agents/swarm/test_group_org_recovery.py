"""Lost organizations are closed without touching a live/reused team."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.swarm import group_org_bootstrap as boot, group_org_recovery as recovery
from jiuwenswarm.server.runtime import group_execution as ge, group_service as gs
from tests.unit_tests.server.test_group_service import service, create


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
async def test_pool_for_passes_the_resolved_runtime_config_to_the_shared_database(monkeypatch):
    from openjiuwen.agent_teams.organization import task_pool
    from openjiuwen.agent_teams.spawn import shared_resources

    from jiuwenswarm.agents.harness.team import config_loader

    config = object()
    db = SimpleNamespace(initialize=AsyncMock())
    opened = {}

    def get_shared_db(got):
        opened['config'] = got
        return db

    class Manager:
        def __init__(self, *, db, organization_id):
            opened['db'] = db
            opened['organization_id'] = organization_id
            self.initialize = AsyncMock()

    monkeypatch.setattr(config_loader, 'resolve_team_runtime_database_config', lambda: config)
    monkeypatch.setattr(shared_resources, 'get_shared_db', get_shared_db)
    monkeypatch.setattr(task_pool, 'OrgTaskManager', Manager)
    pool = await recovery._pool_for({'organization_id': 'org-1'})
    assert opened['config'] is config
    assert opened['db'] is db
    assert opened['organization_id'] == 'org-1'
    db.initialize.assert_awaited_once()
    pool.initialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_pool_for_does_not_open_a_database_when_runtime_config_is_missing(monkeypatch):
    from openjiuwen.agent_teams.spawn import shared_resources

    from jiuwenswarm.agents.harness.team import config_loader

    def get_shared_db(_config):
        raise AssertionError('database opened')

    monkeypatch.setattr(config_loader, 'resolve_team_runtime_database_config', lambda: None)
    monkeypatch.setattr(shared_resources, 'get_shared_db', get_shared_db)
    with pytest.raises(RuntimeError, match='No verified organization database'):
        await recovery._pool_for({'organization_id': 'org-1'})
