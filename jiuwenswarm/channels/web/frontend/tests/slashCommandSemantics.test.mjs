import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getWebSlashCommandsForMode,
  hasUnfinishedGoal,
  isSlashCommandDisabledByGoal,
  resolvePlanGoalInterlock,
  shouldExecuteRegisteredSlashCommand,
  supportsWebSlashCommands,
} from '../node_modules/.cache/slash-command-semantics/components/ChatPanel/slashCommands/semantics.js';

test('standalone plan command executes', () => {
  assert.equal(shouldExecuteRegisteredSlashCommand('plan', '', 'agent'), true);
  assert.equal(shouldExecuteRegisteredSlashCommand('PLAN', '   ', 'agent'), true);
});

test('plan with arguments remains an ordinary chat message', () => {
  assert.equal(shouldExecuteRegisteredSlashCommand('plan', 'hi', 'agent'), false);
  assert.equal(shouldExecuteRegisteredSlashCommand('plan', 'open', 'agent'), false);
});

test('other registered slash commands keep their existing argument behavior', () => {
  assert.equal(shouldExecuteRegisteredSlashCommand('compact', '', 'agent'), true);
  assert.equal(shouldExecuteRegisteredSlashCommand('persist', '跟进发布', 'agent'), true);
});

test('team mode neither exposes nor executes web slash commands', () => {
  const commands = [{ name: 'compact' }, { name: 'plan' }, { name: 'persist' }];

  assert.equal(supportsWebSlashCommands('team'), false);
  assert.deepEqual(getWebSlashCommandsForMode(commands, 'team'), []);
  assert.equal(shouldExecuteRegisteredSlashCommand('compact', '', 'team'), false);
  assert.equal(shouldExecuteRegisteredSlashCommand('plan', '', 'team'), false);
  assert.equal(shouldExecuteRegisteredSlashCommand('persist', '跟进发布', 'team'), false);
});

test('single-agent mode keeps command visibility and execution', () => {
  const commands = [{ name: 'compact' }, { name: 'persist' }];

  assert.equal(supportsWebSlashCommands('agent'), true);
  assert.equal(getWebSlashCommandsForMode(commands, 'agent'), commands);
  assert.equal(shouldExecuteRegisteredSlashCommand('compact', '', 'agent'), true);
  assert.equal(shouldExecuteRegisteredSlashCommand('persist', '任务', 'agent'), true);
});

test('plan entry is blocked while a real goal is unfinished', () => {
  for (const status of ['active', 'paused', 'blocked']) {
    const goal = { status };
    assert.equal(hasUnfinishedGoal(goal), true);
    assert.equal(resolvePlanGoalInterlock(goal, false), 'block');
    assert.equal(resolvePlanGoalInterlock(goal, true), 'block');
  }
});

test('plan entry clears only an uncommitted goal toggle', () => {
  assert.equal(resolvePlanGoalInterlock(null, true), 'clear_goal_armed');
  assert.equal(resolvePlanGoalInterlock({ status: 'completed' }, true), 'clear_goal_armed');
  assert.equal(resolvePlanGoalInterlock(null, false), 'allow');
  assert.equal(resolvePlanGoalInterlock({ status: 'completed' }, false), 'allow');
});

test('only /plan is disabled in the picker while a goal is unfinished', () => {
  assert.equal(isSlashCommandDisabledByGoal('plan', true), true);
  assert.equal(isSlashCommandDisabledByGoal('PLAN', true), true);
  assert.equal(isSlashCommandDisabledByGoal('compact', true), false);
  assert.equal(isSlashCommandDisabledByGoal('plan', false), false);
});
