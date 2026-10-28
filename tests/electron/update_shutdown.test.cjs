const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { createShutdownReceipt, completeShutdownReceipt, recordShutdownOutcome } = require('../../jiuwenswarm/channels/desktop/electron/update_shutdown.cjs');

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
