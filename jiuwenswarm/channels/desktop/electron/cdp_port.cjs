const { Worker } = require('node:worker_threads');
const http = require('node:http');

function reservePortInWorker() {
  const { workerData } = require('node:worker_threads');
  const net = require('node:net');
  const state = new Int32Array(workerData);
  const finish = port => {
    Atomics.store(state, 1, port);
    Atomics.store(state, 0, port > 0 ? 1 : -1);
    Atomics.notify(state, 0);
  };
  const server = net.createServer();
  server.once('error', () => finish(0));
  server.listen({ host: '127.0.0.1', port: 0, exclusive: true }, () => {
    const address = server.address();
    const port = address && typeof address === 'object' ? address.port : 0;
    server.close(error => finish(error ? 0 : port));
  });
}

function reserveLoopbackPortSync({ timeoutMs = 5_000 } = {}) {
  if (!Number.isInteger(timeoutMs) || timeoutMs <= 0) {
    throw new RangeError('CDP port reservation timeout must be a positive integer');
  }
  // Chromium reads the switch before app.ready. A Node worker can bind a
  // temporary socket while the main thread waits, without spawning Electron
  // again on the cold-start path. The socket is closed before publishing it.
  const state = new Int32Array(new SharedArrayBuffer(2 * Int32Array.BYTES_PER_ELEMENT));
  const worker = new Worker(`(${reservePortInWorker.toString()})()`, {
    eval: true,
    workerData: state.buffer,
    execArgv: [],
  });
  worker.on('error', () => {});
  worker.unref();
  try {
    if (Atomics.wait(state, 0, 0, timeoutMs) === 'timed-out') {
      throw new Error('Timed out reserving a non-zero loopback CDP port');
    }
    const port = Atomics.load(state, 1);
    if (Atomics.load(state, 0) !== 1 || port < 1 || port > 65535) {
      throw new Error('Failed to reserve a non-zero loopback CDP port');
    }
    return port;
  } finally {
    void worker.terminate().catch(() => {});
  }
}

function readCdpTargets(port, timeoutMs) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(error);
      else resolve(value);
    };
    const request = http.get({ host: '127.0.0.1', port, path: '/json/list', agent: false }, response => {
      response.on('error', error => finish(error));
      if (response.statusCode !== 200) {
        finish(new Error(`CDP target list returned HTTP ${response.statusCode}`));
        request.destroy();
        return;
      }
      response.setEncoding('utf8');
      let body = '';
      let size = 0;
      response.on('data', chunk => {
        size += Buffer.byteLength(chunk);
        if (size > 1024 * 1024) {
          request.destroy(new Error('CDP target list exceeds the response limit'));
          return;
        }
        body += chunk;
      });
      response.on('end', () => {
        try { finish(null, JSON.parse(body)); } catch (error) { finish(error); }
      });
    });
    // An absolute deadline also bounds a listener that accepts but never replies.
    const timer = setTimeout(() => request.destroy(new Error('CDP target request timed out')), timeoutMs);
    request.on('error', error => finish(error));
  });
}

async function waitForOwnedCdpTarget({ port, targetId, timeoutMs = 5_000, retryIntervalMs = 100 }) {
  if (!Number.isInteger(port) || port < 1 || port > 65535 || !targetId) {
    throw new Error('A non-zero CDP port and an owned target ID are required');
  }
  const deadline = Date.now() + timeoutMs;
  let lastError = new Error('CDP endpoint has not become ready');
  while (Date.now() < deadline) {
    try {
      const targets = await readCdpTargets(port, Math.min(500, Math.max(1, deadline - Date.now())));
      if (Array.isArray(targets) && targets.some(target => target?.type === 'page' && target.id === targetId)) {
        return;
      }
      lastError = new Error('CDP endpoint does not contain this Electron window');
    } catch (error) {
      lastError = error;
    }
    const remaining = deadline - Date.now();
    if (remaining > 0) await new Promise(resolve => setTimeout(resolve, Math.min(retryIntervalMs, remaining)));
  }
  throw new Error(`Unable to verify this Electron CDP endpoint: ${lastError.message}`);
}

module.exports = { reserveLoopbackPortSync, waitForOwnedCdpTarget };
