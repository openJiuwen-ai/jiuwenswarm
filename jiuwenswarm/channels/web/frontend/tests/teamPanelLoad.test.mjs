import assert from 'node:assert/strict';
import test from 'node:test';

import {
  loadTeamPanelSources,
  teamsAfterLoad,
} from '../node_modules/.cache/team-panel-load/components/TeamPanel/teamPanelLoad.js';

test('loadTeamPanelSources returns ok when both sources resolve', async () => {
  const result = await loadTeamPanelSources(
    async () => [{ id: 's1' }],
    async () => [{ team_name: 'team-a' }],
  );

  assert.equal(result.outcome.status, 'ok');
  assert.deepEqual(result.outcome.failedSources, []);
  assert.deepEqual(result.sessionRows, [{ id: 's1' }]);
  assert.deepEqual(result.controlTeams, [{ team_name: 'team-a' }]);
});

test('a failed session.list does not discard the control-plane teams', async () => {
  const result = await loadTeamPanelSources(
    async () => {
      throw new Error('runtime scope full');
    },
    async () => [{ team_name: 'team-a' }],
  );

  assert.equal(result.outcome.status, 'partial');
  assert.deepEqual(result.outcome.failedSources, ['sessions']);
  assert.equal(result.sessionRows, null);
  assert.deepEqual(result.controlTeams, [{ team_name: 'team-a' }]);
});

test('a failed team.list does not discard the session rows', async () => {
  const result = await loadTeamPanelSources(
    async () => [{ id: 's1' }],
    async () => {
      throw new Error('control plane unavailable');
    },
  );

  assert.equal(result.outcome.status, 'partial');
  assert.deepEqual(result.outcome.failedSources, ['control']);
  assert.deepEqual(result.sessionRows, [{ id: 's1' }]);
  assert.equal(result.controlTeams, null);
});

test('both failures are reported as failed', async () => {
  const result = await loadTeamPanelSources(
    async () => {
      throw new Error('scope full');
    },
    async () => {
      throw new Error('control plane unavailable');
    },
  );

  assert.equal(result.outcome.status, 'failed');
  assert.deepEqual(result.outcome.failedSources, ['sessions', 'control']);
  assert.equal(result.sessionRows, null);
  assert.equal(result.controlTeams, null);
});

test('a pending session read does not block the control-plane read', async () => {
  let releaseSession;
  const pendingSessions = new Promise((resolve) => {
    releaseSession = resolve;
  });
  let controlLoaded = false;
  const load = loadTeamPanelSources(
    () => pendingSessions,
    async () => {
      controlLoaded = true;
      return [{ team_name: 'team-a' }];
    },
  );

  await Promise.resolve();
  assert.equal(controlLoaded, true);

  releaseSession?.([{ id: 's1' }]);
  const result = await load;
  assert.equal(result.outcome.status, 'ok');
});

test('a failed refresh keeps the previously rendered teams', () => {
  const previous = [{ name: 'team-a' }];
  assert.deepEqual(teamsAfterLoad(previous, [], 'failed'), previous);
});

test('a partial or successful refresh applies the newly loaded teams', () => {
  const previous = [{ name: 'team-a' }];
  const next = [{ name: 'team-b' }];
  assert.deepEqual(teamsAfterLoad(previous, next, 'partial'), next);
  assert.deepEqual(teamsAfterLoad(previous, next, 'ok'), next);
});
