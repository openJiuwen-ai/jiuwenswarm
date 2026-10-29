const fs = require('node:fs/promises');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { createShutdownReceipt } = require('./update_shutdown.cjs');

function installedBundle(executable) {
  const bundle = path.dirname(path.dirname(path.dirname(executable)));
  if (!bundle.endsWith('.app') || path.basename(path.dirname(executable)) !== 'MacOS'
      || path.basename(path.dirname(path.dirname(executable))) !== 'Contents') {
    throw new Error('The updater requires an installed Electron app');
  }
  if (bundle.startsWith('/Volumes/')) {
    throw new Error('Move the app to Applications before updating');
  }
  return bundle;
}

// The detached helper first stages and validates the new app. Only its READY
// acknowledgement allows Electron to quit; verification errors leave it running.
async function launchMacUpdate({ installerPath, executable, backendExecutable, parentPid, childPids, ports,
  updatesDir, logDir, arch }, spawnProcess = spawn) {
  const bundle = installedBundle(await fs.realpath(executable));
  const backend = await fs.realpath(backendExecutable);
  if (path.dirname(backend) !== path.join(bundle, 'Contents', 'Resources', 'backend')) {
    throw new Error('The updater requires the installed backend lifecycle helper');
  }
  const installer = await fs.realpath(installerPath);
  const root = await fs.realpath(updatesDir);
  if (path.dirname(installer) !== root || !installer.endsWith('.dmg')
      || !(await fs.stat(installer)).isFile()) {
    throw new Error('The updater only installs DMGs from its download directory');
  }
  if (!Number.isInteger(parentPid) || parentPid <= 1
      || !childPids.every(pid => Number.isInteger(pid) && pid > 1)
      || !ports.every(port => Number.isInteger(port) && port > 0 && port <= 65535)
      || !['arm64', 'x64'].includes(arch)) {
    throw new Error('Invalid macOS update process parameters');
  }
  await fs.mkdir(logDir, { recursive: true });
  // Copy outside the old .app: the script must survive the bundle swap.
  const helperDir = await fs.mkdtemp(path.join(root, 'electron-update-'));
  const helperPath = path.join(helperDir, 'install.sh');
  const shutdownReceipt = await createShutdownReceipt(helperDir);
  await fs.copyFile(path.join(__dirname, 'install_macos_update.sh'), helperPath);
  const args = [helperPath, installer, bundle, String(parentPid), childPids.join(','),
    ports.join(','), path.join(logDir, 'electron-update.log'),
    arch === 'x64' ? 'x86_64' : arch, backend, shutdownReceipt];
  return new Promise((resolve, reject) => {
    let ready = false;
    let settled = false;
    let output = '';
    const child = spawnProcess('/bin/bash', args, {
      detached: true, stdio: ['ignore', 'pipe', 'ignore'],
      env: { ...process.env, PATH: '/usr/bin:/bin:/usr/sbin:/sbin' },
    });
    const timeout = setTimeout(() => {
      child.kill('SIGTERM');
      fail(new Error('Timed out preparing the macOS update; see electron-update.log'));
    }, 180_000);
    function fail(error) {
      clearTimeout(timeout);
      if (!settled) {
        settled = true;
        reject(error);
      }
    }
    child.once('error', fail);
    child.once('exit', code => {
      if (!ready) fail(new Error(`macOS update preparation failed (${code}); see electron-update.log`));
      void fs.rm(helperDir, { recursive: true, force: true }).catch(() => {});
    });
    child.stdout.on('data', data => {
      output += data.toString();
      if (!settled && output.includes('READY\n')) {
        ready = true;
        settled = true;
        clearTimeout(timeout);
        child.stdout.destroy();
        child.unref();
        resolve({ shutdownReceipt });
      }
    });
  });
}

module.exports = { installedBundle, launchMacUpdate };
