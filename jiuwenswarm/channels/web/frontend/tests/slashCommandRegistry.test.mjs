import assert from 'node:assert/strict';
import test from 'node:test';

import {
  findSlashCommand,
  togglePlanFromSlash,
} from '../node_modules/.cache/slash-command-registry/slashCommands/registry.js';

const NEW_CONVERSATION_ID = 'new';

function createContext(sessionId, inputLine) {
  const messages = [];
  const submissions = [];
  return {
    messages,
    submissions,
    context: {
      sessionId,
      mode: 'agent',
      inputLine,
      addMessage: (_sessionId, message) => messages.push(message),
      submitMessage: (content) => submissions.push(content),
    },
  };
}

test('/btw is not registered by the Web frontend', () => {
  assert.equal(findSlashCommand('btw'), undefined);
});

test('/persist is registered and delegates new-session creation to the existing submit path', async () => {
  const command = findSlashCommand('persist');
  assert.ok(command);
  assert.equal(command.requiresSession, false);

  const state = createContext(NEW_CONVERSATION_ID, '/persist 帮我跟进产品发布');
  await command.execute(state.context, '帮我跟进产品发布');

  assert.deepEqual(state.submissions, ['/persist 帮我跟进产品发布']);
  assert.deepEqual(state.messages, []);
});

test('/persist requires a task on the new-session page', async () => {
  const command = findSlashCommand('persist');
  assert.ok(command);

  const state = createContext(NEW_CONVERSATION_ID, '/persist');
  await command.execute(state.context, '');

  assert.deepEqual(state.submissions, []);
  assert.match(state.messages[0].commandOutput, /\/persist <任务>/);
});

test('/persist does not mutate an existing session', async () => {
  const command = findSlashCommand('persist');
  assert.ok(command);

  const state = createContext('existing-session', '/persist 新任务');
  await command.execute(state.context, '新任务');

  assert.deepEqual(state.submissions, []);
  assert.match(state.messages[0].commandOutput, /只能在创建新会话时开启/);
});

function createPlanAndGoalStores({ planActive = false, goal = null, goalArmed = false } = {}) {
  const calls = [];
  return {
    calls,
    planStore: {
      ensureRuntime: (sessionId) => calls.push(['ensurePlanRuntime', sessionId]),
      isActive: () => planActive,
      setActive: (sessionId, active, options) => calls.push(['setPlanActive', sessionId, active, options]),
    },
    goalStore: {
      getRuntime: () => ({ goal, armed: goalArmed }),
      setArmed: (sessionId, armed) => calls.push(['setGoalArmed', sessionId, armed]),
    },
  };
}

test('/plan closes an armed but uncommitted goal before entering plan mode', () => {
  const stores = createPlanAndGoalStores({ goalArmed: true });

  const result = togglePlanFromSlash('session-1', stores.planStore, stores.goalStore);

  assert.equal(result, 'activated');
  assert.deepEqual(stores.calls, [
    ['ensurePlanRuntime', 'session-1'],
    ['setGoalArmed', 'session-1', false],
    [
      'setPlanActive',
      'session-1',
      true,
      { explicitEntry: true, entrySource: 'slash_command' },
    ],
  ]);
});

test('/plan cannot enter plan mode while a goal is unfinished', () => {
  const stores = createPlanAndGoalStores({
    goal: { status: 'paused' },
    goalArmed: true,
  });

  const result = togglePlanFromSlash('session-1', stores.planStore, stores.goalStore);

  assert.equal(result, 'blocked_by_goal');
  assert.deepEqual(stores.calls, [['ensurePlanRuntime', 'session-1']]);
});

test('/plan cannot toggle while the session is busy (processing / awaiting ask_user)', () => {
  const openStores = createPlanAndGoalStores({ planActive: false });
  assert.equal(
    togglePlanFromSlash('session-1', openStores.planStore, openStores.goalStore, true),
    'blocked_by_busy',
  );
  assert.deepEqual(openStores.calls, [['ensurePlanRuntime', 'session-1']]);

  // 关闭方向同样被拦（ask_user 待回答时 isProcessing 已回 false，旧闸门会漏放）。
  const closeStores = createPlanAndGoalStores({ planActive: true });
  assert.equal(
    togglePlanFromSlash('session-1', closeStores.planStore, closeStores.goalStore, true),
    'blocked_by_busy',
  );
  assert.deepEqual(closeStores.calls, [['ensurePlanRuntime', 'session-1']]);
});
