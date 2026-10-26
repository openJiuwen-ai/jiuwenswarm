import assert from 'node:assert/strict';
import test from 'node:test';

import { createStreamDeltaBatcher } from '../node_modules/.cache/stream-delta-batcher/services/streamDeltaBatcher.js';

function fakeScheduler() {
  let nextId = 1;
  const callbacks = new Map();
  return {
    schedule(callback) {
      const id = nextId;
      nextId += 1;
      callbacks.set(id, callback);
      return id;
    },
    cancel(id) {
      callbacks.delete(id);
    },
    runAll() {
      for (const [id, callback] of [...callbacks.entries()]) {
        callbacks.delete(id);
        callback();
      }
    },
    size() {
      return callbacks.size;
    },
  };
}

test('coalesces chunks for one key into one ordered flush', () => {
  const scheduler = fakeScheduler();
  const values = [];
  const batcher = createStreamDeltaBatcher({
    schedule: scheduler.schedule,
    cancel: scheduler.cancel,
  });

  batcher.enqueue('s1:m1', 'A', value => values.push(value));
  batcher.enqueue('s1:m1', ' B', value => values.push(value));
  scheduler.runAll();

  assert.deepEqual(values, ['A B']);
});

test('keeps different stream keys isolated', () => {
  const scheduler = fakeScheduler();
  const values = [];
  const batcher = createStreamDeltaBatcher({
    schedule: scheduler.schedule,
    cancel: scheduler.cancel,
  });

  batcher.enqueue('s1:m1', 'A', value => values.push(['s1', value]));
  batcher.enqueue('s2:m2', 'B', value => values.push(['s2', value]));
  scheduler.runAll();

  assert.deepEqual(values, [
    ['s1', 'A'],
    ['s2', 'B'],
  ]);
});

test('flush submits immediately and clear discards pending content', () => {
  const scheduler = fakeScheduler();
  const values = [];
  const batcher = createStreamDeltaBatcher({
    schedule: scheduler.schedule,
    cancel: scheduler.cancel,
  });

  batcher.enqueue('flush', 'A', value => values.push(value));
  batcher.enqueue('clear', 'B', value => values.push(value));
  batcher.flush('flush');
  batcher.clear('clear');
  scheduler.runAll();

  assert.deepEqual(values, ['A']);
});

test('flushAll preserves content order within every key', () => {
  const scheduler = fakeScheduler();
  const values = [];
  const batcher = createStreamDeltaBatcher({
    schedule: scheduler.schedule,
    cancel: scheduler.cancel,
  });

  batcher.enqueue('one', 'A', value => values.push(value));
  batcher.enqueue('one', 'B', value => values.push(value));
  batcher.enqueue('two', 'C', value => values.push(value));
  batcher.flushAll();

  assert.deepEqual(values, ['AB', 'C']);
  assert.equal(scheduler.size(), 0);
});

test('clearAll discards every pending key', () => {
  const scheduler = fakeScheduler();
  const values = [];
  const batcher = createStreamDeltaBatcher({
    schedule: scheduler.schedule,
    cancel: scheduler.cancel,
  });

  batcher.enqueue('one', 'A', value => values.push(value));
  batcher.enqueue('two', 'B', value => values.push(value));
  batcher.clearAll();
  scheduler.runAll();

  assert.deepEqual(values, []);
  assert.equal(scheduler.size(), 0);
});

test('empty content does not schedule a flush', () => {
  const scheduler = fakeScheduler();
  const batcher = createStreamDeltaBatcher({
    schedule: scheduler.schedule,
    cancel: scheduler.cancel,
  });

  batcher.enqueue('s1:m1', '', () => assert.fail('must not flush'));

  assert.equal(scheduler.size(), 0);
});

test('flushBefore applies a barrier action after pending delta content', () => {
  const scheduler = fakeScheduler();
  const events = [];
  const batcher = createStreamDeltaBatcher({
    schedule: scheduler.schedule,
    cancel: scheduler.cancel,
  });

  batcher.enqueue('s1:m1', 'A', value => events.push(`delta:${value}`));
  batcher.flushBefore('s1:m1', () => events.push('media'));
  scheduler.runAll();

  assert.deepEqual(events, ['delta:A', 'media']);
});
