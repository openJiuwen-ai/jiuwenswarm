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

let shortRecords = [
  { role: 'user', timestamp: '2026-10-10T00:00:00Z', content: 'Export a table, formula and diagram.' },
  { role: 'assistant', timestamp: '2026-10-10T00:00:01Z', content: '## Export verification\n\n| Path | Engine |\n| --- | --- |\n| Desktop | Chromium |\n\n$E = mc^2$\n\n```mermaid\ngraph LR\n A[Snapshot] --> B[PNG]\n```' },
];
let longRecords = Array.from({ length: 90 }, (_, index) => ({
  role: 'assistant', timestamp: new Date(Date.UTC(2026, 9, 10, 0, 0, index)).toISOString(), content: `### Section ${index}\n\n\`\`\`text\n`
    + Array.from({ length: 30 }, (_, line) => `Long image verification line ${line}`).join('\n') + '\n```',
}));
const realHistoryRoot = process.env.SWARM_TEST_SHARE_IMAGE_REAL_HISTORY_DIR;
const sessionIds = {
  short: realHistoryRoot ? 'web_1a13313fe60_fc346e863e1c' : 'short',
  long: realHistoryRoot ? 'web_1a05a945041_71cf3f398eff' : 'long',
};
let realHistoryBytes = null;
if (realHistoryRoot) {
  const readHistory = id => fs.readFileSync(path.join(realHistoryRoot, id, 'history.jsonl'), 'utf8').trim().split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line));
  longRecords = readHistory('web_1a05a945041_71cf3f398eff');
  shortRecords = readHistory('web_1a13313fe60_fc346e863e1c');
  realHistoryBytes = fs.statSync(path.join(realHistoryRoot, 'web_1a05a945041_71cf3f398eff', 'history.jsonl')).size;
}
const requests = [];
const mimeTypes = { '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.html': 'text/html' };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  requests.push(url.pathname);
  if (url.pathname.startsWith('/share-api/')) {
    res.setHeader('content-type', 'application/json');
    const requestedSessionId = url.searchParams.get('session_id');
    const id = requestedSessionId === sessionIds.long ? 'long' : requestedSessionId === sessionIds.short || requestedSessionId === 'short-peer' ? 'short' : null;
    if (!String(req.headers.cookie || '').includes('export-auth=synthetic')) {
      res.writeHead(403); res.end('{}'); return;
    }
    if (url.pathname !== '/share-api/snapshot' || !['short', 'long'].includes(id)) {
      res.writeHead(404); res.end('{}'); return;
    }
    res.end(JSON.stringify({ filename: `export-${id}.png`, snapshot: {
      session_id: requestedSessionId, metadata: { title: realHistoryRoot && requestedSessionId !== 'short-peer' ? JSON.parse(fs.readFileSync(path.join(realHistoryRoot, requestedSessionId, 'metadata.json'), 'utf8')).title : 'Export integration fixture', exported_at: '2026-10-10T00:02:00Z' },
      records: id === 'long' ? longRecords : requestedSessionId === 'short-peer' ? [...shortRecords, { role: 'assistant', content: 'Distinct concurrent file content.' }] : shortRecords,
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
    await main.webContents.executeJavaScript(`window.exportProgress = [];
      window.stopExportProgress = window.jiuwenDesktop.onShareImageExportProgress(value => window.exportProgress.push(value));
      void 0;`);
    const mainPid = main.webContents.getOSProcessId();
    const run = id => main.webContents.executeJavaScript(`window.jiuwenDesktop.exportShareImage(${JSON.stringify(sessionIds[id] || id)}, 'en')`);
    const error = id => main.webContents.executeJavaScript(`window.jiuwenDesktop.exportShareImage(${JSON.stringify(sessionIds[id] || id)}, 'zh').then(() => null, error => error.message)`);
    assert.match(await error('short'), /share_export_snapshot_http_403/);
    await main.webContents.session.cookies.set({ url: base, name: 'export-auth', value: 'synthetic', httpOnly: true, sameSite: 'lax' });
    assert.equal(await run('short'), true);
    const existingFiles = new Set(fs.readdirSync(scratch));
    assert.deepEqual(await Promise.all([run('short'), run('short-peer')]), [true, true]);
    const concurrentFiles = fs.readdirSync(scratch).filter(name => !existingFiles.has(name) && name.endsWith('.png'));
    assert.equal(concurrentFiles.length, 2, 'Same-name concurrent exports must both survive');
    assert.notDeepEqual(fs.readFileSync(path.join(scratch, concurrentFiles[0])), fs.readFileSync(path.join(scratch, concurrentFiles[1])));
    assert.match(await error('missing'), /share_export_snapshot_http_404/);
    const start = Date.now();
    const before = await main.webContents.executeJavaScript('window.ticks');
    let longFinishedAt;
    let shortFinishedAt;
    const longTask = run('long').then(value => { longFinishedAt = Date.now(); return value; });
    const shortTask = run('short').then(value => { shortFinishedAt = Date.now(); return value; });
    const progressObserver = setInterval(async () => {
      const events = await main.webContents.executeJavaScript('window.exportProgress');
      console.log(JSON.stringify({ elapsedMs: Date.now() - start, progress: events.filter(event => event.sessionId === sessionIds.long).at(-1), shortCompleted: Boolean(shortFinishedAt) }));
    }, 5000);
    try { assert.deepEqual(await Promise.all([longTask, shortTask]), [true, true]); }
    finally { clearInterval(progressObserver); }
    assert.ok(shortFinishedAt < longFinishedAt, 'A short session must not queue behind a long export');
    const after = await main.webContents.executeJavaScript('window.ticks');
    const gaps = await main.webContents.executeJavaScript('window.gaps');
    const progress = await main.webContents.executeJavaScript('window.exportProgress');
    assert.ok(progress.some(event => event.sessionId === sessionIds.long && event.phase === 'rendering' && event.progress > 0 && event.progress < 100));
    assert.ok(progress.some(event => event.sessionId === sessionIds.long && event.phase === 'saving'));
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
      maximumChatTimerGapMs: Math.max(...gaps), pngWidth: 2250, pngBytes: png.length, zipBytes: zip.length,
      progressEvents: progress.filter(event => event.sessionId === sessionIds.long), shortConcurrentExportMs: shortFinishedAt - start,
      historyRecords: longRecords.length, historyBytes: realHistoryBytes }));
    app.exit(0);
  } catch (error) {
    console.error(error);
    app.exit(1);
  } finally {
    server.close();
  }
});
