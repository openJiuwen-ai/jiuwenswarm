const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const { EventEmitter } = require('node:events');
const path = require('node:path');
const { registerDisplayMediaHandler, orderSources, pickerHtml, PICKER_PROTOCOL } = require('../../jiuwenswarm/channels/desktop/electron/display_media_picker.cjs');

test('both Electron packagers include the display media picker', () => {
  for (const script of ['build-electron-exe.ps1', 'build-electron-exe.sh']) {
    const source = fs.readFileSync(path.join(__dirname, '../../scripts', script), 'utf8');
    assert.match(source, /(?:Copy-Item|cp).*display_media_picker\.cjs/);
  }
});

test('screens are listed before windows and names are escaped', () => {
  const sources = orderSources([{ id: 'window:1:0', name: '<b>x</b>' }, { id: 'screen:1:0', name: 'Screen' }]);
  assert.deepEqual(sources.map(source => source.id), ['screen:1:0', 'window:1:0']);
  const html = pickerHtml(sources);
  assert.ok(html.includes('&lt;b&gt;x&lt;/b&gt;'));
  assert.ok(!html.includes('<b>x</b>'));
});

function register(platform, getSources, BrowserWindow = null) {
  const origin = 'http://127.0.0.1:5173';
  const mainFrame = { url: origin + '/', detached: false };
  mainFrame.top = mainFrame;
  const parent = { isDestroyed: () => false, webContents: { mainFrame } };
  let handler;
  let options;
  let callbacks = 0;
  const session = { setDisplayMediaRequestHandler: (next, opts) => { handler = next; options = opts; } };
  registerDisplayMediaHandler({ session, desktopCapturer: { getSources }, BrowserWindow, getParentWindow: () => parent, getFrontendUrl: () => origin, platform });
  return {
    request: (request) => new Promise(resolve => handler({ frame: mainFrame, securityOrigin: origin, ...request }, value => { callbacks += 1; resolve(value); })),
    options,
    mainFrame,
    parent,
    get callbackCount() { return callbacks; },
  };
}

test('all platforms disable the system picker that bypasses the origin guard', () => {
  assert.equal(register('darwin', async () => []).options.useSystemPicker, false);
  assert.equal(register('win32', async () => []).options.useSystemPicker, false);
});

test('requests without video, with no sources, or with capture errors are denied', async () => {
  assert.equal(await register('win32', async () => []).request({ videoRequested: false }), null);
  assert.equal(await register('win32', async () => []).request({ videoRequested: true }), null);
  assert.equal(await register('win32', async () => { throw new Error('no permission'); }).request({ videoRequested: true }), null);
});


for (const [platform, securityOrigin] of ['win32', 'darwin', 'linux'].flatMap(targetPlatform => [
  [targetPlatform, 'http://127.0.0.1:5173'],
  [targetPlatform, 'http://127.0.0.1:5173/'],
])) {
  test(`${platform} fallback picker grants the selected screen exactly once for ${securityOrigin}`, async () => {
    const screen = { id: 'screen:1:0', name: 'Screen' };
    const window = { id: 'window:1:0', name: 'Window' };
    let picker;
    class PickerWindow extends EventEmitter {
      constructor() {
        super();
        picker = this;
        this.webContents = new EventEmitter();
        this.destroyed = false;
      }
      isDestroyed() { return this.destroyed; }
      destroy() { this.destroyed = true; this.emit('closed'); }
      show() {}
      async loadURL() {
        queueMicrotask(() => this.webContents.emit(
          'will-navigate', { preventDefault() {} }, `${PICKER_PROTOCOL}//select?index=0`,
        ));
      }
    }
    const registered = register(platform, async () => [window, screen], PickerWindow);
    const result = await registered.request({ videoRequested: true, securityOrigin });
    assert.deepEqual(result, { video: screen });
    assert.equal(picker.destroyed, true);
    assert.equal(registered.callbackCount, 1);
    assert.equal(registered.options.useSystemPicker, false);
  });
}


for (const platform of ['win32', 'darwin', 'linux']) {
  test(`${platform} denies untrusted frames before enumerating sources`, async () => {
    let enumerated = 0;
    const registered = register(platform, async () => { enumerated += 1; return []; });
    const ownFrame = registered.mainFrame;
    const externalFrame = { url: 'https://login.example.com/', detached: false };
    externalFrame.top = externalFrame;
    const sameOriginPopup = { url: ownFrame.url, detached: false };
    sameOriginPopup.top = sameOriginPopup;
    for (const frame of [
      null,
      externalFrame,
      sameOriginPopup,
      { url: 'http://127.0.0.1:5174/', top: ownFrame },
      { url: 'https://127.0.0.1:5173/', top: ownFrame },
      { url: 'http://localhost:5173/', top: ownFrame },
      { url: 'https://login.example.com/', top: ownFrame },
      { url: ownFrame.url, top: ownFrame, detached: true },
    ]) {
      assert.equal(await registered.request({ videoRequested: true, frame }), null);
    }
    for (const securityOrigin of [
      'null', '', 'invalid-url', 'http://127.0.0.1:5174/',
      'https://127.0.0.1:5173/', 'http://localhost:5173/', 'https://login.example.com/',
    ]) {
      assert.equal(await registered.request({ videoRequested: true, securityOrigin }), null);
    }
    registered.parent.isDestroyed = () => true;
    assert.equal(await registered.request({ videoRequested: true }), null);
    assert.equal(enumerated, 0);
  });
}

test('same-origin child frame in the main window can request the picker', async () => {
  let enumerated = 0;
  const registered = register('darwin', async () => { enumerated += 1; return []; });
  const frame = { url: registered.mainFrame.url + 'child', top: registered.mainFrame };
  assert.equal(await registered.request({ videoRequested: true, frame }), null);
  assert.equal(enumerated, 1);
});

test('navigation during enumeration denies the request without opening a picker', async () => {
  const registered = register('darwin', async () => {
    registered.mainFrame.url = 'https://login.example.com/';
    return [{ id: 'screen:1:0', name: 'Screen' }];
  });
  assert.equal(await registered.request({ videoRequested: true }), null);
  assert.equal(registered.callbackCount, 1);
});

test('navigation while the picker is open never grants the selected stream', async () => {
  class PickerWindow extends EventEmitter {
    constructor() { super(); this.webContents = new EventEmitter(); this.destroyed = false; }
    isDestroyed() { return this.destroyed; }
    destroy() { this.destroyed = true; this.emit('closed'); }
    show() {}
    async loadURL() {
      registered.mainFrame.url = 'http://127.0.0.1:5174/';
      queueMicrotask(() => this.webContents.emit(
        'will-navigate', { preventDefault() {} }, `${PICKER_PROTOCOL}//select?index=0`,
      ));
    }
  }
  const registered = register('darwin', async () => [{ id: 'screen:1:0', name: 'Screen' }], PickerWindow);
  assert.equal(await registered.request({ videoRequested: true }), null);
  assert.equal(registered.callbackCount, 1);
});
