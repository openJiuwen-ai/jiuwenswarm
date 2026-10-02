import assert from 'node:assert/strict';
import test from 'node:test';
import {
  captureTeamConnectionPresentation,
  isRunningTeamTaskStatus,
  loadTeamConnectionPresentation,
  retainRunningTeamConnectionPresentation,
  saveTeamConnectionPresentation,
  shouldPresentTeamMemberIdle,
} from '../node_modules/.cache/team-connection-presentation/teamConnectionPresentation.mjs';

test('captures only running members in Team mode', () => {
  const members = [
    { member_id: 'runner', status: 'busy' },
    { member_id: 'idle', status: 'idle' },
  ];
  assert.equal(captureTeamConnectionPresentation('agent', members), null);
  assert.deepEqual(captureTeamConnectionPresentation('team', members), { memberIds: ['runner'] });
});

test('retains only captured items that remain running at the grace deadline', () => {
  const captured = { memberIds: ['runner', 'stopped'] };

  assert.deepEqual(
    retainRunningTeamConnectionPresentation(captured, [
      { member_id: 'runner', status: 'running' },
      { member_id: 'stopped', status: 'shutdown' },
    ]),
    { memberIds: ['runner'] },
  );
});

test('projects only captured running members', () => {
  const presentation = { memberIds: ['runner'] };

  assert.equal(shouldPresentTeamMemberIdle('runner', 'busy', presentation), true);
  assert.equal(shouldPresentTeamMemberIdle('runner', 'idle', presentation), false);
  assert.equal(shouldPresentTeamMemberIdle('other', 'busy', presentation), false);
  assert.equal(isRunningTeamTaskStatus('in_progress'), true);
  assert.equal(isRunningTeamTaskStatus('completed'), false);
});

test('keeps the member hint for the same tab after reload and clears it per session', () => {
  const values = new Map();
  globalThis.sessionStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  try {
    saveTeamConnectionPresentation('team-a', { memberIds: ['runner'] });
    assert.deepEqual(loadTeamConnectionPresentation('team-a'), { memberIds: ['runner'] });
    assert.equal(loadTeamConnectionPresentation('team-b'), null);
    saveTeamConnectionPresentation('team-a', null);
    assert.equal(loadTeamConnectionPresentation('team-a'), null);
  } finally {
    delete globalThis.sessionStorage;
  }
});
