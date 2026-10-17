import assert from 'node:assert/strict';
import { test } from 'node:test';

// Requires build cache populated by `npm run test:cron-mode`; fresh clones must run that first or this test fails with MODULE_NOT_FOUND.
import { isTeamCronModeValue } from '../node_modules/.cache/cron-mode/components/CronPanel/cronMode.js';

// ── M2: isTeamCronModeValue 识别全部 team 系新串，避免团队定时任务被误判单 agent ──

test('isTeamCronModeValue returns true for team and legacy team canonicals', () => {
  assert.equal(isTeamCronModeValue('team'), true);
  assert.equal(isTeamCronModeValue('team.plan'), true);
  assert.equal(isTeamCronModeValue('team.plan.normal'), true);
  assert.equal(isTeamCronModeValue('team.plan.code'), true);
  assert.equal(isTeamCronModeValue('code.team'), true);
});

test('isTeamCronModeValue returns true for new team.work.* / team.code.* canonicals', () => {
  // M2 修复核心：新三段命名 canonical（P3 引入）必须全部识别为团队
  assert.equal(isTeamCronModeValue('team.work.normal'), true);
  assert.equal(isTeamCronModeValue('team.work.plan'), true);
  assert.equal(isTeamCronModeValue('team.code.normal'), true);
  assert.equal(isTeamCronModeValue('team.code.plan'), true);
});

test('isTeamCronModeValue is case-insensitive and trims whitespace', () => {
  assert.equal(isTeamCronModeValue('TEAM.WORK.PLAN'), true);
  assert.equal(isTeamCronModeValue('  team.code.normal  '), true);
});

test('isTeamCronModeValue returns false for agent / non-team / empty', () => {
  assert.equal(isTeamCronModeValue('agent'), false);
  assert.equal(isTeamCronModeValue('agent.work.plan'), false);
  assert.equal(isTeamCronModeValue('agent.fast'), false);
  assert.equal(isTeamCronModeValue('proactive.tick'), false);
  assert.equal(isTeamCronModeValue(''), false);
  assert.equal(isTeamCronModeValue(undefined), false);
  assert.equal(isTeamCronModeValue(null), false);
});
