import assert from 'node:assert/strict';
import test from 'node:test';

globalThis.window = globalThis;
globalThis.window.location = { protocol: 'http:', host: 'localhost:18180' };
const values = new Map();
globalThis.localStorage = {
  getItem: (key) => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, value),
  removeItem: (key) => values.delete(key),
};

const { useCronStore } = await import('../node_modules/.cache/cron-run-notifications/stores/cronStore.mjs');

function update(overrides = {}) {
  return {
    event_id: 'run:1', run_id: 'run', job_id: 'job', session_id: 'session',
    revision: 1, status: 'running', phase: 'started', project_id: 'default',
    mode: 'agent.work.normal', timestamp: 1, notify: false, ...overrides,
  };
}

test('run notifications are idempotent and a late start cannot revert a terminal run', () => {
  useCronStore.setState({ runUpdates: {} });
  const store = useCronStore.getState();
  assert.ok(store.applyRunUpdate(update()));
  assert.equal(store.applyRunUpdate(update()), null);
  assert.ok(store.applyRunUpdate(update({ revision: 2, event_id: 'run:2', status: 'succeeded', phase: 'finished' })));
  assert.equal(store.applyRunUpdate(update({ revision: 3 })), null);
  assert.ok(store.applyRunUpdate(update({ revision: 4, status: 'succeeded', phase: 'reminder', notify: true })));
  assert.equal(useCronStore.getState().runUpdates.run.status, 'succeeded');
});

test('concurrent runs of one job retain independent Session identities', () => {
  useCronStore.setState({ runUpdates: {} });
  const store = useCronStore.getState();
  store.applyRunUpdate(update());
  store.applyRunUpdate(update({ run_id: 'second', event_id: 'second:1', session_id: 'second-session' }));
  assert.equal(useCronStore.getState().runUpdates.run.session_id, 'session');
  assert.equal(useCronStore.getState().runUpdates.second.session_id, 'second-session');
});

test('an older run finishing or reminding cannot replace a newer running label', () => {
  useCronStore.setState({ runUpdates: {} });
  const store = useCronStore.getState();
  store.applyRunUpdate(update({ timestamp: 10 }));
  store.applyRunUpdate(update({ run_id: 'second', event_id: 'second:1', session_id: 'second-session', timestamp: 20 }));
  for (const [revision, phase] of [[2, 'finished'], [3, 'reminder']]) {
    store.applyRunUpdate(update({
      revision, event_id: `run:${revision}`, timestamp: 20 + revision,
      status: 'succeeded', phase, notify: phase === 'reminder',
    }));
    const runs = Object.values(useCronStore.getState().runUpdates);
    const latest = runs.sort((left, right) => right.firstSeenAt - left.firstSeenAt)[0];
    assert.equal(latest.run_id, 'second');
    assert.equal(latest.status, 'running');
  }
  assert.equal(useCronStore.getState().runUpdates.run.firstSeenAt, 10);
  assert.equal(useCronStore.getState().runUpdates.run.timestamp, 23);
  assert.equal(useCronStore.getState().runUpdates.run.notify, true);
});

test('malformed notifications do not create state or unread entries', () => {
  useCronStore.setState({ runUpdates: {} });
  const store = useCronStore.getState();
  assert.equal(store.applyRunUpdate(update({ revision: Number.NaN })), null);
  assert.equal(store.applyRunUpdate(update({ status: 'unexpected' })), null);
  assert.equal(store.applyRunUpdate(update({ run_id: '' })), null);
  assert.equal(store.applyRunUpdate(update({ session_id: 7 })), null);
  assert.equal(store.applyRunUpdate(update({ project_id: null })), null);
  assert.deepEqual(useCronStore.getState().runUpdates, {});
  assert.ok(store.applyRunUpdate(update({ session_id: null })));
  assert.equal(useCronStore.getState().runUpdates.run.session_id, null);
});


test('disconnect clears unconfirmed runs without inventing a terminal status', () => {
  useCronStore.setState({ runUpdates: {} });
  const store = useCronStore.getState();
  store.applyRunUpdate(update());
  store.applyRunUpdate(update({ run_id: 'done', event_id: 'done:2', revision: 2, status: 'succeeded', phase: 'finished' }));
  store.clearUnconfirmedRuns();
  assert.equal(useCronStore.getState().runUpdates.run, undefined);
  assert.equal(useCronStore.getState().runUpdates.done.status, 'succeeded');
  store.applyRunUpdate(update({ revision: 2, status: 'failed', phase: 'finished' }));
  assert.equal(useCronStore.getState().runUpdates.run.status, 'failed');
});

test('online cache bounds run identities to 256 entries', () => {
  useCronStore.setState({ runUpdates: {} });
  for (let i = 0; i < 260; i += 1) {
    useCronStore.getState().applyRunUpdate(update({ run_id: `run-${i}`, event_id: `run-${i}:1` }));
  }
  assert.equal(Object.keys(useCronStore.getState().runUpdates).length, 256);
  assert.equal(useCronStore.getState().runUpdates['run-0'], undefined);
  assert.ok(useCronStore.getState().runUpdates['run-259']);
});
