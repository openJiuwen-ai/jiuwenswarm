"""Organization disclosures use isolated round boards, never historical work."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.agent_teams.context import get_session_id
from jiuwenswarm.agents.swarm import group_org_tasks as projection
from jiuwenswarm.agents.swarm import group_org_bootstrap as bootstrap
from tests.agents.swarm.test_group_org_bootstrap import _MemoryGroup, _install_card_store, _stale_plan, SESSION, ORG


def task(key, status='pending', assignee='researcher'):
    return SimpleNamespace(task_id=key, title=key, status=status, assignee=assignee)


@pytest.mark.asyncio
async def test_round_baselines_and_live_updates_do_not_count_expert_tasks_as_org_work(monkeypatch):
    _MemoryGroup(monkeypatch)
    store = _install_card_store(monkeypatch)
    plan = _stale_plan()
    old = task('old', 'completed')
    boards = {'team-a': [old], 'team-b': []}

    async def read(team):
        assert get_session_id() == SESSION
        return boards.get(team, [])

    reader = AsyncMock(side_effect=read)
    pool = SimpleNamespace(db=SimpleNamespace(task=SimpleNamespace(get_team_tasks=reader)))
    monkeypatch.setattr(projection, 'group_organization_teams', lambda session, org: [
        {'team_id': 'team-a'}, {'team_id': 'team-b'},
    ] if org == ORG else [])
    saved = Mock()
    monkeypatch.setattr(projection, 'update_group_organization_plan', saved)
    await projection.capture_task_baselines(SESSION, plan, ORG, pool)
    saved.assert_called_once()
    await projection.capture_task_baselines(SESSION, plan, ORG, pool)
    assert reader.await_count == 3
    child_a = SimpleNamespace(task_id='org-a', title='Business', status='IN_PROGRESS',
                              parent_task_id=bootstrap._root_task_id(plan['plan_id']), assigned_team_id='team-a')
    child_b = SimpleNamespace(task_id='org-b', title='Technology', status='IN_PROGRESS',
                              parent_task_id=child_a.parent_task_id, assigned_team_id='team-b')
    pool.list_tasks = AsyncMock(return_value=[child_a, child_b])
    boards['team-a'].append(task('analysis', 'in_progress'))
    boards['team-b'].append(task('tech', 'blocked', 'engineer'))

    async def publish():
        await bootstrap.publish_organization_card(session_id=SESSION, plan=plan, organization_id=ORG,
                                                   team_id='team-a', task_pool=pool)
        return copy.deepcopy(store.calls[-1][1])

    first = await publish()
    assert first['tasks'] == {'org-a': 'in_progress', 'org-b': 'in_progress'}
    assert first['task_details']['org-a']['expert_tasks'] == [
        dict(task_id='analysis', title='analysis', assignee='researcher', status='in_progress'),
    ]
    assert first['task_details']['org-b']['expert_tasks'][0]['status'] == 'blocked'
    boards['team-a'][1].status = 'in_review'
    assert (await publish())['task_details']['org-a']['expert_tasks'][0]['status'] == 'in_review'
    boards['team-a'][1].status = 'completed'
    assert (await publish())['task_details']['org-a']['expert_tasks'][0]['status'] == 'completed'
    # A new request reusing the same instance excludes all previous work.
    next_plan = {**plan, 'plan_id': 'next'}
    next_plan.pop('private_task_baselines')
    await projection.capture_task_baselines(SESSION, next_plan, ORG, pool)
    assert next_plan['private_task_baselines']['team-a'] == ['old', 'analysis']


@pytest.mark.asyncio
async def test_snapshot_failure_keeps_last_progress_and_never_guesses_a_baseline(monkeypatch):
    pool = SimpleNamespace(db=SimpleNamespace(task=SimpleNamespace(get_team_tasks=AsyncMock(side_effect=OSError('offline')))))
    monkeypatch.setattr(projection, 'group_organization_teams', lambda *args: [{'team_id': 'a'}])
    monkeypatch.setattr(projection, 'update_group_organization_plan', Mock())
    plan = {'plan_id': 'one'}
    await projection.capture_task_baselines('session', plan, 'org', pool)
    assert plan['private_task_baselines'] == {'a': None, 'org-summary-org': None}
    child = SimpleNamespace(task_id='child', assigned_team_id='a')
    previous = {'child': {'expert_tasks': [dict(task_id='x', title='x', status='in_progress', assignee='expert')]}}
    details = {'child': {}}
    await projection.add_expert_task_details('session', plan, pool, [child], details, previous)
    assert details['child']['expert_tasks_unavailable']
    assert details['child']['expert_tasks'] == previous['child']['expert_tasks']
    plan['private_task_baselines']['a'] = []
    await projection.add_expert_task_details('session', plan, pool, [child], details, previous)
    assert details['child']['expert_tasks_unavailable']


@pytest.mark.asyncio
async def test_team_board_appears_once_and_other_organizations_are_not_read():
    reader = AsyncMock(return_value=[task('new', 'in_progress')])
    pool = SimpleNamespace(db=SimpleNamespace(task=SimpleNamespace(get_team_tasks=reader)))
    plan = {'private_task_baselines': {'a': []}}
    rows = [SimpleNamespace(task_id=key, assigned_team_id=team) for key, team in
            [('child', 'a'), ('repair', 'a'), ('root', 'a'), ('foreign', 'b')]]
    details = {r.task_id: {} for r in rows}
    await projection.add_expert_task_details('session', plan, pool, rows, details, {}, fail_unfinished=True)
    reader.assert_awaited_once_with('a')
    assert details['child']['expert_tasks'][0]['status'] == 'interrupted'
    assert details['repair'] == details['root'] == details['foreign'] == {}


@pytest.mark.asyncio
async def test_real_task_database_is_scoped_to_session_and_retains_cleaned_tasks(tmp_path, monkeypatch):
    from openjiuwen.agent_teams.context import set_session_id, reset_session_id
    from openjiuwen.agent_teams.tools.database import TeamDatabase, DatabaseConfig

    db = TeamDatabase(DatabaseConfig(connection_string=str(tmp_path / 'team.db')))
    token = set_session_id('round-session')
    try:
        await db.initialize()
        await db.team.create_team('team', 'Team', 'leader')
        assert await db.task.create_task('old', 'team', 'Old task', '', 'completed')
        pool = SimpleNamespace(db=db)
        monkeypatch.setattr(projection, 'group_organization_teams', lambda *args: [{'team_id': 'team'}])
        monkeypatch.setattr(projection, 'update_group_organization_plan', Mock())
        plan = {'plan_id': 'one'}
        await projection.capture_task_baselines('round-session', plan, 'org', pool)
        assert await db.task.create_task('new', 'team', 'New task', '', 'pending')
        assert await db.task.update_task_status('new', 'in_progress')
        assert await db.task.update_task_status('new', 'completed')
        # The caller's ambient session must not select a different task board.
        other = set_session_id('another-session')
        try:
            details = {'chapter': {}}
            rows = [SimpleNamespace(task_id='chapter', assigned_team_id='team')]
            await projection.add_expert_task_details('round-session', plan, pool, rows, details, {})
            assert [t['task_id'] for t in details['chapter']['expert_tasks']] == ['new']
            assert details['chapter']['expert_tasks'][0]['status'] == 'completed'
            assert get_session_id() == 'another-session'
        finally:
            reset_session_id(other)
        await db.task.delete_task('new')
        retained = {'chapter': {}}
        await projection.add_expert_task_details('round-session', plan, pool, rows, retained, details)
        assert retained['chapter']['expert_tasks'] == details['chapter']['expert_tasks']
    finally:
        reset_session_id(token)
        await db.close()
