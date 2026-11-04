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
  let handler;
  let options;
  let callbacks = 0;
  const session = { setDisplayMediaRequestHandler: (next, opts) => { handler = next; options = opts; } };
  registerDisplayMediaHandler({ session, desktopCapturer: { getSources }, BrowserWindow, getParentWindow: () => null, platform });
  return {
    request: (request) => new Promise(resolve => handler(request, value => { callbacks += 1; resolve(value); })),
    options,
    get callbackCount() { return callbacks; },
  };
}

test('macOS uses the system picker; other platforms use the in-app picker', () => {
  assert.equal(register('darwin', async () => []).options.useSystemPicker, true);
  assert.equal(register('win32', async () => []).options.useSystemPicker, false);
});

test('requests without video, with no sources, or with capture errors are denied', async () => {
  assert.equal(await register('win32', async () => []).request({ videoRequested: false }), null);
  assert.equal(await register('win32', async () => []).request({ videoRequested: true }), null);
  assert.equal(await register('win32', async () => { throw new Error('no permission'); }).request({ videoRequested: true }), null);
});


for (const platform of ['win32', 'darwin']) {
  test(`${platform} fallback picker grants the selected screen exactly once`, async () => {
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
    const result = await registered.request({ videoRequested: true });
    assert.deepEqual(result, { video: screen });
    assert.equal(picker.destroyed, true);
    assert.equal(registered.callbackCount, 1);
    assert.equal(registered.options.useSystemPicker, platform === 'darwin');
  });
}

