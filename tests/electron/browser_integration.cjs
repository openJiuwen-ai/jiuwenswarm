// Run with SWARM_TEST_MODULES pointing to node_modules containing @playwright/mcp@0.0.78.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { spawn, execFile, execFileSync } = require('node:child_process');
const readline = require('node:readline');
const { once } = require('node:events');
const { panelIdentity } = require('../../jiuwenswarm/channels/desktop/electron/browser_panels.cjs');

const repo = path.resolve(__dirname, '../..');
const desktopDir = path.join(repo, 'jiuwenswarm/channels/desktop/electron');
const modules = process.env.SWARM_TEST_MODULES;
if (!modules) throw new Error('Set SWARM_TEST_MODULES to the pinned MCP node_modules directory');
const { _electron } = require(path.join(modules, 'playwright'));
const { build } = require(path.join(repo, 'jiuwenswarm/channels/web/frontend/node_modules/esbuild'));
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'swarm-browser-integration-'));
const profile = path.join(scratch, 'profile');
const children = new Set();

function mcp(env) {
  const child = spawn(process.execPath, [path.join(desktopDir, 'target_mcp_wrapper.cjs')], {
    env: { ...process.env, ...env, PATH: `${path.join(modules, '.bin')}${path.delimiter}${process.env.PATH}`,
      PLAYWRIGHT_MCP_DIAGNOSTIC_LOG: path.join(scratch, 'mcp.log') },
    cwd: scratch, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
  });
  children.add(child);
  let id = 0;
  let stderr = '';
  const pending = new Map();
  child.stderr.on('data', data => { stderr += data; });
  readline.createInterface({ input: child.stdout }).on('line', line => {
    const message = JSON.parse(line);
    const request = pending.get(message.id);
    if (request) {
      pending.delete(message.id);
      clearTimeout(request.timer);
      if (message.error) request.reject(new Error(JSON.stringify(message.error)));
      else request.resolve(message.result);
    }
  });
  child.on('exit', code => {
    children.delete(child);
    for (const request of pending.values()) {
      clearTimeout(request.timer);
      request.reject(new Error(`MCP exited ${code}: ${stderr}`));
    }
    pending.clear();
  });
  const request = (method, params = {}) => new Promise((resolve, reject) => {
    const requestId = ++id;
    const timer = setTimeout(() => { pending.delete(requestId); reject(new Error(`MCP timeout: ${method}; ${stderr}`)); }, 45000);
    pending.set(requestId, { resolve, reject, timer });
    child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: requestId, method, params })}\n`);
  });
  return {
    request,
    async init() {
      await request('initialize', { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'regression-test', version: '1' } });
      child.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n');
    },
    async tool(name, args) {
      const result = await request('tools/call', { name, arguments: args });
      assert.ok(!result.isError, JSON.stringify(result));
      return result;
    },
    async close() { const exited = once(child, 'exit'); child.stdin.end(); await exited; },
  };
}

async function main() {
  await build({ entryPoints: [path.join(__dirname, 'browser_fixture.tsx')], bundle: true,
    outfile: path.join(scratch, 'ui.js'), jsx: 'automatic', absWorkingDir: path.join(repo, 'jiuwenswarm/channels/web/frontend'),
    nodePaths: [path.join(repo, 'jiuwenswarm/channels/web/frontend/node_modules')], define: { 'process.env.NODE_ENV': '"development"' } });
  const server = http.createServer(async (req, res) => {
    if (req.url === '/browser-diagnostics') {
      const views = await electron.evaluate(async () => ({
        layout: global.browserTest.layoutState(),
        views: await Promise.all([...global.browserTest.browserViews.values()].map(async entry => ({
          panelId: entry.panelId, loaded: entry.loaded, visible: entry.visible,
          bounds: entry.view.getBounds(), viewportSize: entry.viewportSize,
          actual: await entry.view.webContents.executeJavaScript('({width:innerWidth,height:innerHeight})'),
        }))),
      }));
      res.setHeader('Content-Type', 'application/json');
      return res.end(JSON.stringify(views));
    }
    if (req.url === '/ui.js' || req.url === '/ui.css') {
      res.setHeader('Content-Type', req.url.endsWith('.js') ? 'text/javascript' : 'text/css');
      return res.end(fs.readFileSync(path.join(scratch, req.url.slice(1))));
    }
    res.setHeader('Content-Type', 'text/html');
    if (req.url === '/login') res.setHeader('Set-Cookie', 'server-login=synthetic-account; Path=/; Max-Age=3600; HttpOnly; SameSite=Lax');
    if (req.url === '/') return res.end(`<!doctype html><html><head><title>Panel UI</title><link rel="stylesheet" href="/ui.css"><style>
      :root{--color-surface-page:#fff;--color-surface-card:#f5f6f8;--color-border-default:#cdd0d5;--color-text-primary:#202124;--color-text-secondary:#454b54;--color-text-tertiary:#777;--color-action-primary:#168478;--color-action-secondary:#e6e8ec}
      body{margin:0;font-family:Arial}#root{display:flex;height:100vh}</style></head><body><div id="root"></div><script src="/ui.js"></script></body></html>`);
    res.end('<!doctype html><title>Browser fixture</title><h1>Local browser fixture</h1><input aria-label="Query"><button onclick="document.querySelector(\'h1\').textContent=document.querySelector(\'input\').value">Submit</button>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const portServer = http.createServer();
  await new Promise(resolve => portServer.listen(0, '127.0.0.1', resolve));
  const cdpPort = portServer.address().port;
  await new Promise(resolve => portServer.close(resolve));
  let electron;
  try {
    const launchEnv = { ...process.env, SWARM_TEST_PROFILE: profile,
      SWARM_TEST_URL: base, JIUWENSWARM_ELECTRON_CDP_PORT: String(cdpPort) };
    delete launchEnv.ELECTRON_RUN_AS_NODE;
    const launch = () => _electron.launch({
      executablePath: require(path.join(desktopDir, 'node_modules/electron')),
      args: [path.join(__dirname, 'browser_fixture.cjs')],
      env: launchEnv,
      timeout: 30000,
    });
    electron = await launch();
    const ui = await electron.firstWindow();
    await ui.getByTestId('desktop-browser-tabs').waitFor();
    const payload = await electron.evaluate(() => global.browserTest.endpoints);
    await electron.evaluate(async ({ session }, base) => {
      await session.defaultSession.cookies.set({ url: base, name: 'trusted-ui-cookie', value: 'synthetic-ui-only' });
    }, base);
    const env = { ...payload.env_json, PLAYWRIGHT_MCP_SESSION_ID: 'session-one' };
    const alice = mcp({ ...env, PLAYWRIGHT_MCP_MEMBER_ID: 'alice', PLAYWRIGHT_MCP_PANEL_LABEL: 'Alice' });
    const bob = mcp({ ...env, PLAYWRIGHT_MCP_MEMBER_ID: 'bob', PLAYWRIGHT_MCP_PANEL_LABEL: 'Bob' });
    await alice.init();
    await bob.init();
    const tools = await alice.request('tools/list');
    for (const name of ['browser_run_code', 'browser_run_code_unsafe', 'browser_snapshot']) {
      assert.ok(tools.tools.some(tool => tool.name === name), `Missing SDK tool ${name}`);
    }
    await alice.tool('browser_navigate', { url: `${base}/alice` });
    await bob.tool('browser_navigate', { url: `${base}/bob` });
    const viewport = await alice.tool('browser_run_code', { code: 'async page => page.evaluate(() => innerWidth > 0 && innerHeight > 0)' });
    assert.match(JSON.stringify(viewport), /Result\\ntrue/);
    await alice.tool('browser_run_code_unsafe', { code: "async page => { await page.getByRole('textbox', { name: 'Query' }).fill('Alice result'); await page.getByRole('button', { name: 'Submit' }).click(); return await page.locator('h1').innerText(); }" });
    const bobResult = await bob.tool('browser_run_code', { code: "async page => ({url: page.url(), text: await page.locator('h1').innerText(), pages: page.context().pages().length, contexts: page.context().browser().contexts().length})" });
    assert.match(JSON.stringify(bobResult), /Local browser fixture/);
    assert.doesNotMatch(JSON.stringify(bobResult), /Alice result/);
    assert.match(JSON.stringify(bobResult), /pages\\?":1/);
    const python = path.join(repo, '.venv/Scripts/python.exe');
    const probeOutput = execFileSync(python, ['-c', 'import json; from openjiuwen.harness.tools.browser_move.playwright_runtime.probes import build_browser_state_metadata_js, build_interactive_probe_js; print(json.dumps([build_browser_state_metadata_js(), build_interactive_probe_js()]))'], { cwd: repo, encoding: 'utf8', windowsHide: true });
    const probes = JSON.parse(probeOutput.trim().split(/\r?\n/).at(-1));
    for (const code of probes) await alice.tool('browser_run_code_unsafe', { code });
    await alice.tool('browser_navigate', { url: `${base}/login` });
    const browserCookies = JSON.stringify(await bob.tool('browser_run_code', { code: 'async page => page.context().cookies()' }));
    assert.match(browserCookies, /server-login/);
    assert.doesNotMatch(browserCookies, /trusted-ui-cookie/);
    await bob.tool('browser_run_code', { code: 'async page => page.context().clearCookies({name: /^server-login$/})' });
    assert.doesNotMatch(JSON.stringify(await alice.tool('browser_run_code', { code: 'async page => page.context().cookies()' })), /server-login/);
    await alice.tool('browser_run_code_unsafe', { code: `async page => { await page.context().addCookies([{name:'test-login',value:'synthetic-account',url:'${base}',expires:Date.now()/1000+3600}]); await page.evaluate(() => localStorage.setItem('test-login','synthetic-account')); return 'ok'; }` });
    const cookie = await bob.tool('browser_run_code', { code: "async page => page.evaluate(() => ({cookie: document.cookie, storage: localStorage.getItem('test-login')}))" });
    assert.match(JSON.stringify(cookie), /test-login=synthetic-account/);
    const next = mcp({ ...env, PLAYWRIGHT_MCP_SESSION_ID: 'session-two' });
    await next.init();
    await next.tool('browser_navigate', { url: `${base}/next-session` });
    assert.match(JSON.stringify(await next.tool('browser_run_code', { code: 'async page => page.evaluate(() => document.cookie)' })), /test-login=synthetic-account/);
    await alice.tool('browser_navigate', { url: `${base}/alice` });
    await ui.getByRole('tab', { name: 'Alice', exact: true }).click();
    await ui.waitForFunction(url => document.querySelector('[data-testid="desktop-browser-address"]').value === url, `${base}/alice`);
    assert.equal(await ui.getByTestId('desktop-browser-address').inputValue(), `${base}/alice`);
    await ui.getByRole('tab', { name: 'Bob', exact: true }).click();
    await ui.waitForFunction(url => document.querySelector('[data-testid="desktop-browser-address"]').value === url, `${base}/bob`);
    assert.equal(await ui.getByTestId('desktop-browser-address').inputValue(), `${base}/bob`);
    for (const width of [1100, 540]) {
      await electron.evaluate(({ BrowserWindow }, width) => BrowserWindow.getAllWindows()[0].setSize(width, 780), width);
      await ui.getByTestId('desktop-browser-address').waitFor();
      assert.equal(await ui.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      await ui.screenshot({ path: path.join(scratch, `panels-${width}.png`) });
      const image = await electron.evaluate(async (_electron, id) => {
        const view = global.browserTest.browserViews.get(id).view;
        const image = await view.webContents.capturePage();
        if (image.isEmpty()) throw new Error('Empty native page capture');
        return image.toPNG().toString('base64');
      }, panelIdentity('session-one', 'bob').panelId);
      fs.writeFileSync(path.join(scratch, `page-${width}.png`), Buffer.from(image, 'base64'));
    }
    const ids = [panelIdentity('session-one', 'alice').panelId, panelIdentity('session-one', 'bob').panelId];
    await electron.evaluate(async (_electron, ids) => {
      for (let i = 0; i < 10; i++) await global.browserTest.ensureBrowserView(`idle-${i}`);
      for (const id of ids) if (!global.browserTest.browserViews.has(id)) throw new Error(`Evicted busy panel ${id}`);
    }, ids);
    await alice.close();
    await bob.tool('browser_snapshot', {});
    await bob.close();
    await next.close();
    // New background sessions must remain usable when the parent has no viewport.
    if (process.platform === 'win32') {
      await electron.evaluate(async ({ BrowserWindow }) => {
        const window = BrowserWindow.getAllWindows()[0];
        if (!window.isMinimized()) {
          await new Promise(resolve => {
            window.once('minimize', resolve);
            window.minimize();
          });
        }
      });
    }
    const sdkOutput = await new Promise((resolve, reject) => {
      const child = execFile(python, ['-m', 'tests.electron.sdk_binding_smoke'], {
        cwd: repo, encoding: 'utf8', windowsHide: true, timeout: process.env.SWARM_TEST_CHROME ? 180000 : 90000,
        env: { ...process.env, JIUWENSWARM_DATA_DIR: path.join(scratch, 'sdk-data') },
      }, (error, stdout, stderr) => {
        fs.writeFileSync(path.join(scratch, 'sdk.stdout.log'), stdout);
        fs.writeFileSync(path.join(scratch, 'sdk.stderr.log'), stderr);
        if (error) {
          const diagnostics = stdout.split(/\r?\n/).filter(line => line.startsWith('SDK_')).join('\n');
          reject(new Error(`${error.message.split('\n')[0]}\n${stderr.slice(0, 3000)}\n${diagnostics}\nArtifacts: ${scratch}`));
        } else resolve(stdout);
      });
      child.stdin.end(JSON.stringify({ node: process.execPath, wrapper: path.join(desktopDir, 'target_mcp_wrapper.cjs'),
        chrome: process.env.SWARM_TEST_CHROME,
        cwd: scratch, base, env: { ...payload.env_json, PATH: `${path.join(modules, '.bin')}${path.delimiter}${process.env.PATH}`,
          PLAYWRIGHT_MCP_DIAGNOSTIC_LOG: path.join(scratch, 'sdk-mcp.log') } }));
    });
    assert.match(sdkOutput, /SDK_BINDINGS_OK/);
    assert.match(sdkOutput, /SDK_SINGLE_SESSION_ISOLATION_OK/);
    if (process.env.SWARM_TEST_CHROME) {
      assert.match(sdkOutput, /SDK_SWARM_MANAGED_HEADLESS_OK/);
      assert.match(sdkOutput, /SDK_SWARM_MANAGED_HEADED_OK/);
      console.log('PASS: Swarm external Chrome, headed/headless, isolated members, Electron coexistence and port cleanup.');
    }
    await electron.evaluate(async ({ session }) => {
      const shared = session.fromPartition('persist:jiuwenswarm-browser');
      shared.flushStorageData();
      await shared.cookies.flushStore();
      global.browserTest.stop();
    });
    await electron.close();
    electron = await launch();
    await electron.firstWindow();
    const cookies = await electron.evaluate(async ({ session }) => session.fromPartition('persist:jiuwenswarm-browser').cookies.get({ name: 'test-login' }));
    assert.equal(cookies[0].value, 'synthetic-account');
    console.log(`PASS: real Electron + MCP tools, SDK probes, concurrent single-agent sessions and members, shared cross-session cookies/storage, UI switching, busy eviction, persistent login. Artifacts: ${scratch}`);
  } finally {
    for (const child of children) child.kill();
    if (electron) await electron.close().catch(() => {});
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
