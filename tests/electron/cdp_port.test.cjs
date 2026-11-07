const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const net = require('node:net');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { createRequire } = require('node:module');
const vm = require('node:vm');
const { reserveLoopbackPortSync, waitForOwnedCdpTarget } = require('../../jiuwenswarm/channels/desktop/electron/cdp_port.cjs');

const mainPath = path.join(__dirname, '../../jiuwenswarm/channels/desktop/electron/main.cjs');
const mainRequire = createRequire(mainPath);

function listen(server, port = 0) {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen({ host: '127.0.0.1', port, exclusive: true }, resolve);
  });
}

function loadStartup({ packaged = true, port = '', reservePort = reserveLoopbackPortSync,
  waitForTarget = waitForOwnedCdpTarget } = {}) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'workswarm-cdp-test-'));
  const switches = [];
  const descriptors = [];
  const app = {
    isPackaged: packaged,
    commandLine: { appendSwitch: (...args) => switches.push(args) },
    getPath: () => home,
    getVersion: () => 'test',
    requestSingleInstanceLock: () => false,
    quit: () => {},
    on: () => {},
  };
  const context = vm.createContext({
    __dirname: path.dirname(mainPath),
    Buffer,
    URL,
    URLSearchParams,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    console: { ...console, log: () => {}, error: () => {} },
    process: {
      env: { JIUWENSWARM_ELECTRON_CDP_PORT: port },
      argv: ['electron', mainPath],
      platform: process.platform,
      pid: process.pid,
      execPath: process.execPath,
      on: () => {},
      once: () => {},
    },
    require: name => {
      if (name === 'electron') return { app };
      if (name === './cdp_port.cjs') return { reserveLoopbackPortSync: reservePort, waitForOwnedCdpTarget: waitForTarget };
      if (name === 'node:fs') {
        return {
          ...fs,
          openSync: (...args) => {
            const descriptor = fs.openSync(...args);
            descriptors.push(descriptor);
            return descriptor;
          },
        };
      }
      return mainRequire(name);
    },
  });
  const close = () => {
    vm.runInContext('stopBrowserEndpointsPublisher(); browserTargetResolver?.server.close();', context);
    descriptors.forEach(descriptor => fs.closeSync(descriptor));
    fs.rmSync(home, { recursive: true, force: true });
  };
  try {
    vm.runInContext(fs.readFileSync(mainPath, 'utf8'), context, { filename: mainPath });
    const api = vm.runInContext(`({
      setWindow(targetId) {
        mainWindow = { isDestroyed: () => false,
          webContents: { getOrCreateDevToolsTargetId: () => targetId } };
      },
      ready: () => hasCdp,
      confirm: initializeBrowserCdp,
      async startResolver() { browserTargetResolver = await startBrowserTargetResolver(); },
      publish: startBrowserEndpointsPublisher,
      payload: buildBrowserEndpointsPayload,
      endpointsPath: browserEndpointsFilePath()
    })`, context);
    return { switches, context, api, close };
  } catch (error) {
    descriptors.forEach(descriptor => fs.closeSync(descriptor));
    fs.rmSync(home, { recursive: true, force: true });
    throw error;
  }
}

function runStartup({ inspect = () => {}, ...options } = {}) {
  const startup = loadStartup(options);
  try {
    inspect(startup.context);
    return startup.switches;
  } finally {
    startup.close();
  }
}

async function targetServer(t, handler) {
  const server = http.createServer(handler);
  await listen(server);
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  return server.address().port;
}

test('allocated port is non-zero, skips a busy socket and is released for Chromium', async t => {
  const occupied = net.createServer();
  await listen(occupied);
  t.after(() => new Promise(resolve => occupied.close(resolve)));
  const port = reserveLoopbackPortSync();
  assert.ok(Number.isInteger(port) && port > 0 && port <= 65535);
  assert.notEqual(port, occupied.address().port);
  const chromiumSocket = net.createServer();
  await listen(chromiumSocket, port);
  t.after(() => new Promise(resolve => chromiumSocket.close(resolve)));
});

test('packaged startup passes a real non-zero port to Chromium before ready', () => {
  const switches = runStartup();
  assert.deepEqual(switches.map(entry => entry[0]), ['remote-debugging-address', 'remote-debugging-port']);
  assert.equal(switches[0][1], '127.0.0.1');
  assert.ok(Number(switches[1][1]) > 0);
});

test('selecting a port does not mark an unverified Chromium endpoint as ready', () => {
  let ready;
  runStartup({ inspect: context => { ready = vm.runInContext('hasCdp', context); } });
  assert.equal(ready, false);
});

test('a port claimed by another CDP browser never enables or publishes the endpoint', async t => {
  const port = await targetServer(t, (_request, response) => {
    response.end(JSON.stringify([{ id: 'another-browser', type: 'page' }]));
  });
  for (const explicit of [false, true]) {
    // Simulate a listener claiming the selected port before Chromium binds.
    const startup = loadStartup({
      port: explicit ? String(port) : '',
      reservePort: () => port,
      waitForTarget: options => waitForOwnedCdpTarget({ ...options, timeoutMs: 60, retryIntervalMs: 5 }),
    });
    try {
      startup.api.setWindow('this-window');
      assert.equal(await startup.api.confirm(), false);
      assert.equal(startup.api.ready(), false);
      await assert.rejects(startup.api.startResolver(), /has not been verified/);
      startup.api.publish();
      assert.equal(fs.existsSync(startup.api.endpointsPath), false);
      assert.throws(() => startup.api.payload(), /has not been verified/);
    } finally {
      startup.close();
    }
  }
});

test('only this window becoming visible in CDP enables endpoint publication', async t => {
  let ready = false;
  let queried;
  const firstQuery = new Promise(resolve => { queried = resolve; });
  const port = await targetServer(t, (_request, response) => {
    response.end(JSON.stringify(ready ? [{ id: 'this-window', type: 'page' }] : []));
    queried();
  });
  const startup = loadStartup({ port: String(port) });
  try {
    startup.api.setWindow('this-window');
    const confirming = startup.api.confirm();
    await firstQuery;
    assert.equal(startup.api.ready(), false);
    startup.api.publish();
    assert.equal(fs.existsSync(startup.api.endpointsPath), false);
    ready = true;
    assert.equal(await confirming, true);
    await startup.api.startResolver();
    startup.api.publish();
    const payload = JSON.parse(fs.readFileSync(startup.api.endpointsPath, 'utf8'));
    assert.equal(payload.cdp_endpoint, `http://127.0.0.1:${port}`);
    assert.ok(payload.target_resolver.startsWith('http://127.0.0.1:'));
  } finally {
    startup.close();
  }
});

test('a listener that accepts connections but never replies cannot hang verification', { timeout: 2000 }, async t => {
  const port = await targetServer(t, () => {});
  await assert.rejects(waitForOwnedCdpTarget({ port, targetId: 'this-window', timeoutMs: 60 }), /timed out/);
});

test('HTTP errors, malformed JSON and non-page targets cannot establish ownership', async t => {
  for (const result of [{ status: 503, body: 'unavailable' }, { status: 200, body: '<html>' },
    { status: 200, body: JSON.stringify([{ id: 'this-window', type: 'service_worker' }]) }]) {
    const port = await targetServer(t, (_request, response) => {
      response.writeHead(result.status);
      response.end(result.body);
    });
    await assert.rejects(waitForOwnedCdpTarget({ port, targetId: 'this-window', timeoutMs: 30,
      retryIntervalMs: 5 }), /Unable to verify/);
  }
});

test('explicit non-zero ports are preserved without allocating another socket', () => {
  for (const packaged of [true, false]) {
    const switches = runStartup({
      packaged,
      port: '58167',
      reservePort: () => { throw new Error('must not allocate'); },
    });
    assert.equal(switches[1][1], '58167');
  }
});

test('allocation failure leaves CDP disabled instead of falling back to port zero', () => {
  const switches = runStartup({ reservePort: () => { throw new Error('socket unavailable'); } });
  assert.deepEqual(switches, []);
});

test('development still requires its launcher to supply a non-zero port', () => {
  assert.throws(() => runStartup({ packaged: false }), /valid loopback CDP port/);
});

test('both installers include the CDP allocator and require it in the bundle', () => {
  for (const script of ['build-electron-exe.ps1', 'build-electron-exe.sh']) {
    const source = fs.readFileSync(path.join(__dirname, '../../scripts', script), 'utf8');
    assert.match(source, /(?:Copy-Item|cp).*cdp_port\.cjs/);
    assert.equal(source.match(/cdp_port\.cjs/g).length, 2);
  }
});
