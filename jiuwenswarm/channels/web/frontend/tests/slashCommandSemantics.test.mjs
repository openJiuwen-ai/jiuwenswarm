import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getWebSlashCommandsForMode,
  hasUnfinishedGoal,
  isSlashCommandDisabledByGoal,
  resolvePlanGoalInterlock,
  resolveSlashCommandDescription,
  shouldExecuteRegisteredSlashCommand,
  supportsWebSlashCommands,
} from '../node_modules/.cache/slash-command-semantics/components/ChatPanel/slashCommands/semantics.js';

test('cached command metadata switches language without changing the source description', () => {
  const command = {
    description: '旧版说明',
    description_i18n: { zh: '中文说明', en: 'English description' },
  };
  assert.equal(resolveSlashCommandDescription(command, 'en'), 'English description');
  assert.equal(resolveSlashCommandDescription(command, 'zh'), '中文说明');
  assert.equal(resolveSlashCommandDescription(command, 'en'), 'English description');
  assert.equal(command.description, '旧版说明');
});

test('command descriptions resolve regional locales before falling back to the base language', () => {
  const command = {
    description: '默认说明',
    description_i18n: { zh: '中文说明', en: 'English description', 'en-gb': 'British description' },
  };
  assert.equal(resolveSlashCommandDescription(command, 'zh-CN'), '中文说明');
  assert.equal(resolveSlashCommandDescription(command, ' EN_us '), 'English description');
  assert.equal(resolveSlashCommandDescription(command, 'en-GB'), 'British description');
});

test('old servers and missing translations fall back to the original description', () => {
  const legacyCommand = { description: 'Legacy description' };
  assert.equal(resolveSlashCommandDescription(legacyCommand, 'en'), 'Legacy description');
  assert.equal(
    resolveSlashCommandDescription({ ...legacyCommand, description_i18n: { zh: '中文说明' } }, 'en'),
    'Legacy description',
  );
  assert.equal(
    resolveSlashCommandDescription({ ...legacyCommand, description_i18n: { en: '' } }, 'en'),
    'Legacy description',
  );
  assert.equal(
    resolveSlashCommandDescription({ ...legacyCommand, description_i18n: { en: 'English' } }, 'fr'),
    'Legacy description',
  );
});

test('standalone plan command executes', () => {
  assert.equal(shouldExecuteRegisteredSlashCommand('plan', '', 'agent'), true);
  assert.equal(shouldExecuteRegisteredSlashCommand('PLAN', '   ', 'agent'), true);
});

test('plan with arguments remains an ordinary chat message', () => {
  assert.equal(shouldExecuteRegisteredSlashCommand('plan', 'hi', 'agent'), false);
  assert.equal(shouldExecuteRegisteredSlashCommand('plan', 'open', 'agent'), false);
});

test('fork executes only as a standalone command', () => {
  assert.equal(shouldExecuteRegisteredSlashCommand('fork', '', 'agent'), true);
  assert.equal(shouldExecuteRegisteredSlashCommand('FORK', '   ', 'agent'), true);
  assert.equal(shouldExecuteRegisteredSlashCommand('fork', 'custom title', 'agent'), false);
});

test('goal executes with or without control arguments', () => {
  assert.equal(shouldExecuteRegisteredSlashCommand('goal', '', 'agent'), true);
  assert.equal(shouldExecuteRegisteredSlashCommand('goal', 'pause', 'agent'), true);
  assert.equal(shouldExecuteRegisteredSlashCommand('goal', 'ship the release', 'agent'), true);
});

test('other registered slash commands keep their existing argument behavior', () => {
  assert.equal(shouldExecuteRegisteredSlashCommand('compact', '', 'agent'), true);
  assert.equal(shouldExecuteRegisteredSlashCommand('persist', '跟进发布', 'agent'), true);
});

test('team mode exposes no slash commands', () => {
  const commands = [{ name: 'fork' }, { name: 'compact' }, { name: 'plan' }, { name: 'goal' }, { name: 'persist' }];

  assert.equal(supportsWebSlashCommands('team'), false);
  assert.deepEqual(getWebSlashCommandsForMode(commands, 'team'), []);
  for (const command of commands) {
    assert.equal(shouldExecuteRegisteredSlashCommand(command.name, '', 'team'), false);
  }
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
