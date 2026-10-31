// Run as an Electron main script, using a temporary profile and local pages.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const { app, BrowserWindow } = require('electron');
const { reserveLoopbackPortSync, waitForOwnedCdpTarget } = require(process.env.SWARM_TEST_CDP_ALLOCATOR
  || '../../jiuwenswarm/channels/desktop/electron/cdp_port.cjs');

if (!process.env.SWARM_TEST_CDP_PROFILE || !process.env.SWARM_TEST_CDP_RESULT) {
  throw new Error('Set SWARM_TEST_CDP_PROFILE and SWARM_TEST_CDP_RESULT to temporary paths');
}
app.setPath('userData', process.env.SWARM_TEST_CDP_PROFILE);
const started = performance.now();
const port = reserveLoopbackPortSync();
const reservationMs = performance.now() - started;
app.commandLine.appendSwitch('remote-debugging-address', '127.0.0.1');
app.commandLine.appendSwitch('remote-debugging-port', String(port));
const timeout = setTimeout(() => app.exit(1), 20_000);

function readTargets() {
  return new Promise((resolve, reject) => {
    http.get(`http://127.0.0.1:${port}/json/list`, response => {
      let text = '';
      response.on('data', chunk => { text += chunk; });
      response.on('end', () => {
        try { resolve(JSON.parse(text)); } catch (error) { reject(error); }
      });
    }).on('error', reject);
  });
}

app.whenReady().then(async () => {
  app.dock?.hide();
  const options = { show: false, webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true } };
  const main = new BrowserWindow(options);
  const child = new BrowserWindow({ ...options, parent: main });
  await Promise.all([main.loadURL('about:blank'), child.loadURL('about:blank')]);
  await waitForOwnedCdpTarget({ port, targetId: main.webContents.getOrCreateDevToolsTargetId() });
  const expression = '({webdriver:navigator.webdriver,userAgent:navigator.userAgent})';
  const mainSignals = await main.webContents.executeJavaScript(expression);
  const childSignals = await child.webContents.executeJavaScript(expression);
  assert.equal(mainSignals.webdriver, false);
  assert.equal(childSignals.webdriver, false);
  const targets = await readTargets();
  for (const window of [main, child]) {
    assert.ok(targets.some(target => target.id === window.webContents.getOrCreateDevToolsTargetId()));
  }
  const result = { electron: process.versions.electron, chromium: process.versions.chrome,
    port, reservationMs, main: mainSignals, child: childSignals, cdpTargetsVerified: true };
  fs.writeFileSync(process.env.SWARM_TEST_CDP_RESULT, `${JSON.stringify(result, null, 2)}\n`);
  console.log(JSON.stringify(result));
  clearTimeout(timeout);
  app.exit(0);
}).catch(error => {
  console.error(error);
  clearTimeout(timeout);
  app.exit(1);
});
