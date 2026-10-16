const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const { registerDisplayMediaHandler, orderSources, pickerHtml } = require('../../jiuwenswarm/channels/desktop/electron/display_media_picker.cjs');

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

function register(platform, getSources) {
  let handler;
  let options;
  const session = { setDisplayMediaRequestHandler: (next, opts) => { handler = next; options = opts; } };
  registerDisplayMediaHandler({ session, desktopCapturer: { getSources }, BrowserWindow: null, getParentWindow: () => null, platform });
  return { request: (request) => new Promise(resolve => handler(request, resolve)), options };
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
