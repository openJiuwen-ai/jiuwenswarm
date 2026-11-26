const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { setImmediate: nextTurn } = require('node:timers/promises');
const vm = require('node:vm');
const { registerMediaDevicePermissions } = require('../../jiuwenswarm/channels/desktop/electron/media_device_permissions.cjs');

const HOST = 'http://127.0.0.1:5173';
const GUEST = 'https://meeting.example.com:8443';
const ROOM = GUEST + '/room?room-id=private';

function setup(choose, locale = 'en-US') {
  let checkPermission;
  let requestPermission;
  const prompts = [];
  const mainFrame = { frameTreeNodeId: 1, frameToken: 'host', frames: [], framesInSubtree: [] };
  const elements = [];
  class IframeElement {
    constructor(name, features) {
      this.name = name;
      this.featurePolicy = { allowsFeature: (feature, origin) => origin === GUEST && features.includes(feature) };
    }
  }
  const contents = Object.assign(new EventEmitter(), {
    mainFrame,
    getURL: () => HOST + '/workbench',
    isDestroyed: () => false,
    executeJavaScriptInIsolatedWorld: async (_world, [{ code }]) => vm.runInNewContext(code, {
      HTMLIFrameElement: IframeElement,
      document: { getElementsByName: name => elements.filter(element => element.name === name) },
    }),
    session: {
      setPermissionCheckHandler: handler => { checkPermission = handler; },
      setPermissionRequestHandler: handler => { requestPermission = handler; },
    },
  });
  const window = { webContents: contents, isDestroyed: () => false };
  const app = { getLocale: () => locale, commandLine: { hasSwitch: () => false } };
  const dialog = {
    showMessageBox(parent, options) {
      assert.equal(parent, window);
      prompts.push(options);
      return choose(options);
    },
  };
  registerMediaDevicePermissions({ window, app, dialog, getFrontendUrl: () => HOST });

  function embed({ url = ROOM, name = 'workswarm-media-room', features = ['camera', 'microphone'], parent = mainFrame } = {}) {
    const id = mainFrame.framesInSubtree.length + 2;
    const frame = { frameTreeNodeId: id, frameToken: String(id), parent, name, url: '', origin: HOST };
    if (parent === mainFrame) {
      mainFrame.frames.push(frame);
      elements.push(new IframeElement(name, features));
    }
    mainFrame.framesInSubtree.push(frame);
    contents.emit('frame-created', {}, { frame });
    Object.assign(frame, { url, origin: GUEST });
    return frame;
  }

  const firstFrame = embed();
  const details = { isMainFrame: false, securityOrigin: GUEST + '/', requestingUrl: ROOM, mediaTypes: ['audio', 'video'] };
  return {
    contents,
    embed,
    firstFrame,
    prompts,
    check: (changes = {}, permission = 'media', source = contents) =>
      checkPermission(source, permission, GUEST, { ...details, ...changes }),
    request: (changes = {}, permission = 'media', source = contents) =>
      new Promise(resolve => requestPermission(source, permission, resolve, { ...details, ...changes })),
  };
}

test('a cross-origin device request shows the requesting origin and grants only after confirmation', async () => {
  const decision = Promise.withResolvers();
  const host = setup(() => decision.promise);
  assert.equal(host.check(), false);
  const request = host.request();
  await nextTurn();
  const [prompt] = host.prompts;
  assert.equal(prompt.detail, GUEST);
  assert.match(prompt.message, /camera and microphone/);
  assert.deepEqual(prompt.buttons, ['Deny', 'Allow once']);
  assert.equal(prompt.defaultId, 0);
  assert.equal(prompt.cancelId, 0);
  decision.resolve({ response: 1 });
  assert.equal(await request, true);
  assert.equal(host.check(), false);
  assert.equal(await host.request(), true);
  assert.equal(host.prompts.length, 2);
});

test('declaring one device does not prompt for the other device', async () => {
  const host = setup(async () => ({ response: 1 }));
  const url = GUEST + '/audio';
  host.embed({ url, name: 'workswarm-media-audio', features: ['microphone'] });
  assert.equal(await host.request({ requestingUrl: url, mediaTypes: ['video'] }), false);
  assert.equal(await host.request({ requestingUrl: url }), false);
  assert.equal(host.prompts.length, 0);
  assert.equal(await host.request({ requestingUrl: url, mediaTypes: ['audio'] }), true);
  assert.equal(host.prompts.length, 1);
});

test('another iframe at the same origin cannot borrow the declaration', async () => {
  const host = setup(async () => ({ response: 1 }));
  const url = GUEST + '/no-media';
  const undeclared = host.embed({ url, name: '', features: [] });
  undeclared.name = host.firstFrame.name;
  host.contents.emit('frame-created', {}, { frame: undeclared });
  assert.equal(await host.request({ requestingUrl: url }), false);
  assert.equal(host.prompts.length, 0);
  host.firstFrame.name = 'changed-by-guest';
  assert.equal(await host.request(), true);
});

test('an ambiguous address only prompts when every candidate delegates the requested devices', async () => {
  const host = setup(async () => ({ response: 1 }));
  host.embed({ name: 'workswarm-media-audio-only', features: ['microphone'] });
  assert.equal(await host.request(), false);
  assert.equal(host.prompts.length, 0);
  assert.equal(await host.request({ mediaTypes: ['audio'] }), true);
  assert.equal(host.prompts.length, 1);
});

test('same-origin nested pages use the declaration of their owning iframe', async () => {
  const host = setup(async () => ({ response: 1 }));
  const url = GUEST + '/nested';
  host.embed({ url, name: '', parent: host.firstFrame });
  assert.equal(await host.request({ requestingUrl: url }), true);
});

test('canceling the localized microphone prompt rejects the request', async () => {
  const host = setup(async options => ({ response: options.cancelId }), 'zh-CN');
  assert.equal(await host.request({ mediaTypes: ['audio'] }), false);
  assert.equal(host.prompts[0].message, '允许此网页使用麦克风吗？');
  assert.deepEqual(host.prompts[0].buttons, ['拒绝', '允许本次']);
});

test('unknown origins and device types do not get a desktop prompt', async () => {
  const host = setup(() => assert.fail('unexpected prompt'));
  assert.equal(await host.request({ securityOrigin: 'null' }), false);
  assert.equal(await host.request({ mediaTypes: [] }), false);
  assert.equal(await host.request({ mediaTypes: ['unknown'] }), false);
  assert.equal(host.prompts.length, 0);
});

test('first-party media and unrelated permission paths keep their default policy', async () => {
  const host = setup(() => assert.fail('unexpected prompt'));
  assert.equal(await host.request({ securityOrigin: HOST, isMainFrame: true }), true);
  assert.equal(await host.request({ securityOrigin: HOST }), true);
  assert.equal(await host.request({ securityOrigin: HOST, mediaTypes: [] }), true);
  assert.equal(host.check({ securityOrigin: HOST }), true);
  assert.equal(await host.request({}, 'media', {}), true);
  assert.equal(await host.request({}, 'fullscreen'), true);
  assert.equal(host.check({}, 'deprecated-sync-clipboard-read'), false);
  assert.equal(host.prompts.length, 0);
});

test('one prompt is active at a time and navigation cancels the outstanding approval', async () => {
  const decision = Promise.withResolvers();
  const host = setup(() => decision.promise);
  const first = host.request();
  await nextTurn();
  assert.equal(await host.request(), false);
  assert.equal(host.prompts.length, 1);

  host.contents.emit('did-start-navigation', { frame: { origin: GUEST }, isSameDocument: true });
  assert.equal(host.prompts[0].signal.aborted, false);
  host.contents.emit('did-start-navigation', { frame: { origin: GUEST }, isSameDocument: false });
  assert.equal(host.prompts[0].signal.aborted, true);

  decision.resolve({ response: 1 });
  assert.equal(await first, false);
});

test('both Electron packagers copy the media permission module', () => {
  for (const script of ['build-electron-exe.ps1', 'build-electron-exe.sh']) {
    const source = fs.readFileSync(path.join(__dirname, '../../scripts', script), 'utf8');
    assert.match(source, /(?:Copy-Item|cp).*media_device_permissions\.cjs/);
  }
});
