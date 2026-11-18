const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { recordShutdownOutcome } = require('../../jiuwenswarm/channels/desktop/electron/update_shutdown.cjs');

const source = fs.readFileSync(path.join(__dirname, '../../jiuwenswarm/channels/desktop/electron/main.cjs'), 'utf8');
const ports = { agentServer: 18092, gatewayApi: 19000, gatewayInternal: 19001, frontend: 5173 };

function section(start, end) {
  const offset = source.indexOf(start);
  const finish = source.indexOf(end, offset);
  assert.ok(offset >= 0 && finish > offset, start);
  return source.slice(offset, finish);
}

function fixture(platform, frontendOnly = false) {
  const app = new EventEmitter();
  const children = new Map();
  const launches = [];
  const helpers = [];
  const exits = [];
  const errors = [];
  let watched = 0;
  let workerAlive = true; // A resident Worker from an earlier Front.
  let pid = 1000;
  app.isPackaged = true;
  app.exit = code => exits.push(code);
  const context = vm.createContext({
    app, mainWindow: null, isFrontendOnly: frontendOnly, VITE_DEV_MODE: false, hasCdp: false,
    browserTargetResolver: null, forceManagedBrowser: false, startupDiagnosticsDir: null,
    desktopLockToken: 'isolated', BACKEND_HOST: '127.0.0.1', FRONTEND_HOST: '127.0.0.1',
    process: { platform, argv: ['workswarm'], execPath: 'workswarm',
      env: { JIUWENSWARM_DATA_DIR: 'isolated' }, kill(group, signal) {
      const child = children.get(-group);
      if (!child || child.exitCode !== null) throw Object.assign(new Error('gone'), { code: 'ESRCH' });
      if (signal !== 0) { child.exitCode = 0; queueMicrotask(() => child.emit('exit', 0)); }
    } },
    serviceProcesses: new Map(), shuttingDown: false, quitRequested: false, shutdownPromise: null,
    finalShutdownPromise: null, shutdownComplete: false, requestedExitCode: 0,
    SERVICE_SHUTDOWN_TIMEOUT_MS: 500, SERVICE_KILL_TIMEOUT_MS: 500, RUNTIME_SHUTDOWN_TIMEOUT_MS: 3000,
    backendExecutable: () => 'backend', serviceWorkingDirectory: () => '.', logStreamFor: () => 'ignore',
    updateShutdownReceipt: null, recordShutdownOutcome, saveSessionLastUrls() {}, saveStoredDownloads() {},
    abortAllBlobSaves: async () => {}, destroyTray() {}, setTimeout, clearTimeout,
    console: { log() {}, warn() {}, error: (...args) => errors.push(args) },
    waitForTcp: async () => {}, watchBackendPair: () => { watched += 1; },
    spawn(command, args, options) {
      const child = new EventEmitter();
      Object.assign(child, { pid: ++pid, exitCode: null, signalCode: null, unref() {}, kill() {} });
      if (args.includes('--desktop-run-worker-lifecycle') || command === 'taskkill') {
        helpers.push({ command, args, env: options.env });
        if (args.includes('--shutdown-worker')) workerAlive = false;
        const target = args.includes('--detach-front') ? args[args.indexOf('--detach-front') + 1]
          : command === 'taskkill' ? args[1] : null;
        if (target) {
          const stopped = children.get(Number(target));
          stopped.exitCode = 0;
          queueMicrotask(() => stopped.emit('exit', 0));
        }
        queueMicrotask(() => child.emit('exit', 0));
      } else {
        children.set(child.pid, child);
        launches.push({ args, duringShutdown: context.shuttingDown });
      }
      return child;
    },
  });
  vm.runInContext([
    section('function serviceCommand(', 'function waitForTcp('),
    section('function runToExit(', 'function prepareRuntimeWorkspace('),
    section('async function startBackendServices(', 'function forceStopServices('),
    section("app.on('before-quit'", "app.on('activate'"),
  ].join('\n'), context);
  context.prepareRuntimeWorkspace = () => context.runToExit('backend', ['--desktop-prepare-runtime-workspace']);
  const quit = () => app.emit('before-quit', { preventDefault() {} });
  return { context, launches, helpers, exits, errors, quit,
    get workerAlive() { return workerAlive; }, get watched() { return watched; } };
}

for (const platform of ['darwin', 'win32']) {
  for (const preparationExit of [0, 1]) {
    test(`${platform}: Quit wins a queued workspace exit (${preparationExit}) without launching backend`, async () => {
      const f = fixture(platform);
      const webProcess = f.context.spawnService('web', ports);
      const starting = f.context.startBackendServices({ ports, webProcess, webReady: Promise.resolve() });
      const preparation = f.context.serviceProcesses.get('workspace-prepare');
      assert.ok(preparation);
      // OS exit precedes Quit, but Node dispatches its completion after the shutdown snapshot.
      preparation.exitCode = preparationExit;
      f.quit();
      f.quit();
      preparation.emit('exit', preparationExit);
      await Promise.all([starting, f.context.finalShutdownPromise]);
      assert.deepEqual(f.launches.filter(item => item.duringShutdown), []);
      assert.equal(f.launches.some(item => item.args.includes('--desktop-run-agent')), false);
      assert.equal(f.launches.some(item => item.args.includes('--desktop-run-gateway')), false);
      assert.equal(f.helpers.filter(item => item.args.includes('--shutdown-worker')).length, 1);
      assert.equal(f.workerAlive, false);
      assert.equal(f.watched, 0);
      assert.equal(f.errors.length, 0);
      assert.deepEqual(f.exits, [0]);
      assert.equal(f.context.serviceProcesses.size, 0);
    });
  }

  test(`${platform}: service entry points reject after shutdown without spawning`, async () => {
    const f = fixture(platform);
    await f.context.stopServices();
    assert.throws(() => f.context.spawnService('agent', ports), /shutting down/i);
    await assert.rejects(f.context.runToExit('backend', ['prepare']), /shutting down/i);
    await f.context.startBackendServices({ ports, webReady: Promise.resolve() });
    assert.equal(f.launches.length, 0);
  });

  for (const frontendOnly of [false, true]) {
    test(`${platform}: pre-Agent Quit respects frontend-only=${frontendOnly}`, async () => {
      const f = fixture(platform, frontendOnly);
      f.quit();
      await f.context.finalShutdownPromise;
      const finalHelpers = f.helpers.filter(item => item.args.includes('--shutdown-worker'));
      assert.equal(finalHelpers.length, frontendOnly ? 0 : 1);
      if (!frontendOnly) assert.equal(finalHelpers[0].env, f.context.process.env);
      assert.equal(f.workerAlive, frontendOnly);
      assert.deepEqual(f.exits, [0]);
    });
  }

  test(`${platform}: ready and detached Front still shut down the same deployment`, async () => {
    for (const frontGone of [false, true]) {
      const f = fixture(platform);
      const webProcess = f.context.spawnService('web', ports);
      const starting = f.context.startBackendServices({ ports, webProcess, webReady: Promise.resolve() });
      const preparation = f.context.serviceProcesses.get('workspace-prepare');
      preparation.exitCode = 0;
      preparation.emit('exit', 0);
      await starting;
      assert.equal(f.watched, 1);
      const agent = f.context.serviceProcesses.get('agent');
      if (frontGone) agent.exitCode = 0;
      f.quit();
      await f.context.finalShutdownPromise;
      const finalHelpers = f.helpers.filter(item => item.args.includes('--shutdown-worker'));
      assert.equal(finalHelpers.length, 1);
      assert.equal(finalHelpers[0].env, agent.spawnEnvironment);
      assert.equal(f.workerAlive, false);
      assert.deepEqual(f.exits, [0]);
    }
  });

  test(`${platform}: Quit during readiness does not install backend watchers`, async () => {
    const f = fixture(platform);
    let ready;
    const webReady = new Promise(resolve => { ready = resolve; });
    const starting = f.context.startBackendServices({ ports, webReady });
    const preparation = f.context.serviceProcesses.get('workspace-prepare');
    preparation.exitCode = 0;
    preparation.emit('exit', 0);
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(f.context.serviceProcesses.has('agent'));
    f.quit();
    ready();
    await Promise.all([starting, f.context.finalShutdownPromise]);
    assert.equal(f.watched, 0);
    assert.deepEqual(f.exits, [0]);
  });
}

function windowFixture() {
  const f = fixture('win32', true);
  const window = new EventEmitter();
  Object.assign(window, {
    visible: true, shows: 0,
    isDestroyed: () => false, isMinimized: () => false,
    hide() { this.visible = false; }, show() { this.visible = true; this.shows += 1; },
    restore() {}, maximize() {}, focus() {},
  });
  Object.assign(f.context, {
    mainWindow: window, tray: null, resolveIconPath: () => 'logo.ico',
    Tray: class extends EventEmitter {
      destroyed = false;
      isDestroyed() { return this.destroyed; }
      destroy() { this.destroyed = true; }
      setToolTip() {}
      setContextMenu(menu) { this.menu = menu; }
    },
    Menu: { buildFromTemplate: items => items },
    CLOSE_ACTION_ASK: 'ask', CLOSE_ACTION_QUIT: 'quit',
    loadCloseAction: () => 'hide',
  });
  vm.runInContext([
    section('function showAndMaximizeMainWindow()', 'async function promptCloseAction('),
    section('async function handleMainWindowCloseRequest()', 'function loadingHtml('),
    section("  app.on('second-instance'", '  app.whenReady()'),
    section("app.on('activate'", "app.on('window-all-closed'"),
    section("  mainWindow.once('ready-to-show'", '  registerDisplayMediaHandler('),
  ].join('\n'), f.context);
  return { ...f, window };
}

test('hide to tray keeps services running and supports tray restoration', async () => {
  const f = windowFixture();
  await f.context.handleMainWindowCloseRequest();
  const tray = f.context.tray;
  assert.equal(f.window.visible, false);
  assert.equal(f.context.shuttingDown, false);
  assert.equal(f.context.finalShutdownPromise, null);
  tray.emit('double-click');
  assert.equal(f.window.visible, true);
  f.window.hide();
  tray.menu[0].click();
  assert.equal(f.window.visible, true);
  assert.equal(tray.destroyed, false);
});

test('Quit hides the window and removes tray before cleanup; queued wakeups cannot restore it', async () => {
  const f = windowFixture();
  f.context.ensureTray();
  const tray = f.context.tray;
  let finishLoad;
  f.window.loadURL = () => new Promise(resolve => { finishLoad = resolve; });
  Object.assign(f.context, {
    URL, navigated: false, currentFrontendUrl: '',
  });
  vm.runInContext(section('  const onWebReady =', '  let webStartup =')
    + '\nonWebReady("http://127.0.0.1:5173");', f.context);
  let finishCleanup;
  f.context.stopServices = () => {
    assert.equal(f.window.visible, false);
    assert.equal(tray.destroyed, true);
    return new Promise(resolve => { finishCleanup = resolve; });
  };
  f.quit();
  assert.equal(f.context.tray, null);
  assert.deepEqual(f.exits, []);
  tray.emit('double-click');
  tray.menu[0].click();
  f.context.app.emit('second-instance');
  f.context.app.emit('activate');
  f.window.emit('ready-to-show');
  finishLoad();
  await Promise.resolve();
  assert.equal(f.window.visible, false);
  assert.equal(f.window.shows, 0);
  finishCleanup();
  await f.context.finalShutdownPromise;
  assert.deepEqual(f.exits, [0]);
});

test('a pending close choice cannot recreate tray after Quit', async () => {
  const f = windowFixture();
  let finishChoice;
  f.context.loadCloseAction = () => 'ask';
  f.context.promptCloseAction = () => new Promise(resolve => { finishChoice = resolve; });
  const closing = f.context.handleMainWindowCloseRequest();
  f.quit();
  finishChoice({ action: 'hide', remember: false });
  await Promise.all([closing, f.context.finalShutdownPromise]);
  assert.equal(f.context.tray, null);
  assert.equal(f.window.visible, false);
});

for (const failure of ['hide', 'destroyTray']) {
  test(`UI teardown failure in ${failure} still starts cleanup and exits`, async () => {
    const f = windowFixture();
    f.context.ensureTray();
    const tray = f.context.tray;
    if (failure === 'hide') {
      f.window.hide = () => { throw new Error('native hide failed'); };
    } else {
      tray.destroy = () => { throw new Error('native tray destruction failed'); };
    }
    let cleanups = 0;
    f.context.stopServices = async () => { cleanups += 1; };
    f.quit();
    assert.notEqual(f.context.finalShutdownPromise, null);
    await f.context.finalShutdownPromise;
    assert.equal(cleanups, 1);
    assert.equal(f.context.tray, null);
    assert.equal(f.errors.length, 1);
    assert.deepEqual(f.exits, [0]);
  });
}
