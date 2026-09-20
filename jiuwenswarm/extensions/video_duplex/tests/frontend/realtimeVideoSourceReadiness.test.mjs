import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isVideoSourceReady,
  waitForFirstVideoFrame,
} from '../../../../channels/web/frontend/node_modules/.cache/video-source/videoSource.js';

const stream = (readyState) => ({
  getVideoTracks: () => [{ readyState }],
});

test('camera, screen, file, and voice readiness stay distinct', () => {
  const base = {
    cameraStream: null,
    screens: [],
    screenStreams: new Map(),
    video: null,
  };

  assert.equal(isVideoSourceReady({ ...base, source: null }), true);
  assert.equal(isVideoSourceReady({ ...base, source: 'camera' }), false);
  assert.equal(isVideoSourceReady({ ...base, source: 'camera', cameraStream: stream('live') }), true);
  assert.equal(isVideoSourceReady({ ...base, source: 'file', video: { paused: true } }), false);
  assert.equal(isVideoSourceReady({ ...base, source: 'file', video: { paused: false } }), true);
  assert.equal(isVideoSourceReady({
    ...base,
    source: 'screen',
    screens: [{ id: 'a' }, { id: 'b' }],
    screenStreams: new Map([['a', stream('live')], ['b', stream('ended')]]),
  }), false);
  assert.equal(isVideoSourceReady({
    ...base,
    source: 'screen',
    screens: [{ id: 'a' }, { id: 'b' }],
    screenStreams: new Map([['a', stream('live')], ['b', stream('live')]]),
  }), true);
});

test('camera start waits for both a live source and its first cached frame', async () => {
  let now = 0;
  let polls = 0;
  let hasFrame = false;
  const ready = await waitForFirstVideoFrame(
    () => true,
    () => hasFrame,
    {
      now: () => now,
      wait: async (delayMs) => {
        now += delayMs;
        polls += 1;
        if (polls === 2) hasFrame = true;
      },
    },
  );

  assert.equal(ready, true);
  assert.equal(polls, 2);
});

test('camera start fails immediately when its track is no longer live', async () => {
  let waited = false;
  const ready = await waitForFirstVideoFrame(
    () => false,
    () => false,
    { wait: async () => { waited = true; } },
  );

  assert.equal(ready, false);
  assert.equal(waited, false);
});
