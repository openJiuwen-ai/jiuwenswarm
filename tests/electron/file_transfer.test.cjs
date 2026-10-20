const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const fsSync = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { randomUUID } = require('node:crypto');
const { Readable } = require('node:stream');
const { pipeline } = require('node:stream/promises');

const source = fsSync.readFileSync(path.join(__dirname, '../../jiuwenswarm/channels/desktop/electron/main.cjs'), 'utf8');
const helpers = source.slice(source.indexOf('function sanitizeFilename('), source.indexOf('// ─── Blob'));

async function fixture(t, response = () => new Response('complete')) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'electron-transfer-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const target = path.join(root, 'chosen.txt');
  const calls = { fetched: [], revealed: [], dialogs: [] };
  let choice = { canceled: false, filePath: target };
  const context = vm.createContext({
    fs, path, randomUUID, Readable, pipeline, URL, Date, console, Buffer, decodeURIComponent,
    process: { env: { JIUWENSWARM_DATA_DIR: root } },
    app: { getPath: () => root }, mainWindow: {}, currentFrontendUrl: 'http://127.0.0.1:5173',
    net: { fetch: async url => { calls.fetched.push(url.toString()); return response(); } },
    dialog: { showSaveDialog: async (_window, options) => { calls.dialogs.push(options); return choice; } },
    shell: { showItemInFolder: file => calls.revealed.push(file) },
  });
  vm.runInContext(helpers, context);
  return { root, target, calls, save: context.saveDownloadedFile, cleanup: context.cleanupStaleUpdateDownloads,
    choose: next => { choice = next; } };
}

test('download uses the chosen path and atomically replaces an existing file', async t => {
  const f = await fixture(t);
  await fs.writeFile(f.target, 'original');
  const result = await f.save('/artifact/1', 'suggested.txt');
  assert.equal(result.ok, true);
  assert.equal(await fs.readFile(f.target, 'utf8'), 'complete');
  assert.equal(f.calls.dialogs[0].defaultPath, path.join(f.root, 'suggested.txt'));
  assert.deepEqual(f.calls.fetched, ['http://127.0.0.1:5173/artifact/1']);
  assert.deepEqual(f.calls.revealed, [f.target]);
  assert.deepEqual(await fs.readdir(f.root), ['chosen.txt']);
});

test('cancel does not fetch, write, or reveal anything', async t => {
  const f = await fixture(t);
  f.choose({ canceled: true });
  const result = await f.save('/artifact/1', 'file.txt');
  assert.equal(result.ok, false);
  assert.equal(result.cancelled, true);
  assert.deepEqual(f.calls.fetched, []);
  assert.deepEqual(f.calls.revealed, []);
  assert.deepEqual(await fs.readdir(f.root), []);
});

test('interrupted stream removes partial output and preserves the existing target', async t => {
  const f = await fixture(t, () => ({ ok: true, body: Readable.toWeb(Readable.from((async function* () {
    yield Buffer.from('partial');
    throw new Error('connection interrupted');
  })())) }));
  await fs.writeFile(f.target, 'original');
  await assert.rejects(f.save('/artifact', 'file.txt'), /connection interrupted/);
  assert.equal(await fs.readFile(f.target, 'utf8'), 'original');
  assert.deepEqual(await fs.readdir(f.root), ['chosen.txt']);
  assert.deepEqual(f.calls.revealed, []);
});

test('HTTP failure leaves no output and local protocols are rejected before the dialog', async t => {
  const f = await fixture(t, () => new Response('', { status: 503 }));
  await assert.rejects(f.save('file:///secret.txt', 'file.txt'), /Unsupported download protocol/);
  assert.equal(f.calls.dialogs.length, 0);
  await assert.rejects(f.save('/artifact', 'file.txt'), /HTTP 503/);
  assert.deepEqual(await fs.readdir(f.root), []);
});

test('file-preview text downloads can save FileReader data URLs without net.fetch', async t => {
  const f = await fixture(t);
  const payload = Buffer.from('{"name":"demo"}', 'utf8').toString('base64');
  const result = await f.save(`data:text/plain;charset=utf-8;base64,${payload}`, 'manifest.json');
  assert.equal(result.ok, true);
  assert.equal(await fs.readFile(f.target, 'utf8'), '{"name":"demo"}');
  assert.deepEqual(f.calls.fetched, []);
  assert.deepEqual(f.calls.revealed, [f.target]);
});

test('data URL write failure removes the leftover .part and preserves the target', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'electron-transfer-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const target = path.join(root, 'chosen.txt');
  await fs.writeFile(target, 'original');
  const calls = { fetched: [], revealed: [], dialogs: [] };
  const realOpen = fs.open.bind(fs);
  const context = vm.createContext({
    fs: {
      ...fs,
      open: async (filePath, flags) => {
        const handle = await realOpen(filePath, flags);
        if (!String(filePath).endsWith('.part')) return handle;
        return {
          writeFile: async () => { throw new Error('ENOSPC'); },
          createWriteStream: handle.createWriteStream.bind(handle),
          close: () => handle.close(),
        };
      },
    },
    path, randomUUID, Readable, pipeline, URL, Date, console, Buffer, decodeURIComponent,
    process: { env: { JIUWENSWARM_DATA_DIR: root } },
    app: { getPath: () => root }, mainWindow: {}, currentFrontendUrl: 'http://127.0.0.1:5173',
    net: { fetch: async url => { calls.fetched.push(url.toString()); return new Response('complete'); } },
    dialog: {
      showSaveDialog: async (_window, options) => {
        calls.dialogs.push(options);
        return { canceled: false, filePath: target };
      },
    },
    shell: { showItemInFolder: file => calls.revealed.push(file) },
  });
  vm.runInContext(helpers, context);
  const payload = Buffer.from('{"name":"demo"}', 'utf8').toString('base64');
  await assert.rejects(
    context.saveDownloadedFile(`data:text/plain;base64,${payload}`, 'manifest.json'),
    /ENOSPC/,
  );
  assert.equal(await fs.readFile(target, 'utf8'), 'original');
  assert.deepEqual(await fs.readdir(root), ['chosen.txt']);
  assert.deepEqual(calls.revealed, []);
});

test('rename failure removes only this download temporary file', async t => {
  const f = await fixture(t);
  await fs.mkdir(f.target);
  await assert.rejects(f.save('/artifact', 'file.txt'));
  assert.equal((await fs.stat(f.target)).isDirectory(), true);
  assert.deepEqual(await fs.readdir(f.root), ['chosen.txt']);
});

test('startup cleanup removes only old installer fragments', async t => {
  const f = await fixture(t);
  await f.cleanup(); // Missing directory must not prevent startup.
  const updates = path.join(f.root, '.updates');
  await fs.mkdir(updates);
  const old = new Date(Date.now() - 48 * 60 * 60 * 1000);
  for (const name of ['old.exe.part', 'old.dmg.part', 'old.tar.gz.part', 'complete.exe', 'notes.part', '_install_helper.sh']) {
    const file = path.join(updates, name);
    await fs.writeFile(file, name);
    await fs.utimes(file, old, old);
  }
  await fs.writeFile(path.join(updates, 'active.exe.part'), 'in progress');
  await fs.mkdir(path.join(updates, 'directory.exe.part'));
  await fs.utimes(path.join(updates, 'directory.exe.part'), old, old);
  await f.cleanup();
  assert.deepEqual((await fs.readdir(updates)).sort(),
    ['_install_helper.sh', 'active.exe.part', 'complete.exe', 'directory.exe.part', 'notes.part']);
  assert.ok(source.indexOf('await cleanupStaleUpdateDownloads();') < source.indexOf('await createMainWindow();'));
});

test('preload resolves native File objects before IPC and rejects virtual files', async () => {
  const apis = {};
  const calls = [];
  const preload = fsSync.readFileSync(path.join(__dirname, '../../jiuwenswarm/channels/desktop/electron/preload.cjs'), 'utf8');
  vm.runInNewContext(preload, { require: () => ({
    contextBridge: { exposeInMainWorld: (name, value) => { apis[name] = value; } },
    ipcRenderer: { sendSync: () => false, invoke: async (...args) => { calls.push(args); return ['described']; } },
    webUtils: { getPathForFile: file => file.nativePath || '' },
  }) });
  assert.equal((await apis.jiuwenDesktop.describeDroppedFiles([{ nativePath: '/tmp/a.zip' }, { nativePath: '/tmp/a.zip' }]))[0], 'described');
  assert.equal(calls[0][0], 'desktop:describe-local-files');
  assert.deepEqual(Array.from(calls[0][1]), ['/tmp/a.zip']);
  assert.equal((await apis.jiuwenDesktop.describeDroppedFiles([{}])).length, 0);
  assert.equal(calls.length, 1);
});
