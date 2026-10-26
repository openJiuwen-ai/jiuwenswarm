import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DEFAULT_SNOOZE_HOURS,
  dismissReminder,
  getReminders,
  ingestStageDueEvent,
  resetReminders,
  restoreRemindersFromInbox,
  runLongHorizonStageAction,
} from '../node_modules/.cache/long-horizon-reminder/index.js';

test.beforeEach(() => {
  resetReminders();
});

test('long_horizon.stage_due renders one reminder', () => {
  ingestStageDueEvent({
    event_type: 'long_horizon.stage_due',
    task_id: 'lhc_1',
    stage_id: 'lhs_1',
    exec_session_id: 'longhorizon_lhc_1',
    title: '年度发布',
    stage_title: '准备材料',
    due_at: '2026-09-01T01:00:00+00:00',
    hint: '整理清单',
  });

  const reminders = getReminders();
  assert.equal(reminders.length, 1);
  assert.equal(reminders[0].id, 'lhc_1:lhs_1');
  assert.equal(reminders[0].taskId, 'lhc_1');
  assert.equal(reminders[0].stageId, 'lhs_1');
  assert.equal(reminders[0].title, '年度发布');
  assert.equal(reminders[0].stageTitle, '准备材料');
});

test('duplicate stage_due keeps a single reminder', () => {
  const payload = {
    event_type: 'long_horizon.stage_due',
    task_id: 'lhc_1',
    stage_id: 'lhs_1',
    title: '年度发布',
    stage_title: '准备材料',
  };
  ingestStageDueEvent(payload);
  ingestStageDueEvent(payload);
  assert.equal(getReminders().length, 1);
});

test('start sends stage_action=start and navigates to exec session', async () => {
  ingestStageDueEvent({
    event_type: 'long_horizon.stage_due',
    task_id: 'lhc_1',
    stage_id: 'lhs_1',
    exec_session_id: 'longhorizon_lhc_1',
    title: '年度发布',
    stage_title: '准备材料',
  });

  const calls = [];
  const opens = [];
  await runLongHorizonStageAction(
    {
      taskId: 'lhc_1',
      stageId: 'lhs_1',
      action: 'start',
      execSessionId: 'longhorizon_lhc_1',
    },
    {
      request: async (method, params) => {
        calls.push({ method, params });
        return {
          exec_session_id: 'longhorizon_lhc_1',
          kick_query: '请根据长程任务协助用户推进本阶段。',
        };
      },
      openSession: (detail) => {
        opens.push(detail);
      },
    }
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, 'long_horizon_stage_action');
  assert.equal(calls[0].params.stage_action, 'start');
  assert.equal(calls[0].params.task_id, 'lhc_1');
  assert.equal(calls[0].params.stage_id, 'lhs_1');
  assert.equal(opens.length, 1);
  assert.equal(opens[0].sessionId, 'longhorizon_lhc_1');
  assert.equal(opens[0].kickQuery, '请根据长程任务协助用户推进本阶段。');
  assert.equal(getReminders().length, 0);
});

test('snooze sends two hours', async () => {
  ingestStageDueEvent({
    event_type: 'long_horizon.stage_due',
    task_id: 'lhc_1',
    stage_id: 'lhs_1',
    title: '年度发布',
    stage_title: '准备材料',
  });

  const calls = [];
  await runLongHorizonStageAction(
    {
      taskId: 'lhc_1',
      stageId: 'lhs_1',
      action: 'snooze',
    },
    {
      request: async (method, params) => {
        calls.push({ method, params });
        return { success: true };
      },
    }
  );

  assert.equal(DEFAULT_SNOOZE_HOURS, 2);
  assert.equal(calls[0].params.stage_action, 'snooze');
  assert.equal(calls[0].params.snooze_hours, 2);
  assert.equal(getReminders().length, 0);
});

test('skip sends stage_action=skip', async () => {
  ingestStageDueEvent({
    event_type: 'long_horizon.stage_due',
    task_id: 'lhc_1',
    stage_id: 'lhs_1',
    title: '年度发布',
    stage_title: '准备材料',
  });

  const calls = [];
  await runLongHorizonStageAction(
    {
      taskId: 'lhc_1',
      stageId: 'lhs_1',
      action: 'skip',
    },
    {
      request: async (method, params) => {
        calls.push({ method, params });
        return { success: true };
      },
    }
  );

  assert.equal(calls[0].params.stage_action, 'skip');
  assert.equal(getReminders().length, 0);
});

test('failed request keeps the toast visible', async () => {
  ingestStageDueEvent({
    event_type: 'long_horizon.stage_due',
    task_id: 'lhc_1',
    stage_id: 'lhs_1',
    title: '年度发布',
    stage_title: '准备材料',
  });

  await assert.rejects(
    () =>
      runLongHorizonStageAction(
        {
          taskId: 'lhc_1',
          stageId: 'lhs_1',
          action: 'skip',
        },
        {
          request: async () => {
            throw new Error('network down');
          },
        }
      ),
    /network down/
  );

  assert.equal(getReminders().length, 1);
});

test('startup inbox restores unresolved due reminders', () => {
  restoreRemindersFromInbox([
    {
      task: {
        id: 'lhc_2',
        title: '季度复盘',
        exec_session_id: 'longhorizon_lhc_2',
      },
      stage: {
        id: 'lhs_9',
        title: '收集材料',
        due_at: '2026-09-10T01:00:00+00:00',
        hint: '先列提纲',
        status: 'due',
      },
    },
  ]);

  const reminders = getReminders();
  assert.equal(reminders.length, 1);
  assert.equal(reminders[0].id, 'lhc_2:lhs_9');
  assert.equal(reminders[0].execSessionId, 'longhorizon_lhc_2');
  dismissReminder(reminders[0].id);
  assert.equal(getReminders().length, 0);
});
