// Run via run_share_image_export_integration.cjs after building the frontend.
// Uses production IPC, preload, runner and encoder, with local synthetic history.
const assert = require('node:assert/strict');
const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');

const repo = path.resolve(__dirname, '../..');
const desktop = path.join(repo, 'jiuwenswarm/channels/desktop/electron');
const dist = path.join(repo, 'jiuwenswarm/channels/web/frontend/dist');
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'swarm-export-integration-'));
app.setPath('userData', path.join(scratch, 'profile'));
app.setPath('downloads', scratch);

const mainPath = path.join(desktop, 'main.cjs');
const source = fs.readFileSync(mainPath, 'utf8');
const bootstrap = source.indexOf('if (!app.requestSingleInstanceLock())');
assert.ok(bootstrap > 0, 'Electron bootstrap boundary changed');
const production = new Module(mainPath, module);
production.filename = mainPath;
production.paths = Module._nodeModulePaths(desktop);
production._compile(`${source.slice(0, bootstrap)}
module.exports = { init(window, url) { mainWindow = window; currentFrontendUrl = url; registerIpcHandlers(); } };`, mainPath);

const shortRecords = [
  { role: 'user', timestamp: '2026-10-10T00:00:00Z', content: 'Export a table, formula and diagram.' },
  { role: 'assistant', timestamp: '2026-10-10T00:00:01Z', content: '## Export verification\n\n| Path | Engine |\n| --- | --- |\n| Desktop | Chromium |\n\n$E = mc^2$\n\n```mermaid\ngraph LR\n A[Snapshot] --> B[PNG]\n```' },
];
const longRecords = Array.from({ length: 90 }, (_, index) => ({
  role: 'assistant', timestamp: new Date(Date.UTC(2026, 9, 10, 0, 0, index)).toISOString(), content: `### Section ${index}\n\n\`\`\`text\n`
    + Array.from({ length: 30 }, (_, line) => `Long image verification line ${line}`).join('\n') + '\n```',
}));
const requests = [];
const mimeTypes = { '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.html': 'text/html' };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  requests.push(url.pathname);
  if (url.pathname.startsWith('/share-api/')) {
    res.setHeader('content-type', 'application/json');
    const id = url.searchParams.get('session_id');
    if (!String(req.headers.cookie || '').includes('export-auth=synthetic')) {
      res.writeHead(403); res.end('{}'); return;
    }
    if (url.pathname !== '/share-api/snapshot' || !['short', 'long'].includes(id)) {
      res.writeHead(404); res.end('{}'); return;
    }
    res.end(JSON.stringify({ filename: `export-${id}.png`, snapshot: {
      session_id: id, metadata: { title: 'Export integration fixture', exported_at: '2026-10-10T00:02:00Z' },
      records: id === 'long' ? longRecords : shortRecords,
    } }));
    return;
  }
  if (url.pathname === '/chat-fixture') {
    res.setHeader('content-type', 'text/html');
    res.end(`<script>window.ticks=0;window.gaps=[];let last=performance.now();
      setInterval(()=>{const now=performance.now();window.gaps.push(now-last);last=now;window.ticks++},10);</script>`);
    return;
  }
  const relative = url.pathname === '/share-export-runner' ? 'index.html' : decodeURIComponent(url.pathname).slice(1);
  const target = path.resolve(dist, relative);
  if (!target.startsWith(dist + path.sep) || !fs.existsSync(target) || !fs.statSync(target).isFile()) {
    res.writeHead(404); res.end(); return;
  }
  res.setHeader('content-type', mimeTypes[path.extname(target)] || 'application/octet-stream');
  fs.createReadStream(target).pipe(res);
});

app.whenReady().then(async () => {
  const rendererRecords = [];
  const exportSessions = new Set();
  let main;
  let exportWindows = 0;
  app.on('browser-window-created', (_event, window) => {
    if (!main) return;
    exportWindows++;
    exportSessions.add(window.webContents.session);
    window.webContents.once('did-finish-load', () => {
      rendererRecords.push({ pid: window.webContents.getOSProcessId(), hidden: !window.isVisible() });
    });
  });
  try {
    assert.ok(fs.existsSync(path.join(dist, 'index.html')), 'Build the frontend first');
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const base = `http://127.0.0.1:${server.address().port}`;
    main = new BrowserWindow({ show: false, webPreferences: {
      preload: path.join(desktop, 'preload.cjs'), sandbox: true, contextIsolation: true,
      nodeIntegration: false, backgroundThrottling: false,
    } });
    production.exports.init(main, base);
    await main.loadURL(base + '/chat-fixture');
    const mainPid = main.webContents.getOSProcessId();
    const run = id => main.webContents.executeJavaScript(`window.jiuwenDesktop.exportShareImage(${JSON.stringify(id)}, 'en')`);
    const error = id => main.webContents.executeJavaScript(`window.jiuwenDesktop.exportShareImage(${JSON.stringify(id)}, 'zh').then(() => null, error => error.message)`);
    assert.match(await error('short'), /share_export_snapshot_http_403/);
    await main.webContents.session.cookies.set({ url: base, name: 'export-auth', value: 'synthetic', httpOnly: true, sameSite: 'lax' });
    assert.equal(await run('short'), true);
    assert.match(await error('missing'), /share_export_snapshot_http_404/);
    const start = Date.now();
    const before = await main.webContents.executeJavaScript('window.ticks');
    assert.equal(await run('long'), true);
    const after = await main.webContents.executeJavaScript('window.ticks');
    const gaps = await main.webContents.executeJavaScript('window.gaps');
    assert.ok(after - before > 20, 'Chat renderer must remain active during long export');
    assert.ok(rendererRecords.every(record => record.pid !== mainPid && record.hidden));
    assert.deepEqual(BrowserWindow.getAllWindows(), [main], 'Export windows must be destroyed');
    for (const session of exportSessions) {
      assert.notEqual(session, main.webContents.session);
      assert.deepEqual(await session.cookies.get({}), [], 'Temporary export credentials must be cleared');
    }
    assert.ok(!requests.includes('/share-api/jobs'), 'Electron must not start server-side Playwright jobs');
    const png = fs.readFileSync(path.join(scratch, 'export-short.png'));
    assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
    assert.equal(png.readUInt32BE(16), 2250);
    const zip = fs.readFileSync(path.join(scratch, 'export-long.zip'));
    assert.equal(zip.subarray(0, 4).toString('hex'), '504b0304');
    assert.ok(!fs.readdirSync(scratch).some(name => name.endsWith('.part')));
    console.log(JSON.stringify({ result: 'passed', electron: process.versions.electron, output: scratch,
      mainPid, exportPids: rendererRecords.map(record => record.pid), exportWindows,
      longExportMs: Date.now() - start, chatTicksDuringExport: after - before,
      maximumChatTimerGapMs: Math.max(...gaps), pngWidth: 2250, pngBytes: png.length, zipBytes: zip.length }));
    app.exit(0);
  } catch (error) {
    console.error(error);
    app.exit(1);
  } finally {
    server.close();
  }
});
