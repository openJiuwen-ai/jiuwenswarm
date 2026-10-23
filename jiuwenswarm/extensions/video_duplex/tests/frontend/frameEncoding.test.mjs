import assert from 'node:assert/strict';
import test from 'node:test';

import {
  FRAME_MAX_JPEG_BYTES,
  FrameEncoder,
  fitFrameSize,
  frameStepsFor,
} from '../../../../channels/web/frontend/node_modules/.cache/frame-encoding/frameEncoding.mjs';

// Fake JPEG size: bytes per pixel shrink with quality; `density` stands in for picture detail.
const fakeRenderer = (density, calls = []) => async (width, height, quality) => {
  calls.push(`${width}x${height}@${quality}`);
  return { size: Math.round(width * height * quality * density) };
};

test('keeps 1080p for a 4K source and never upscales smaller sources', () => {
  assert.deepEqual(fitFrameSize(3840, 2160, 1920), { width: 1920, height: 1080 });
  assert.deepEqual(fitFrameSize(2160, 3840, 1920), { width: 1080, height: 1920 });
  // 4:3 at 1920 long edge would be 1440 high; the 1080p limit caps the short edge.
  assert.deepEqual(fitFrameSize(2560, 1920, 1920), { width: 1440, height: 1080 });
  assert.deepEqual(fitFrameSize(1280, 720, 1920), { width: 1280, height: 720 });
  assert.deepEqual(fitFrameSize(640, 480, 768), { width: 640, height: 480 });
});

test('a camera at 1280x720 skips duplicate steps and keeps its full resolution', async () => {
  const steps = frameStepsFor(1280, 720);
  assert.deepEqual(steps.slice(0, 3).map((s) => `${s.width}x${s.height}@${s.quality}`), [
    '1280x720@0.72',
    '1280x720@0.5',
    '1280x720@0.3',
  ]);
  assert.equal(steps.length, 6);

  const calls = [];
  const frame = await new FrameEncoder().encode('camera', 1280, 720, fakeRenderer(0.2, calls));
  assert.equal(frame.width, 1280);
  assert.equal(frame.quality, 0.72);
  assert.deepEqual(calls, ['1280x720@0.72']);
});

test('lowers quality before resolution until the frame fits the budget', async () => {
  const calls = [];
  // Density chosen so every 1920x1080 step is too big and 1280x720 at q0.5 just fits.
  const density = (FRAME_MAX_JPEG_BYTES / (1280 * 720 * 0.5)) * 0.99;
  const frame = await new FrameEncoder().encode('screen', 3840, 2160, fakeRenderer(density, calls));
  assert.deepEqual(calls, [
    '1920x1080@0.72',
    '1920x1080@0.5',
    '1920x1080@0.3',
    '1280x720@0.72',
    '1280x720@0.5',
  ]);
  assert.equal(frame.width, 1280);
  assert.equal(frame.quality, 0.5);
  assert.ok(frame.blob.size <= FRAME_MAX_JPEG_BYTES);
});

test('remembers the fitting step per source and climbs back when the picture gets simpler', async () => {
  const encoder = new FrameEncoder();
  const dense = (FRAME_MAX_JPEG_BYTES / (1280 * 720 * 0.5)) * 0.99;
  await encoder.encode('screen', 3840, 2160, fakeRenderer(dense));

  // Same busy picture: one probe one step up, then the remembered step. No full ladder.
  const steady = [];
  const again = await encoder.encode('screen', 3840, 2160, fakeRenderer(dense, steady));
  assert.deepEqual(steady, ['1280x720@0.72', '1280x720@0.5']);
  assert.equal(again.quality, 0.5);

  // Mostly blank picture: each capture climbs one step until it is back at 1080p q0.72.
  const sparse = 0.01;
  let frame;
  for (let i = 0; i < 4; i += 1) frame = await encoder.encode('screen', 3840, 2160, fakeRenderer(sparse));
  assert.equal(frame.width, 1920);
  assert.equal(frame.quality, 0.72);

  // Another source is not affected by this source's history.
  const other = [];
  await encoder.encode('screen-b', 3840, 2160, fakeRenderer(sparse, other));
  assert.deepEqual(other, ['1920x1080@0.72']);
});

test('drops the frame when even the smallest step is too big', async () => {
  const frame = await new FrameEncoder().encode('screen', 3840, 2160, fakeRenderer(1000));
  assert.equal(frame, null);
});

test('stops encoding when the capture is cancelled', async () => {
  const calls = [];
  const frame = await new FrameEncoder().encode('screen', 3840, 2160, async (w, h, q) => {
    calls.push(q);
    return null;
  });
  assert.equal(frame, null);
  assert.equal(calls.length, 1);
});
