const { spawn } = require('node:child_process');
const nodeNet = require('node:net');
const electronExecutable = require('electron');

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;
// IDEs built on Electron (VS Code / Trae) inject ELECTRON_FORCE_IS_PACKAGED=1 into
// their own process so extensions cannot detect dev builds. Terminals spawned from
// the IDE inherit it, which makes app.isPackaged lie in dev mode and the shell goes
// hunting for a packaged backend exe. Dev launch must always look unpackaged.
delete env.ELECTRON_FORCE_IS_PACKAGED;

let child = null;

function reserveLoopbackPort() {
  return new Promise((resolve, reject) => {
    const server = nodeNet.createServer();
    server.unref();
    server.once('error', reject);
    server.listen({ host: '127.0.0.1', port: 0, exclusive: true }, () => {
      const address = server.address();
      const port = typeof address === 'object' && address ? address.port : 0;
      server.close(error => {
        if (error) reject(error);
        else if (!port) reject(new Error('Failed to allocate Electron CDP port'));
        else resolve(port);
      });
    });
  });
}

async function launchElectron() {
  env.JIUWENSWARM_ELECTRON_CDP_PORT = String(await reserveLoopbackPort());
  child = spawn(electronExecutable, ['.', ...process.argv.slice(2)], {
    cwd: __dirname,
    env,
    detached: process.platform !== 'win32',
    stdio: ['inherit', 'inherit', 'inherit', 'ipc'],
    windowsHide: false,
  });

  child.once('error', error => {
    console.error('[electron-launcher] failed to launch Electron', error);
    process.exitCode = 1;
  });

  child.once('exit', (code, signal) => {
    if (shutdownTimer) clearTimeout(shutdownTimer);
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exitCode = code ?? 1;
  });
}

let shutdownRequested = false;
let shutdownTimer = null;

function requestShutdown(signal) {
  if (shutdownRequested) return;
  shutdownRequested = true;
  const exitCode = signal === 'SIGINT' ? 130 : 143;
  if (!child) {
    process.exitCode = exitCode;
    return;
  }
  if (child.connected) {
    child.send({ type: 'jiuwenswarm:shutdown', exitCode }, error => {
      if (error && child.exitCode === null) child.kill('SIGTERM');
    });
  } else if (child.exitCode === null) {
    child.kill('SIGTERM');
  }
  shutdownTimer = setTimeout(() => {
    if (child.exitCode === null) child.kill('SIGKILL');
  }, 10_000);
  shutdownTimer.unref();
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => requestShutdown(signal));
}

void launchElectron().catch(error => {
  console.error('[electron-launcher] failed to reserve CDP endpoint', error);
  process.exitCode = 1;
});
