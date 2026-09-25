import assert from 'node:assert/strict';
import test from 'node:test';

import {
  findSlashCommand,
  parseGoalSlashArgs,
  prepareGoalSetFromSlash,
  togglePlanFromSlash,
} from '../node_modules/.cache/slash-command-registry/slashCommands/registry.js';

const NEW_CONVERSATION_ID = 'new';

function createContext(sessionId, inputLine) {
  const messages = [];
  const submissions = [];
  const forkedConversations = [];
  const goalActions = [];
  const goalOverwriteConfirmations = [];
  return {
    messages,
    submissions,
    forkedConversations,
    goalActions,
    goalOverwriteConfirmations,
    context: {
      sessionId,
      mode: 'agent',
      inputLine,
      addMessage: (_sessionId, message) => messages.push(message),
      submitMessage: (content) => submissions.push(content),
      forkConversation: async (sourceSessionId) => forkedConversations.push(sourceSessionId),
      runGoalAction: async (goalSessionId, action, objective) => {
        goalActions.push([goalSessionId, action, objective]);
        return null;
      },
      confirmGoalOverwrite: async (currentObjective, requestedObjective) => {
        goalOverwriteConfirmations.push([currentObjective, requestedObjective]);
        return true;
      },
    },
  };
}

test('/btw is not registered by the Web frontend', () => {
  assert.equal(findSlashCommand('btw'), undefined);
});

test('/new and /side are absent from the Web command registry', () => {
  assert.equal(findSlashCommand('new'), undefined);
  assert.equal(findSlashCommand('side'), undefined);
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

test('/goal argument parsing matches the TUI command grammar', () => {
  assert.deepEqual(parseGoalSlashArgs(''), { action: 'get' });
  assert.deepEqual(parseGoalSlashArgs(' PAUSE '), { action: 'pause' });
  assert.deepEqual(parseGoalSlashArgs('resume'), { action: 'resume' });
  assert.deepEqual(parseGoalSlashArgs('CLEAR'), { action: 'clear' });
  assert.deepEqual(parseGoalSlashArgs('set ship the release'), {
    action: 'set',
    objective: 'ship the release',
  });
  assert.deepEqual(parseGoalSlashArgs('set'), { action: 'set', objective: '' });
  assert.deepEqual(parseGoalSlashArgs('get'), { action: 'set', objective: 'get' });
  assert.deepEqual(parseGoalSlashArgs('stop'), { action: 'set', objective: 'stop' });
});

test('/goal without arguments queries and displays the current goal', async () => {
  const command = findSlashCommand('goal');
  assert.ok(command);
  assert.equal(command.requiresSession, false);

  const state = createContext('existing-session', '/goal');
  state.context.runGoalAction = async (sessionId, action, objective) => {
    state.goalActions.push([sessionId, action, objective]);
    return { objective: '发布产品', status: 'active' };
  };
  await command.execute(state.context, '');

  assert.deepEqual(state.goalActions, [['existing-session', 'get', undefined]]);
  assert.equal(state.messages[0].commandName, 'goal');
  assert.match(state.messages[0].commandOutput, /当前目标（进行中）：发布产品/);
});

test('/goal reports when the current session has no goal', async () => {
  const command = findSlashCommand('goal');
  assert.ok(command);
  const state = createContext('existing-session', '/goal');

  await command.execute(state.context, '');

  assert.deepEqual(state.goalActions, [['existing-session', 'get', undefined]]);
  assert.match(state.messages[0].commandOutput, /没有持续目标/);
});

test('/goal supports both explicit and shorthand goal setting', async () => {
  const command = findSlashCommand('goal');
  assert.ok(command);

  const explicit = createContext('session-1', '/goal set ship the release');
  await command.execute(explicit.context, 'set ship the release');
  assert.deepEqual(explicit.goalActions, [['session-1', 'set', 'ship the release']]);

  const shorthand = createContext('session-2', '/goal ship the release');
  await command.execute(shorthand.context, 'ship the release');
  assert.deepEqual(shorthand.goalActions, [['session-2', 'set', 'ship the release']]);
});

test('/goal set requires a non-empty objective', async () => {
  const command = findSlashCommand('goal');
  assert.ok(command);
  const state = createContext('existing-session', '/goal set');

  await command.execute(state.context, 'set');

  assert.deepEqual(state.goalActions, []);
  assert.match(state.messages[0].commandOutput, /\/goal \[set <目标>/);
});

test('/goal pause, resume, and clear delegate to the existing Goal actions', async () => {
  const command = findSlashCommand('goal');
  assert.ok(command);
  const state = createContext('existing-session', '/goal pause');

  await command.execute(state.context, 'pause');
  await command.execute({ ...state.context, inputLine: '/goal resume' }, 'resume');
  await command.execute({ ...state.context, inputLine: '/goal clear' }, 'clear');

  assert.deepEqual(state.goalActions, [
    ['existing-session', 'pause', undefined],
    ['existing-session', 'resume', undefined],
    ['existing-session', 'clear', undefined],
  ]);
});

test('/goal control actions require a real session', async () => {
  const command = findSlashCommand('goal');
  assert.ok(command);
  const state = createContext(NEW_CONVERSATION_ID, '/goal pause');

  await command.execute(state.context, 'pause');

  assert.deepEqual(state.goalActions, []);
  assert.match(state.messages[0].commandOutput, /请先开始一个对话/);
});

function createGoalSetStores({ goal = null, planActive = false, planPending = false } = {}) {
  const calls = [];
  return {
    calls,
    goalStore: {
      getRuntime: () => ({ goal, armed: true }),
      setArmed: (sessionId, armed) => calls.push(['setGoalArmed', sessionId, armed]),
    },
    planStore: {
      isActive: () => planActive,
      hasPendingExplicitEntry: () => planPending,
      setActive: (sessionId, active) => calls.push(['setPlanActive', sessionId, active]),
    },
  };
}

test('/goal asks for confirmation before replacing an unfinished goal', () => {
  const stores = createGoalSetStores({ goal: { objective: 'old goal', status: 'paused' } });

  assert.equal(
    prepareGoalSetFromSlash('session-1', false, stores.planStore, stores.goalStore),
    'confirm_overwrite',
  );
  assert.deepEqual(stores.calls, []);
  assert.equal(
    prepareGoalSetFromSlash('session-1', true, stores.planStore, stores.goalStore),
    'ready',
  );
  assert.deepEqual(stores.calls, [['setGoalArmed', 'session-1', false]]);
});

test('/goal cannot replace a committed plan but clears an uncommitted plan toggle', () => {
  const committed = createGoalSetStores({ planActive: true });
  assert.equal(
    prepareGoalSetFromSlash('session-1', false, committed.planStore, committed.goalStore),
    'blocked_by_plan',
  );
  assert.deepEqual(committed.calls, []);

  const pending = createGoalSetStores({ planActive: true, planPending: true });
  assert.equal(
    prepareGoalSetFromSlash('session-1', false, pending.planStore, pending.goalStore),
    'ready',
  );
  assert.deepEqual(pending.calls, [
    ['setPlanActive', 'session-1', false],
    ['setGoalArmed', 'session-1', false],
  ]);
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
