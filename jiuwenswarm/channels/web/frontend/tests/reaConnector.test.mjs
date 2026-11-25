import assert from 'node:assert/strict';
import test from 'node:test';

import {
  connectReaIfNeeded,
  shouldAutoConnectRea,
} from '../node_modules/.cache/rea-connector/reaConnector.mjs';

test('only macOS auto-connects REA during session navigation', () => {
  assert.equal(shouldAutoConnectRea('Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0)'), true);
  assert.equal(shouldAutoConnectRea('Mozilla/5.0 (Windows NT 10.0; Win64; x64)'), false);
  assert.equal(shouldAutoConnectRea('Mozilla/5.0 (X11; Linux x86_64)'), false);
});

test('already connected REA skips another connect call', async () => {
  let calls = 0;
  const connected = await connectReaIfNeeded('connected', async () => {
    calls += 1;
    return { type: 'connected' };
  });

  assert.equal(connected, true);
  assert.equal(calls, 0);
});

test('disconnected REA connects before navigation', async () => {
  const calls = [];
  const connected = await connectReaIfNeeded('disconnected', async (name) => {
    calls.push(name);
    return { type: 'connected' };
  });

  assert.equal(connected, true);
  assert.deepEqual(calls, ['rea']);
});

test('unsuccessful REA connection returns false without blocking session creation', async () => {
  const noResponse = await connectReaIfNeeded('disconnected', async () => null);
  const rejected = await connectReaIfNeeded('disconnected', async () => {
    throw new Error('connect failed');
  });

  assert.equal(noResponse, false);
  assert.equal(rejected, false);
});
