const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { createRequire } = require('node:module');
const { createShutdownReceipt, completeShutdownReceipt, recordShutdownOutcome } = require('../../jiuwenswarm/channels/desktop/electron/update_shutdown.cjs');

test('complete production entry loads and registers application shutdown', async () => {
  const entry = path.resolve(__dirname, '../../jiuwenswarm/channels/desktop/electron/main.cjs');
  const source = await fs.readFile(entry, 'utf8');
  const host = new EventEmitter();
  let readyCallback;
  const switches = new Map();
  Object.assign(host, {
    isPackaged: false,
    commandLine: { appendSwitch: (key, value) => switches.set(key, value) },
    requestSingleInstanceLock: () => true,
    whenReady: () => ({ then: callback => { readyCallback = callback; } }),
  });
  const processHost = new EventEmitter();
  Object.assign(processHost, { platform: 'win32', env: { JIUWENSWARM_ELECTRON_CDP_PORT: '19319' },
    argv: ['node', entry], execPath: process.execPath, pid: process.pid });
  const realRequire = createRequire(entry);
  vm.runInNewContext(source, {
    require: name => name === 'electron' ? { app: host } : realRequire(name),
    __filename: entry, __dirname: path.dirname(entry), module: { exports: {} }, exports: {},
    process: processHost, Buffer, URL, AbortController, setTimeout, clearTimeout, setInterval, clearInterval,
    console: { log() {}, error() {}, warn() {} },
  }, { filename: entry });
  assert.equal(typeof readyCallback, 'function');
  assert.equal(host.listenerCount('before-quit'), 1);
  assert.equal(host.listenerCount('window-all-closed'), 1);
  assert.equal(processHost.listenerCount('exit'), 1);
  assert.equal(switches.get('remote-debugging-port'), '19319');
});

for (const failure of ['exit', 'spawn', 'timeout', 'service', 'survivor']) {
  test(`failed ${failure} shutdown blocks update even when the next check sees absent`, async t => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shutdown-receipt-'));
    t.after(() => fs.rm(root, { recursive: true, force: true }));
    const receipt = await createShutdownReceipt(root);
    const source = await fs.readFile(path.join(__dirname, '../../jiuwenswarm/channels/desktop/electron/main.cjs'), 'utf8');
    const stop = source.slice(source.indexOf('function stopServices()'), source.indexOf('function forceStopServices()'));
    const beforeQuit = source.slice(source.indexOf("app.on('before-quit'"), source.indexOf("app.on('activate'"));
    let listener;
    let exitCode;
    let spawns = 0;
    let saves = 0;
    const context = vm.createContext({
      shutdownPromise: null, finalShutdownPromise: null, shutdownComplete: false,
      shuttingDown: false, quitRequested: false, requestedExitCode: 0,
      serviceProcesses: new Map([['agent', { pid: 123 }]]), RUNTIME_SHUTDOWN_TIMEOUT_MS: 1,
      terminateService: async () => { if (failure === 'service') throw new Error('Front cleanup failed'); },
      serviceTreeRunning: () => failure === 'survivor',
      serviceCommand: () => ({ command: 'backend', args: [] }), serviceWorkingDirectory: () => root,
      spawn: () => {
        spawns += 1;
        if (failure === 'spawn') throw new Error('Cannot spawn lifecycle helper');
        const child = new EventEmitter();
        child.kill = () => {};
        if (failure !== 'timeout') queueMicrotask(() => child.emit('exit', failure === 'exit' ? 1 : 0));
        return child;
      },
      setTimeout, clearTimeout, console: { log() {}, error() {} }, process: { env: {} },
      updateShutdownReceipt: receipt, recordShutdownOutcome,
      saveSessionLastUrls: () => { saves += 1; }, saveStoredDownloads() {}, destroyTray() {},
      abortAllBlobSaves: async () => {},
      app: { on: (_, callback) => { listener = callback; }, exit: code => { exitCode = code; } },
    });
    vm.runInContext(stop + '\n' + beforeQuit, context);
    listener({ preventDefault() {} });
    listener({ preventDefault() {} });
    await context.finalShutdownPromise;
    assert.equal(exitCode, 1);
    assert.equal(saves, 1, 'one owner must finish the shutdown attempt');
    assert.equal(spawns, 1);
    assert.equal(await fs.readFile(receipt, 'utf8'), 'failed\n');
    await assert.rejects(context.stopServices());
    await assert.rejects(recordShutdownOutcome(receipt, async () => ({ ok: true, absent: true })), /cannot be marked successful/);
    assert.equal(await fs.readFile(receipt, 'utf8'), 'failed\n');
  });
}

test('only successful shutdown completes this unique update attempt', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shutdown-receipt-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const first = await createShutdownReceipt(root);
  const second = await createShutdownReceipt(root);
  assert.notEqual(first, second);
  await recordShutdownOutcome(first, async () => {});
  assert.equal(await fs.readFile(first, 'utf8'), 'ok\n');
  assert.equal(await fs.readFile(second, 'utf8'), 'pending\n');
  await completeShutdownReceipt(first, false);
  await assert.rejects(completeShutdownReceipt(first, true), /cannot be marked successful/);
});

for (const packaged of [false, true]) {
  test(`production lifecycle routing detaches Windows Front and completes shutdown (packaged=${packaged})`, async () => {
    const source = await fs.readFile(path.join(__dirname, '../../jiuwenswarm/channels/desktop/electron/main.cjs'), 'utf8');
    const budget = source.match(/^const RUNTIME_SHUTDOWN_TIMEOUT_MS = .+;$/m);
    assert.ok(budget, 'the production deadline must be declared');
    const command = source.slice(source.indexOf('function serviceCommand('), source.indexOf('function spawnService('));
    const signal = source.slice(source.indexOf('function signalServiceTree('), source.indexOf('function serviceTreeRunning('));
    const stop = source.slice(source.indexOf('function stopServices()'), source.indexOf('function forceStopServices()'));
    const child = { pid: 123, exitCode: null, spawnEnvironment: { JIUWENSWARM_DATA_DIR: 'this-deployment' } };
    const calls = [];
    const context = vm.createContext({
      app: { isPackaged: packaged }, VITE_DEV_MODE: false,
      backendExecutable: () => 'backend.exe',
      process: { platform: 'win32', env: {}, execPath: 'node' },
      serviceProcesses: new Map([['agent', child]]), shutdownPromise: null, shuttingDown: false,
      SERVICE_SHUTDOWN_TIMEOUT_MS: 100, SERVICE_KILL_TIMEOUT_MS: 100,
      serviceWorkingDirectory: () => '.',
      terminateService: async () => { child.exitCode = 0; },
      serviceTreeRunning: service => service.exitCode === null,
      console: { log() {}, error() {} }, setTimeout, clearTimeout,
      spawn: (executable, args, options) => {
        calls.push({ executable, args: [...args], options });
        const helper = new EventEmitter();
        helper.unref = () => {};
        helper.kill = () => {};
        queueMicrotask(() => helper.emit('exit', 0));
        return helper;
      },
    });
    vm.runInContext(budget[0] + '\n' + command + '\n' + signal + '\n' + stop, context);
    assert.equal(vm.runInContext('RUNTIME_SHUTDOWN_TIMEOUT_MS', context), 30_000);
    assert.equal(context.signalServiceTree(child, 'SIGTERM'), true);
    assert.equal(calls[0].args.includes('--detach-front'), true);
    assert.equal(calls[0].executable === 'taskkill', false);
    assert.equal(calls[0].options.env, child.spawnEnvironment);
    assert.equal(context.signalServiceTree(child, 'SIGKILL'), true);
    assert.equal(calls[1].args.includes('--force'), true);
    assert.equal(calls[1].executable === 'taskkill', false, 'Worker subtree must survive Front force-stop');
    await context.stopServices();
    assert.equal(calls[2].args.includes('--shutdown-worker'), true);
    assert.equal(calls[2].args.includes(packaged ? '--desktop-run-worker-lifecycle' : 'jiuwenswarm.server.supervisor.shutdown'), true);
    assert.equal(calls[2].options.env, child.spawnEnvironment);
  });
}

test('missing receipt cannot hide the original shutdown error', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shutdown-receipt-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const receipt = await createShutdownReceipt(root);
  await fs.rm(receipt);
  const failure = new Error('Original Front shutdown failure');
  await assert.rejects(recordShutdownOutcome(receipt, async () => { throw failure; }), error => error === failure);
  await assert.rejects(recordShutdownOutcome(receipt, async () => {}), { code: 'ENOENT' });
});

test('failed installer spawn does not poison a later normal quit', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shutdown-receipt-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const source = await fs.readFile(path.join(__dirname, '../../jiuwenswarm/channels/desktop/electron/main.cjs'), 'utf8');
  const block = source.slice(source.indexOf('    if (canUseBackendUpdateHelper()) {'),
    source.indexOf('    const errorMessage = await shell.openPath(target);'));
  const helper = new EventEmitter();
  helper.unref = () => {};
  let prompts = 0;
  const context = vm.createContext({
    target: 'new-installer.exe', updateShutdownReceipt: null, process: { env: {}, execPath: 'desktop', pid: 1 },
    app: { getPath: () => root }, path, sessionPorts: {}, createShutdownReceipt,
    completeShutdownReceipt,
    canUseBackendUpdateHelper: () => true, backendExecutable: () => 'backend', serviceWorkingDirectory: () => root,
    spawn: () => helper, console: { error() {} },
    dialog: { showMessageBox: async () => { prompts += 1; } },
    requestShutdown: () => assert.fail('Failed helper must not initiate update shutdown'), setTimeout,
  });
  vm.runInContext(`async function install() { ${block} }`, context);
  assert.equal(await context.install(), true);
  const receipt = context.updateShutdownReceipt;
  const failed = new Promise(resolve => {
    context.completeShutdownReceipt = async (...args) => { await completeShutdownReceipt(...args); resolve(); };
  });
  helper.emit('error', new Error('ENOENT'));
  await failed;
  assert.equal(context.updateShutdownReceipt, null);
  assert.equal(prompts, 1);
  assert.equal(await fs.readFile(receipt, 'utf8'), 'failed\n');
  await recordShutdownOutcome(context.updateShutdownReceipt, async () => {});
});

for (const failure of ['success', 'retry', 'nonzero', 'spawn', 'timeout']) {
  test(`Front detach ${failure} must be confirmed before completing an update receipt`, async t => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'detach-receipt-'));
    t.after(() => fs.rm(root, { recursive: true, force: true }));
    const receipt = await createShutdownReceipt(root);
    const source = await fs.readFile(path.join(__dirname, '../../jiuwenswarm/channels/desktop/electron/main.cjs'), 'utf8');
    const block = source.slice(source.indexOf('function signalServiceTree('), source.indexOf('function forceStopServices('));
    const child = { pid: 123, exitCode: null };
    let detachCalls = 0;
    const context = vm.createContext({
      serviceProcesses: new Map([['agent', child]]), shutdownPromise: null, shuttingDown: false,
      SERVICE_SHUTDOWN_TIMEOUT_MS: 20, SERVICE_KILL_TIMEOUT_MS: 20, RUNTIME_SHUTDOWN_TIMEOUT_MS: 200,
      serviceCommand: (_, args) => ({ command: 'backend', args }), serviceWorkingDirectory: () => root,
      process: { platform: 'win32', env: {} }, setTimeout, clearTimeout,
      console: { log() {}, warn() {}, error() {} },
      spawn: (_, args) => {
        if (failure === 'spawn' && args.includes('--detach-front')) throw new Error('Cannot spawn detach helper');
        const helper = new EventEmitter();
        helper.unref = () => {};
        helper.kill = () => {};
        if (args.includes('--detach-front')) {
          detachCalls += 1;
          if (failure !== 'retry' || detachCalls > 1) child.exitCode = 0;
          if (failure !== 'timeout') {
            queueMicrotask(() => helper.emit('exit', failure === 'success' || detachCalls > 1 ? 0 : 1));
          }
        } else {
          queueMicrotask(() => helper.emit('exit', 0));
        }
        return helper;
      },
    });
    vm.runInContext(block, context);
    if (['success', 'retry'].includes(failure)) {
      await recordShutdownOutcome(receipt, () => context.stopServices());
      assert.equal(await fs.readFile(receipt, 'utf8'), 'ok\n');
      assert.equal(detachCalls, failure === 'retry' ? 2 : 1);
    } else {
      await assert.rejects(recordShutdownOutcome(receipt, () => context.stopServices()));
      assert.equal(await fs.readFile(receipt, 'utf8'), 'failed\n');
      if (failure !== 'spawn') assert.equal(detachCalls, 1, 'A missing launcher cannot erase failed descendant cleanup');
    }
  });
}

for (const exited of ['agent', 'gateway']) {
  test(`paired backend cleanup consumes rejected termination after ${exited} exit`, async () => {
    const source = await fs.readFile(path.join(__dirname, '../../jiuwenswarm/channels/desktop/electron/main.cjs'), 'utf8');
    const block = source.slice(source.indexOf('function watchBackendPair('), source.indexOf('const GATEWAY_LOCK_FILENAME'));
    let poll;
    const errors = [];
    const calls = [];
    const context = vm.createContext({
      shuttingDown: false, setInterval: callback => { poll = callback; return 1; }, clearInterval() {},
      console: { error: (...args) => errors.push(args) },
      terminateService: async child => { calls.push(child); throw new Error('Front cleanup refused'); },
    });
    vm.runInContext(block, context);
    const agent = { exitCode: exited === 'agent' ? 1 : null };
    const gateway = { exitCode: exited === 'gateway' ? 1 : null };
    context.watchBackendPair(agent, gateway);
    poll();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(calls, [exited === 'agent' ? gateway : agent]);
    assert.equal(errors.some(args => args[0] === '[electron] background service cleanup incomplete'
      && args[1].message === 'Front cleanup refused'), true);
  });
}

test('startup failure preserves its original error and consumes background cleanup failures', async () => {
  const source = await fs.readFile(path.join(__dirname, '../../jiuwenswarm/channels/desktop/electron/main.cjs'), 'utf8');
  const block = source.slice(source.indexOf('async function startBackendServices('), source.indexOf('function signalServiceTree('));
  const startupError = new Error('Backend startup failed');
  const errors = [];
  const context = vm.createContext({
    BACKEND_HOST: '127.0.0.1', prepareRuntimeWorkspace: async () => {}, spawnService: name => ({ name }),
    waitForTcp: async () => { throw startupError; },
    terminateService: async () => { throw new Error('Background stop failed'); },
    console: { error: (...args) => errors.push(args) },
  });
  vm.runInContext(block, context);
  await assert.rejects(context.startBackendServices({ ports: {}, webProcess: { name: 'web' },
    webReady: Promise.resolve() }), error => error === startupError);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(errors.filter(args => args[0] === '[electron] background service cleanup incomplete').length, 3);
});
