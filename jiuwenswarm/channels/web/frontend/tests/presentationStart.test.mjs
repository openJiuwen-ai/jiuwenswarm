import test from 'node:test';
import assert from 'node:assert/strict';
import { createPresentationStartHandler } from '../node_modules/.cache/presentation-start/start.mjs';

const target = { scope: 'application', slot_id: 'application.presentation', package_id: 'test.forms', instance_key: 'bee' };
const request = () => ({ request_id: 'a'.repeat(32), target, revision: 2, deadline: Date.now() + 30000 });
function setup() {
  let snapshot = { connected: true, state: 'active', revision: 2, frame: { tree: { component: 'ApplicationSurface',
    props: { title: { zh: '蜜蜂', en: 'Bee' }, src: '/extension-assets/test.forms/g1/bee.html', width: 300, height: 200, config: {} } } } };
  const activated = [], results = [];
  const options = { runtime: { getInstance: key => key === '|application.presentation|test.forms|bee' ? snapshot : undefined },
    language: () => 'en', desktop: () => true, activate: async value => { activated.push(value); },
    result: async value => { results.push(value); } };
  return { options, activated, results, replace: value => { snapshot = value; } };
}

test('starts the exact current root and reports success after native readiness', async () => {
  const ctx = setup(); let ready;
  ctx.options.activate = value => { ctx.activated.push(value); return new Promise(resolve => { ready = resolve; }); };
  const pending = createPresentationStartHandler(ctx.options)(request());
  assert.equal(ctx.results.length, 0);
  assert.equal(ctx.activated[0].title, 'Bee');
  assert.equal(ctx.activated[0].src, '/extension-assets/test.forms/g1/bee.html');
  ready(); await pending;
  assert.equal(ctx.results[0].code, 'STARTED');
});

test('browser, stale target, expired command and inactive frame never open', async () => {
  for (const kind of ['browser', 'revision', 'target', 'expired', 'inactive']) {
    const ctx = setup(), payload = request();
    if (kind === 'browser') ctx.options.desktop = () => false;
    if (kind === 'revision') payload.revision = 1;
    if (kind === 'target') payload.target = { ...target, package_id: 'other' };
    if (kind === 'expired') payload.deadline = Date.now() - 1;
    if (kind === 'inactive') ctx.replace(undefined);
    await createPresentationStartHandler(ctx.options)(payload);
    assert.equal(ctx.activated.length, 0, kind);
    assert.equal(ctx.results[0].code, kind === 'browser' ? 'DESKTOP_REQUIRED' : 'TARGET_INACTIVE');
  }
});

test('never accepts commands carrying URLs, scripts or extra target fields', async () => {
  const ctx = setup(), handle = createPresentationStartHandler(ctx.options);
  for (const payload of [{ ...request(), src: 'https://example.com' }, { ...request(), revision: true },
    { ...request(), request_id: '' }, { ...request(), target: { ...target, connection_id: 'other' } }]) await handle(payload);
  assert.equal(ctx.activated.length, 0); assert.equal(ctx.results.length, 0);
});

test('busy and failed native opens are reported without retry', async () => {
  for (const [message, expected] of [['BUSY', 'BUSY'], ['failed', 'OPEN_FAILED']]) {
    const ctx = setup(); let calls = 0;
    ctx.options.activate = async () => { calls += 1; throw new Error(message); };
    await createPresentationStartHandler(ctx.options)(request());
    assert.equal(ctx.results[0].code, expected); assert.equal(calls, 1);
  }
});

test('a candidate replaced while opening cannot report success', async () => {
  const ctx = setup(); ctx.options.activate = async () => ctx.replace(undefined);
  await createPresentationStartHandler(ctx.options)(request());
  assert.equal(ctx.results[0].code, 'TARGET_INACTIVE');
});
