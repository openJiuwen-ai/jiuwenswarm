const assert = require('node:assert/strict');
const { test } = require('node:test');
const { EventEmitter } = require('node:events');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { createShareImageExporter } = require('../../jiuwenswarm/channels/desktop/electron/share_image_export.cjs');

async function fixture(t, { rendererError, downloadResult = 'completed', cookieGate } = {}) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'share-image-test-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const windows = [];
  const cookies = [];
  const revealed = [];
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
          return rendererError ? { status: 'error', error: rendererError }
            : { status: 'ready', filename: 'share.png' };
        }
        const item = new EventEmitter();
        item.getFilename = () => 'share.png';
        item.setSavePath = value => { item.savePath = value; };
        item.cancel = () => { throw new Error('Completed downloads must not be cancelled'); };
        this.webContents.session.emit('will-download', {}, item, this.webContents);
        await fs.writeFile(item.savePath, 'exported-image');
        item.emit('done', {}, downloadResult);
      };
      windows.push(this);
    }
    async loadURL(url) { this.url = url; }
    isDestroyed() { return this.destroyed; }
    destroy() { this.destroyed = true; }
  }
  const exportImage = createShareImageExporter({
    BrowserWindow, getFrontendUrl: () => 'http://127.0.0.1:6173',
    getCookies: async () => {
      if (cookieGate) await cookieGate;
      return [{ name: '__wsdt6173', value: 'auth', httpOnly: true, secure: false, sameSite: 'lax' }];
    },
    uniqueDownloadPath: async filename => path.join(directory, filename),
    showItemInFolder: filename => revealed.push(filename),
  });
  return { exportImage, windows, cookies, revealed, directory, storageCleanups: () => storageCleanups };
}

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
  assert.equal(await fs.readFile(f.revealed[0], 'utf8'), 'exported-image');
  assert.deepEqual(await fs.readdir(f.directory), ['share.png']);
});

test('different sessions wait for the current export to release its renderer', async t => {
  let release;
  const cookieGate = new Promise(resolve => { release = resolve; });
  const f = await fixture(t, { cookieGate });
  const first = f.exportImage('one', 'zh');
  const second = f.exportImage('two', 'en');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.windows.length, 1);
  release();
  await Promise.all([first, second]);
  assert.equal(f.windows.length, 2);
  assert.equal(f.storageCleanups(), 2);
  assert.ok(f.windows.every(window => window.isDestroyed()));
});

test('failed renderer releases resources and allows a queued export', async t => {
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
});
