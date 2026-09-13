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
  const newConversations = [];
  const forkedConversations = [];
  const sideConversations = [];
  return {
    messages,
    submissions,
    newConversations,
    forkedConversations,
    sideConversations,
    context: {
      sessionId,
      mode: 'agent',
      inputLine,
      addMessage: (_sessionId, message) => messages.push(message),
      submitMessage: (content) => submissions.push(content),
      startNewConversation: () => newConversations.push(true),
      forkConversation: async (sourceSessionId) => forkedConversations.push(sourceSessionId),
      startSideConversation: async (sourceSessionId, prompt) => sideConversations.push([sourceSessionId, prompt]),
    },
  };
}

test('/btw is not registered by the Web frontend', () => {
  assert.equal(findSlashCommand('btw'), undefined);
});

test('/new is registered and delegates to the existing new-conversation path', async () => {
  const command = findSlashCommand('new');
  assert.ok(command);
  assert.equal(command.requiresSession, false);

  const state = createContext('existing-session', '/new');
  await command.execute(state.context, '');

  assert.deepEqual(state.newConversations, [true]);
  assert.deepEqual(state.submissions, []);
  assert.deepEqual(state.messages, []);
});

test('/fork is registered and delegates the current session to the App fork path', async () => {
  const command = findSlashCommand('fork');
  assert.ok(command);
  assert.notEqual(command.requiresSession, false);

  const state = createContext('existing-session', '/fork');
  await command.execute(state.context, '');

  assert.deepEqual(state.forkedConversations, ['existing-session']);
  assert.deepEqual(state.submissions, []);
  assert.deepEqual(state.messages, []);
});

test('/fork reports a command result when the App fork path fails', async () => {
  const command = findSlashCommand('fork');
  assert.ok(command);
  const state = createContext('existing-session', '/fork');
  state.context.forkConversation = async () => {
    throw new Error('fork failed');
  };

  await command.execute(state.context, '');

  assert.equal(state.messages.length, 1);
  assert.equal(state.messages[0].commandName, 'fork');
  assert.match(state.messages[0].commandOutput, /分叉会话失败/);
});

test('/side starts an ephemeral side conversation and forwards optional text', async () => {
  const command = findSlashCommand('side');
  assert.ok(command);
  assert.notEqual(command.requiresSession, false);

  const state = createContext('existing-session', '/side inspect the cache path');
  await command.execute(state.context, 'inspect the cache path');

  assert.deepEqual(state.sideConversations, [['existing-session', 'inspect the cache path']]);
  assert.deepEqual(state.messages, []);
});

test('/side reports a command result when side conversation creation fails', async () => {
  const command = findSlashCommand('side');
  assert.ok(command);
  const state = createContext('existing-session', '/side');
  state.context.startSideConversation = async () => {
    throw new Error('side failed');
  };

  await command.execute(state.context, '');

  assert.equal(state.messages.length, 1);
  assert.equal(state.messages[0].commandName, 'side');
  assert.match(state.messages[0].commandOutput, /侧会话失败/);
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
