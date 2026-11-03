const assert = require('node:assert/strict');
const { test } = require('node:test');
const { EventEmitter } = require('node:events');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { createShareImageExporter } = require('../../jiuwenswarm/channels/desktop/electron/share_image_export.cjs');

async function fixture(t, { rendererError, rendererStates, downloadResult = 'completed', cookieGate, downloadGate, onDownloadStarted, onStateRead } = {}) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'share-image-test-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const windows = [];
  const cookies = [];
  const revealed = [];
  const progress = [];
  let storageCleanups = 0;
  class BrowserWindow {
    constructor(options) {
      this.options = options;
      this.destroyed = false;
      this.webContents = new EventEmitter();
      this.webContents.session = new EventEmitter();
      this.webContents.session.cookies = { set: async cookie => cookies.push(cookie) };
      this.webContents.session.clearStorageData = async () => { storageCleanups++; };
      this.webContents.setWindowOpenHandler = handler => { this.openHandler = handler; };
      this.webContents.isDestroyed = () => this.destroyed;
      this.webContents.executeJavaScript = async script => {
        if (script.includes('__SHARE_IMAGE_EXPORT_STATE')) {
          onStateRead?.();
          if (rendererStates?.length) {
            const state = rendererStates.shift();
            return typeof state === 'function' ? state(this) : state;
          }
          return rendererError ? { status: 'error', error: rendererError }
            : { status: 'ready', filename: 'share.png' };
        }
        const item = new EventEmitter();
        item.getFilename = () => 'share.png';
        item.setSavePath = value => { item.savePath = value; };
        item.cancel = () => { throw new Error('Completed downloads must not be cancelled'); };
        this.webContents.session.emit('will-download', {}, item, this.webContents);
        onDownloadStarted?.();
        if (downloadGate) await downloadGate;
        await fs.writeFile(item.savePath, new URL(this.url).searchParams.get('session_id'));
        item.emit('done', {}, downloadResult);
      };
      windows.push(this);
    }
    async loadURL(url) { this.url = url; }
    isDestroyed() { return this.destroyed; }
    destroy() { this.destroyed = true; this.webContents.emit('destroyed'); }
  }
  const exportImage = createShareImageExporter({
    BrowserWindow, getFrontendUrl: () => 'http://127.0.0.1:6173',
    getCookies: async () => {
      if (cookieGate) await cookieGate;
      return [{ name: '__wsdt6173', value: 'auth', httpOnly: true, secure: false, sameSite: 'lax' }];
    },
    getPreferredDownloadPath: filename => path.join(directory, filename),
    showItemInFolder: filename => revealed.push(filename),
    onProgress: value => progress.push(value),
  });
  return { exportImage, windows, cookies, revealed, progress, directory, storageCleanups: () => storageCleanups };
}

test('desktop progress reports measured image generation and saving for the owning session', async t => {
  const f = await fixture(t, { rendererStates: [
    { status: 'rendering', progress: 18 },
    { status: 'rendering', progress: 18 },
    { status: 'rendering', progress: 72 },
  ] });
  assert.equal(await f.exportImage('one', 'zh'), true);
  assert.deepEqual(f.progress, [
    { sessionId: 'one', phase: 'loading_snapshot', progress: null },
    { sessionId: 'one', phase: 'rendering', progress: 18 },
    { sessionId: 'one', phase: 'rendering', progress: 72 },
    { sessionId: 'one', phase: 'saving', progress: 100 },
  ]);
});

test('desktop export deduplicates requests and saves outside the chat renderer', async t => {
  const f = await fixture(t);
  const first = f.exportImage('session / one', 'zh');
  assert.equal(f.exportImage('session / one', 'zh'), first);
  assert.equal(await first, true);
  assert.equal(f.windows.length, 1);
  const window = f.windows[0];
  assert.equal(window.options.show, false);
  assert.match(window.options.webPreferences.partition, /^share-image-export-/);
  assert.equal(window.options.webPreferences.backgroundThrottling, false);
  assert.equal(window.options.webPreferences.nodeIntegration, false);
  assert.equal(window.openHandler().action, 'deny');
  assert.equal(new URL(window.url).searchParams.get('session_id'), 'session / one');
  assert.equal(new URL(window.url).searchParams.get('locale'), 'zh');
  assert.equal(f.cookies[0].name, '__wsdt6173');
  assert.equal(window.isDestroyed(), true);
  assert.equal(f.storageCleanups(), 1);
  assert.equal(await fs.readFile(f.revealed[0], 'utf8'), 'session / one');
  assert.deepEqual(await fs.readdir(f.directory), ['share.png']);
});

test('different sessions export concurrently in independent partitions', async t => {
  let release;
  const cookieGate = new Promise(resolve => { release = resolve; });
  const f = await fixture(t, { cookieGate });
  const first = f.exportImage('one', 'zh');
  const second = f.exportImage('two', 'en');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.windows.length, 2);
  assert.notEqual(f.windows[0].options.webPreferences.partition, f.windows[1].options.webPreferences.partition);
  release();
  await Promise.all([first, second]);
  assert.equal(f.windows.length, 2);
  assert.equal(f.storageCleanups(), 2);
  assert.ok(f.windows.every(window => window.isDestroyed()));
  assert.equal(f.revealed.length, 2);
  assert.equal(new Set(f.revealed).size, 2);
  const content = await Promise.all(f.revealed.map(filename => fs.readFile(filename, 'utf8')));
  assert.deepEqual(content.sort(), ['one', 'two']);
  assert.equal((await fs.readdir(f.directory)).length, 2);
});

test('desktop export reserves the next filename without replacing existing downloads', async t => {
  const f = await fixture(t);
  await fs.writeFile(path.join(f.directory, 'share.png'), 'existing original');
  await fs.writeFile(path.join(f.directory, 'share (1).png'), 'existing numbered');
  assert.equal(await f.exportImage('new session', 'zh'), true);
  assert.deepEqual(await fs.readdir(f.directory), ['share (1).png', 'share (2).png', 'share.png']);
  assert.equal(await fs.readFile(path.join(f.directory, 'share.png'), 'utf8'), 'existing original');
  assert.equal(await fs.readFile(path.join(f.directory, 'share (1).png'), 'utf8'), 'existing numbered');
  assert.equal(await fs.readFile(f.revealed[0], 'utf8'), 'new session');
});

test('failed renderer releases resources and allows a later session export', async t => {
  const f = await fixture(t, { rendererError: 'share_export_snapshot_http_403' });
  await assert.rejects(f.exportImage('one', 'en'), /share_export_snapshot_http_403/);
  assert.equal(f.windows[0].isDestroyed(), true);
  assert.deepEqual(f.revealed, []);
  await assert.rejects(f.exportImage('two', 'en'), /share_export_snapshot_http_403/);
  assert.equal(f.windows.length, 2);
  assert.equal(f.storageCleanups(), 2);
});

test('interrupted download removes temporary output and releases its renderer', async t => {
  const f = await fixture(t, { downloadResult: 'interrupted' });
  await assert.rejects(f.exportImage('session', 'zh'), /share_export_download_interrupted/);
  assert.deepEqual(await fs.readdir(f.directory), []);
  assert.deepEqual(f.revealed, []);
  assert.equal(f.windows[0].isDestroyed(), true);
  assert.equal(f.storageCleanups(), 1);
});

test('generation remains unlimited after initialization even without a changing heartbeat', async t => {
  let now = 0;
  t.mock.method(Date, 'now', () => now);
  const f = await fixture(t, {
    rendererStates: [
      { status: 'rendering', heartbeat: 0 },
      { status: 'rendering', heartbeat: 0 },
      { status: 'ready', filename: 'share.png' },
    ],
    onStateRead: () => { now += 86_400_000; },
  });
  assert.equal(await f.exportImage('large', 'zh'), true);
  assert.equal(await fs.readFile(f.revealed[0], 'utf8'), 'large');
  assert.equal(f.storageCleanups(), 1);
});

test('a started download can save for longer than the download-start window', async t => {
  let releaseDownload;
  let resolveStarted;
  const downloadGate = new Promise(resolve => { releaseDownload = resolve; });
  const downloadStarted = new Promise(resolve => { resolveStarted = resolve; });
  const f = await fixture(t, { downloadGate, onDownloadStarted: resolveStarted });
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let settled = false;
  const task = f.exportImage('large', 'zh');
  task.then(() => { settled = true; }, () => { settled = true; });
  await downloadStarted;
  await new Promise(resolve => setImmediate(resolve));
  t.mock.timers.tick(60_001);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(settled, false);
  releaseDownload();
  assert.equal(await task, true);
  assert.equal(await fs.readFile(f.revealed[0], 'utf8'), 'large');
});

for (const [event, error] of [
  ['render-process-gone', 'share_export_renderer_exited'],
  ['destroyed', 'share_export_renderer_closed'],
]) {
  test(`renderer ${event} releases an in-flight generation evaluation`, async t => {
    let resolvePending;
    const pending = new Promise(resolve => { resolvePending = resolve; });
    const f = await fixture(t, { rendererStates: [
      { status: 'rendering' },
      () => { resolvePending(); return new Promise(() => {}); },
    ] });
    const task = f.exportImage('large', 'zh');
    await pending;
    f.windows[0].webContents.emit(event);
    await assert.rejects(task, new RegExp(error));
    assert.equal(f.windows[0].isDestroyed(), true);
    assert.equal(f.storageCleanups(), 1);
    assert.deepEqual(f.revealed, []);
    assert.deepEqual(await fs.readdir(f.directory), []);
  });
}

test('invalid requests do not create a renderer', async t => {
  const f = await fixture(t);
  await assert.rejects(f.exportImage('', 'en'), /invalid_share_export_request/);
  assert.deepEqual(f.windows, []);
});

test('both Electron packagers include the export runtime', async () => {
  for (const script of ['build-electron-exe.ps1', 'build-electron-exe.sh']) {
    const source = await fs.readFile(path.join(__dirname, '../../scripts', script), 'utf8');
    assert.match(source, /(?:Copy-Item|cp).*share_image_export\.cjs/);
  }
  const macOSBuild = await fs.readFile(path.join(__dirname, '../../scripts/build-electron-exe.sh'), 'utf8');
  assert.match(macOSBuild, /REQUIRED_FILES=\([\s\S]*?share_image_export\.cjs/);
});
