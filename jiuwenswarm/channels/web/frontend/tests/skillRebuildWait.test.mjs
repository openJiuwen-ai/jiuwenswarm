import assert from 'node:assert/strict';
import { test } from 'node:test';

const { waitForSkillRebuild } = await import('../node_modules/.cache/skill-panel-races/skillRebuildWait.mjs');

/** 可编程假时钟：sleep 立即解决并推进 Date.now。 */
function fakeClock() {
  let now = 0;
  const sleepLog = [];
  return {
    now: () => now,
    sleep: async (ms) => {
      sleepLog.push(ms);
      now += ms;
    },
    sleepLog,
  };
}

function makeStatus(status) {
  return { success: status.success ?? true, status: status.status, detail: status.detail };
}

test('resolves done when a poll reports the rebuild finished', async () => {
  const clock = fakeClock();
  const seen = [];
  const outcome = await waitForSkillRebuild('writer', 'rb-1', {
    pollMs: 3000,
    waitMs: 30000,
    sleep: clock.sleep,
    now: clock.now,
    requestStatus: async () => {
      seen.push(1);
      return makeStatus({ status: 'done', success: true });
    },
  });
  assert.equal(outcome, 'done');
  assert.equal(seen.length, 1);
  assert.deepEqual(clock.sleepLog, [3000], '先睡一个轮询间隔再查状态');
});

test('throws when the rebuild failed, keeping the original error semantics', async () => {
  const clock = fakeClock();
  await assert.rejects(
    waitForSkillRebuild('writer', 'rb-1', {
      pollMs: 3000,
      waitMs: 30000,
      sleep: clock.sleep,
    now: clock.now,
      requestStatus: async () => makeStatus({ status: 'failed', success: false, detail: 'boom' }),
    }),
    /boom/,
  );
});

test('returns timeout after the wait budget without a terminal status', async () => {
  const clock = fakeClock();
  let polls = 0;
  const outcome = await waitForSkillRebuild('writer', 'rb-1', {
    pollMs: 3000,
    waitMs: 9000,
    sleep: clock.sleep,
    now: clock.now,
    requestStatus: async () => {
      polls += 1;
      return makeStatus({ status: 'running', success: true });
    },
  });
  assert.equal(outcome, 'timeout');
  assert.equal(polls, 3, '90 秒预算 / 3 秒间隔 = 3 次轮询');
});

test('stops polling once cancelled: unmount or a newer rebuild must end the loop', async () => {
  const clock = fakeClock();
  let cancelled = false;
  let polls = 0;
  const outcome = await waitForSkillRebuild('writer', 'rb-1', {
    pollMs: 3000,
    waitMs: 30 * 60 * 1000,
    sleep: clock.sleep,
    now: clock.now,
    requestStatus: async () => {
      polls += 1;
      if (polls === 2) cancelled = true;
      return makeStatus({ status: 'running', success: true });
    },
    isCancelled: () => cancelled,
  });
  assert.equal(outcome, 'cancelled');
  assert.equal(polls, 2, '取消后不再发起后续轮询（旧行为会继续 30 分钟）');
});

test('cancellation between sleep and status check also exits without extra polling', async () => {
  const clock = fakeClock();
  let cancelled = false;
  let polls = 0;
  const outcome = await waitForSkillRebuild('writer', 'rb-1', {
    pollMs: 3000,
    waitMs: 30 * 60 * 1000,
    sleep: async (ms) => {
      await clock.sleep(ms);
      cancelled = true;
    },
    now: clock.now,
    requestStatus: async () => {
      polls += 1;
      return makeStatus({ status: 'running', success: true });
    },
    isCancelled: () => cancelled,
  });
  assert.equal(outcome, 'cancelled');
  assert.equal(polls, 0, '睡眠期间取消则连一次状态查询都不发');
});

test('a done result arriving after cancellation is discarded', async () => {
  const clock = fakeClock();
  let cancelled = false;
  const outcome = await waitForSkillRebuild('writer', 'rb-1', {
    pollMs: 3000,
    waitMs: 30000,
    sleep: clock.sleep,
    now: clock.now,
    requestStatus: async () => {
      cancelled = true;
      return makeStatus({ status: 'done', success: true });
    },
    isCancelled: () => cancelled,
  });
  assert.equal(outcome, 'cancelled', '状态查询后才发现取消，仍按取消处理，不回写 done');
});
