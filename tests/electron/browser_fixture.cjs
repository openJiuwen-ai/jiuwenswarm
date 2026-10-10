// Exercise the production browser/IPC functions without starting Python services.
const { app, BrowserWindow, session } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');

if (!process.env.SWARM_TEST_PROFILE) throw new Error('An isolated test profile is required');
app.setPath('userData', process.env.SWARM_TEST_PROFILE);
const mainPath = path.resolve(__dirname, '../../jiuwenswarm/channels/desktop/electron/main.cjs');
const source = fs.readFileSync(mainPath, 'utf8');
const bootstrap = source.indexOf('if (!app.requestSingleInstanceLock())');
if (bootstrap < 0) throw new Error('Electron bootstrap boundary changed');
const browserModule = new Module(mainPath, module);
browserModule.filename = mainPath;
browserModule.paths = Module._nodeModulePaths(path.dirname(mainPath));
browserModule._compile(`${source.slice(0, bootstrap)}
module.exports = {
  async init(window) {
    mainWindow = window;
    registerIpcHandlers();
    browserTargetResolver = await startBrowserTargetResolver();
    return buildBrowserEndpointsPayload();
  },
  ensureBrowserView, setBrowserPaneVisible, browserViews, sessionLastUrls,
  layoutState() { return { bounds: lastBrowserBounds, contentBounds: mainWindow.getContentBounds(), zoomFactor: mainWindow.webContents.getZoomFactor() }; },
  stop() { browserTargetResolver.server.close(); saveSessionLastUrls(); }
};`, mainPath);
global.browserTest = browserModule.exports;

app.whenReady().then(async () => {
  const window = new BrowserWindow({
    width: 1100, height: 780, show: false,
    webPreferences: {
      preload: path.join(path.dirname(mainPath), 'preload.cjs'),
      contextIsolation: true, sandbox: true, nodeIntegration: false,
    },
  });
  // No external traffic, real accounts or user data in this regression fixture.
  const browserSession = session.fromPartition('persist:jiuwenswarm-browser');
  browserSession.webRequest.onBeforeRequest((details, callback) => {
    callback({ cancel: /^https?:/.test(details.url) && !details.url.startsWith(process.env.SWARM_TEST_URL) });
  });
  global.browserTest.endpoints = await global.browserTest.init(window);
  await window.loadURL(process.env.SWARM_TEST_URL);
  window.showInactive();
});
app.on('window-all-closed', () => app.quit());
