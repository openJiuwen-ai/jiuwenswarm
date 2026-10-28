import assert from 'node:assert/strict';
import test from 'node:test';
import { HistoryRefreshQueue } from '../node_modules/.cache/history-recovery/historyRefreshQueue.mjs';

test('completion after page restore refreshes history once after processing ends', () => {
  const queue = new HistoryRefreshQueue();
  queue.request('restored');
  assert.equal(queue.take('restored', true), false);
  assert.equal(queue.take('restored', false), true);
  assert.equal(queue.take('restored', false), false);
});

test('late old-turn finals cannot replace a new running turn', () => {
  const queue = new HistoryRefreshQueue();
  queue.request('session');
  queue.request('session');
  assert.equal(queue.take('session', true), false);
  assert.equal(queue.take('session', true), false);
  assert.equal(queue.take('session', false), true);
  assert.equal(queue.take('session', false), false);
});

test('inactive sessions retain invalidation until revisited', () => {
  const queue = new HistoryRefreshQueue();
  queue.request('background');
  assert.equal(queue.take('foreground', false), false);
  assert.equal(queue.take('background', false), true);
});

test('notification during history loading survives until the current load finishes', () => {
  const queue = new HistoryRefreshQueue();
  queue.request('session');
  assert.equal(queue.take('session', false), true);
  queue.request('session');
  assert.equal(queue.take('session', true), false);
  assert.equal(queue.take('session', false), true);
});
