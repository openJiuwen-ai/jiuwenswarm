const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { installedBundle, launchMacUpdate } = require('../../jiuwenswarm/channels/desktop/electron/macos_update.cjs');

test('bundle is derived from Electron, not its nested Python backend', () => {
  assert.equal(installedBundle('/Applications/Work Swarm.app/Contents/MacOS/workswarm'), '/Applications/Work Swarm.app');
  assert.throws(() => installedBundle('/Applications/A.app/Contents/Resources/backend/app'));
  assert.throws(() => installedBundle('/Volumes/A/A.app/Contents/MacOS/Electron'));
});

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'electron-update-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const executable = path.join(root, "Work 'Swarm.app/Contents/MacOS/Electron");
  await fs.mkdir(path.dirname(executable), { recursive: true });
  await fs.writeFile(executable, 'test');
  const backendExecutable = path.join(root, "Work 'Swarm.app/Contents/Resources/backend/workswarm");
  await fs.mkdir(path.dirname(backendExecutable), { recursive: true });
  await fs.writeFile(backendExecutable, 'test');
  const updatesDir = path.join(root, '.updates');
  await fs.mkdir(updatesDir);
  const installerPath = path.join(updatesDir, "Work 'Swarm-electron-setup.dmg");
  await fs.writeFile(installerPath, 'test');
  return { executable, backendExecutable, installerPath, updatesDir, logDir: path.join(root, 'logs'),
    parentPid: 123, childPids: [456], ports: [19000], arch: 'arm64' };
}

test('only READY permits shutdown; paths are distinct argv', async t => {
  const options = await fixture(t);
  const realInstaller = await fs.realpath(options.installerPath);
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.unref = () => {};
  const pending = launchMacUpdate(options, (command, args, config) => {
    assert.equal(command, '/bin/bash');
    assert.equal(args[1], realInstaller);
    assert.equal(args[3], '123');
    assert.equal(args[4], '456');
    assert.equal(args.length, 10);
    assert.equal(args[8], options.backendExecutable);
    assert.equal(config.detached, true);
    process.nextTick(() => { child.stdout.write('REA'); child.stdout.write('DY\n'); });
    return child;
  });
  const prepared = await pending;
  assert.equal(await fs.readFile(prepared.shutdownReceipt, 'utf8'), 'pending\n');
});

test('preflight failure rejects rather than reporting a successful update', async t => {
  const options = await fixture(t);
  await assert.rejects(launchMacUpdate(options, () => {
    const child = new EventEmitter();
    child.stdout = new PassThrough();
    process.nextTick(() => child.emit('exit', 1));
    return child;
  }), /preparation failed/);
});

test('a symlink escaping the download directory is rejected before spawning', async t => {
  const options = await fixture(t);
  const outside = path.join(path.dirname(options.updatesDir), 'outside.dmg');
  await fs.rename(options.installerPath, outside);
  try {
    await fs.symlink(outside, options.installerPath);
  } catch (error) {
    if (process.platform === 'win32' && error.code === 'EPERM') {
      t.skip('Windows symlink permission is unavailable');
      return;
    }
    throw error;
  }
  await assert.rejects(launchMacUpdate(options, () => assert.fail('spawned')), /download directory/);
});

test('a backend outside the installed app is rejected before spawning', async t => {
  const options = await fixture(t);
  options.backendExecutable = options.installerPath;
  await assert.rejects(launchMacUpdate(options, () => assert.fail('spawned')), /lifecycle helper/);
});

test('both desktop APIs forward the installer path without a private protocol', async () => {
  const apis = {};
  const calls = [];
  const source = await fs.readFile(path.join(__dirname, '../../jiuwenswarm/channels/desktop/electron/preload.cjs'), 'utf8');
  vm.runInNewContext(source, { require: () => ({
    contextBridge: { exposeInMainWorld: (name, api) => { apis[name] = api; } },
    ipcRenderer: { sendSync: () => false, invoke: (...args) => { calls.push(args); } },
  }) });
  apis.pywebview.api.install_update('/tmp/update.dmg');
  assert.deepEqual(calls[0], ['desktop:install-update', '/tmp/update.dmg']);
  apis.jiuwenDesktop.installUpdate('/tmp/update.dmg');
  assert.deepEqual(calls[1], ['desktop:install-update', '/tmp/update.dmg']);
});
