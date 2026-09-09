import assert from 'node:assert/strict';
import test from 'node:test';

import { RealtimeVideoFrameScheduler } from '../../../../channels/web/frontend/node_modules/.cache/video-source/videoSource.js';

const frame = (source, version) => ({
  source_id: source,
  data_url: `${source}-${version}`,
});

test('rotates screens at an aggregate one-frame-per-second cadence', () => {
  const scheduler = new RealtimeVideoFrameScheduler();
  const first = [frame('screen-a', 1), frame('screen-b', 1)];

  assert.equal(scheduler.take(first, 0)?.source_id, 'screen-a');
  assert.equal(scheduler.take(first, 999), null);
  assert.equal(scheduler.take(first, 1_000)?.source_id, 'screen-b');

  const second = [frame('screen-a', 2), frame('screen-b', 2)];
  assert.equal(scheduler.take(second, 2_000)?.source_id, 'screen-a');
  assert.equal(scheduler.take(second, 3_000)?.source_id, 'screen-b');
});

test('does not resend a stale frame when capture is paused', () => {
  const scheduler = new RealtimeVideoFrameScheduler();
  const still = [frame('camera', 1)];

  assert.equal(scheduler.take(still, 0)?.data_url, 'camera-1');
  assert.equal(scheduler.take(still, 2_000), null);
  assert.equal(scheduler.take([frame('camera', 2)], 2_001)?.data_url, 'camera-2');
});

test('continues fairly when a screen source is removed', () => {
  const scheduler = new RealtimeVideoFrameScheduler();

  assert.equal(scheduler.take([frame('screen-a', 1), frame('screen-b', 1), frame('screen-c', 1)], 0)?.source_id, 'screen-a');
  assert.equal(scheduler.take([frame('screen-a', 2), frame('screen-c', 2)], 1_000)?.source_id, 'screen-c');
  assert.equal(scheduler.take([frame('screen-a', 3), frame('screen-c', 3)], 2_000)?.source_id, 'screen-a');
});
