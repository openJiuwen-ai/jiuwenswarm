import assert from 'node:assert/strict';
import test from 'node:test';

import { buildCurrentTurnWorkflowRuns } from '../node_modules/.cache/team-workflow-adapter/teamWorkflowAdapter.mjs';

const turnStartedAt = 1_789_091_100_000;
const leader = { id: '1', member_id: 'team_leader', status: 'running', timestamp: turnStartedAt, name: 'leader' };
const researcher = { id: '2', member_id: 'researcher', status: 'running', timestamp: turnStartedAt, name: '洞察专家' };

test('projects only current-turn routed members into a develop WorkflowRun', () => {
  const runs = buildCurrentTurnWorkflowRuns({
    members: [leader, researcher],
    tasks: [
      { task_id: 'old', assignee: 'researcher', status: 'completed', timestamp: turnStartedAt - 1 },
      { task_id: 'new', assignee: 'researcher', title: '分析用户反馈', status: 'in_progress', timestamp: turnStartedAt + 1 },
    ],
    taskEvents: [],
    executionEvents: [],
    leaderMemberIds: ['team_leader'],
    currentTurnStartedAtMs: turnStartedAt,
    query: '帮我分析这批用户反馈',
    isProcessing: true,
  });

  assert.equal(runs.length, 1);
  assert.equal(runs[0].summary, '帮我分析这批用户反馈');
  assert.deepEqual(
    runs[0].phases[0].agents.map(agent => agent.name),
    ['团队主理人', '洞察专家'],
  );
  assert.equal(runs[0].phases[0].agents[1].prompt, '分析用户反馈');
  assert.equal(runs[0].phases[0].agents[1].status, 'running');
});

test('does not manufacture a workflow before a user-turn boundary exists', () => {
  assert.deepEqual(
    buildCurrentTurnWorkflowRuns({
      members: [leader, researcher],
      tasks: [],
      taskEvents: [],
      executionEvents: [],
      currentTurnStartedAtMs: null,
    }),
    [],
  );
});

test('uses final execution output as member outcome', () => {
  const runs = buildCurrentTurnWorkflowRuns({
    members: [leader, researcher],
    tasks: [],
    taskEvents: [],
    executionEvents: [{ member_id: 'researcher', kind: 'final', content: '结论已完成', timestamp: turnStartedAt + 2 }],
    leaderMemberIds: ['team_leader'],
    currentTurnStartedAtMs: turnStartedAt,
  });

  assert.equal(runs[0].phases[0].agents[1].outcome, '结论已完成');
  assert.equal(runs[0].phases[0].agents[1].status, 'completed');
});
