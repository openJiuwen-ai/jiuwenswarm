import test from 'node:test';
import assert from 'node:assert/strict';
import { createPresentationOverlay } from '../node_modules/.cache/application-presentation/protocol.mjs';

const config = (key = 'pet') => ({ key, mode: 'overlay', src: '/extension-assets/pet/g1/index.html',
  resourceUrl: 'http://localhost/extension-assets/pet/g1/index.html', title: 'Pet', width: 160, height: 120,
  config: {}, state: { connected: true } });

test('in-app readiness and movement use the entire application viewport', async () => {
  let area = { x: 0, y: 0, width: 1200, height: 800 };
  const overlay = createPresentationOverlay(() => area);
  try {
    const opening = overlay.api.open(config());
    assert.equal(overlay.getSnapshot().ready, false);
    const frame = overlay.getSnapshot().frameApi;
    const updates = [];
    frame.onUpdate(value => updates.push(value));
    assert.deepEqual((await frame.bootstrap()).host.workArea, area);
    await frame.ready();
    const { generation } = await opening;
    assert.equal(overlay.getSnapshot().ready, true);
    await overlay.api.move('pet', -900, -500);
    assert.deepEqual(overlay.getSnapshot().bounds, { x: 116, y: 108, width: 160, height: 120 });
    await overlay.api.move('pet', 4096, 4096);
    assert.deepEqual(overlay.getSnapshot().bounds, { x: 1040, y: 680, width: 160, height: 120 });
    await overlay.api.resize('pet', 300, 200);
    assert.deepEqual(overlay.getSnapshot().bounds, { x: 900, y: 600, width: 300, height: 200 });
    area = { x: 0, y: 0, width: 240, height: 160 };
    overlay.reflow();
    assert.deepEqual(overlay.getSnapshot().bounds, area);
    assert.deepEqual(updates.at(-1).host.workArea, area);
    const results = [];
    frame.onResult(value => results.push(value));
    assert.equal(await overlay.api.respond('old', { id: 1 }), false);
    assert.equal(await overlay.api.respond(generation, { id: 2 }), true);
    assert.deepEqual(results, [{ id: 2 }]);
  } finally { overlay.dispose(); }
});

test('replacement, close and disposal invalidate old sandbox operations', async () => {
  const overlay = createPresentationOverlay(() => ({ x: 0, y: 0, width: 900, height: 700 }));
  const closes = [];
  overlay.api.onClosed(event => closes.push(event));
  const first = overlay.api.open(config());
  const oldFrame = overlay.getSnapshot().frameApi;
  const rejected = assert.rejects(first, /replaced/);
  const second = overlay.api.open(config('other'));
  await rejected;
  await assert.rejects(oldFrame.ready(), /STALE_REQUEST/);
  await assert.rejects(oldFrame.request({}), /STALE_REQUEST/);
  const newFrame = overlay.getSnapshot().frameApi;
  await newFrame.ready(); await second;
  const requests = [];
  overlay.api.onRequest(value => requests.push(value));
  await newFrame.request({ id: 1 });
  assert.equal(requests[0].key, 'other');
  assert.equal(await overlay.api.close('pet'), false);
  assert.equal(await overlay.api.close('other'), true);
  assert.equal(overlay.getSnapshot(), null);
  assert.deepEqual(closes.map(value => value.reason), ['replaced', 'closed']);
  await assert.rejects(newFrame.bootstrap(), /STALE_REQUEST/);
  overlay.dispose();
});

test('native transitions and pixel hit testing fail explicitly in an in-app surface', async () => {
  const overlay = createPresentationOverlay(() => ({ x: 0, y: 0, width: 900, height: 700 }));
  try {
    const opening = overlay.api.open(config());
    const frame = overlay.getSnapshot().frameApi;
    await assert.rejects(frame.ready({ durationMs: 300 }), /UNSUPPORTED_ACTION/);
    await frame.ready(); await opening;
    await assert.rejects(overlay.api.hitTest('pet', false), /UNSUPPORTED_ACTION/);
    await assert.rejects(overlay.api.transitionWorkspace('pet', { durationMs: 300 }), /UNSUPPORTED_ACTION/);
  } finally { overlay.dispose(); }
});

test('a resource that never becomes ready releases its surface and rejects activation', async () => {
  const overlay = createPresentationOverlay(() => ({ x: 0, y: 0, width: 900, height: 700 }), 5);
  try {
    await assert.rejects(overlay.api.open(config()), /ready_timeout/);
    assert.equal(overlay.getSnapshot(), null);
  } finally { overlay.dispose(); }
});
