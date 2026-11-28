import assert from 'node:assert/strict';
import test from 'node:test';
import { connectionRetryDelay } from '../node_modules/.cache/connection-retry/connectionRetry.mjs';

for (const host of ['localhost', '127.0.0.1', '[::1]']) {
  test(`first local connection has bounded fast retries: ${host}`, () => {
    assert.equal(connectionRetryDelay(1, `ws://${host}:19000/ws`, false, 0, 0.5), 250);
    assert.equal(connectionRetryDelay(2, `ws://${host}:19000/ws`, false, 1000, 0.5), 500);
    assert.equal(connectionRetryDelay(12, `ws://${host}:19000/ws`, false, 14_999, 0.5), 500);
  });
}

test('startup retry window expires without resetting long-lived connections', () => {
  assert.equal(connectionRetryDelay(2, 'ws://localhost/ws', false, 15_000, 0.5), 2000);
  assert.equal(connectionRetryDelay(6, 'ws://localhost/ws', false, 20_000, 0.5), 2000);
  assert.equal(connectionRetryDelay(1, 'ws://localhost/ws', false, -1, 0.5), 1000);
});

for (const url of ['wss://service.example/ws', 'ws://localhost.example/ws', 'invalid address']) {
  test(`remote and invalid addresses retain the original schedule: ${url}`, () => {
    assert.deepEqual(
      Array.from({ length: 7 }, (_, index) => connectionRetryDelay(index + 1, url, false, 100, 0.5)),
      [1000, 2000, 4000, 8000, 16000, 2000, 2000],
    );
  });
}

test('successful prior connection retains the original recovery schedule', () => {
  assert.deepEqual(
    Array.from({ length: 7 }, (_, index) => connectionRetryDelay(index + 1, 'ws://localhost/ws', true, 100, 0.5)),
    [1000, 2000, 4000, 8000, 16000, 2000, 2000],
  );
});

test('startup jitter is bounded for simultaneous local clients', () => {
  assert.equal(connectionRetryDelay(1, 'ws://localhost/ws', false, 100, 0), 225);
  assert.equal(connectionRetryDelay(1, 'ws://localhost/ws', false, 100, 1), 275);
  assert.equal(connectionRetryDelay(2, 'ws://localhost/ws', false, 100, 0), 450);
  assert.equal(connectionRetryDelay(2, 'ws://localhost/ws', false, 100, 1), 550);
});
