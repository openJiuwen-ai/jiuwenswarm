const { app, BrowserWindow, dialog, ipcMain, net, session, shell, WebContentsView } = require('electron');
const { spawn } = require('node:child_process');
const { inspect } = require('node:util');
const fs = require('node:fs/promises');
const fsSync = require('node:fs');
const nodeHttp = require('node:http');
const nodeNet = require('node:net');
const path = require('node:path');
const { Readable } = require('node:stream');
const { pipeline } = require('node:stream/promises');

const BACKEND_HOST = '127.0.0.1';
const FRONTEND_HOST = '127.0.0.1';
// Port-group layout mirrors jiuwenswarm.instance_manager.config.BASE_PORTS:
// one instance index occupies all four ports (+ index * 1000).
const BASE_PORTS = { agentServer: 18092, gatewayApi: 19000, gatewayInternal: 19001, frontend: 5173 };
const STARTUP_TIMEOUT_MS = 45_000;
const isFrontendOnly = process.env.JIUWENSWARM_ELECTRON_FRONTEND_ONLY === '1' || (() => {
  try { return fsSync.existsSync(path.join(__dirname, '.frontend-only')); } catch { return false; }
})();
// 打包正式版不开放受信渲染器的 DevTools（会削弱 target 隔离的信任边界）；
// 测试构建（构建脚本写入 .test marker）与开发模式保留。
const isTestBuild = (() => {
  try { return fsSync.existsSync(path.join(__dirname, '.test')); } catch { return false; }
})();
const FRONTEND_ONLY_BACKEND_HOST = '127.0.0.1';
const FRONTEND_ONLY_BACKEND_PORT = 19000;
const CDP_TARGET_TIMEOUT_MS = 10_000;
const BROWSER_VIEW_MAX_CRASHES = 5;
const SERVICE_SHUTDOWN_TIMEOUT_MS = 5_000;
const SERVICE_KILL_TIMEOUT_MS = 1_500;
const DEFAULT_BROWSER_URL = 'https://cn.bing.com/';
const PLAYWRIGHT_MCP_PACKAGE = '@playwright/mcp@0.0.78';
const TARGET_MCP_WRAPPER_PATH = path.join(__dirname, 'target_mcp_wrapper.cjs');
const PNG_DATA_URL_PREFIX = 'data:image/png;base64,';
const VITE_DEV_MODE = process.argv.includes('--vite-dev');
const ELECTRON_CDP_PORT = Number.parseInt(process.env.JIUWENSWARM_ELECTRON_CDP_PORT || '', 10);
let cdpPort = Number.isInteger(ELECTRON_CDP_PORT) && ELECTRON_CDP_PORT >= 1 && ELECTRON_CDP_PORT <= 65535
  ? ELECTRON_CDP_PORT
  : 0;

function isPortAvailable(port) {
  return new Promise(resolve => {
    const tester = nodeNet.createServer();
    tester.once('error', () => resolve(false));
    tester.once('listening', () => {
      tester.close(() => resolve(true));
    });
    tester.listen({ host: BACKEND_HOST, port, exclusive: true });
  });
}

async function findAvailablePorts(scanRange = 20) {
  for (let i = 0; i < scanRange; i++) {
    const offset = i * 1000;
    const ports = Object.fromEntries(
      Object.entries(BASE_PORTS).map(([name, base]) => [name, base + offset]),
    );
    let groupAvailable = true;
    for (const port of Object.values(ports)) {
      if (!(await isPortAvailable(port))) {
        groupAvailable = false;
        break;
      }
    }
    if (groupAvailable) return ports;
  }
  throw new Error(`No available port group within scan_range=${scanRange}`);
}

// 打包版 CDP 端口获取：--remote-debugging-port=0 让 Chromium 自选临时端口并
// 自行绑定（无"预留给竞态留窗口"，也无需同步 spawn 自身 exe 预留——后者冷启动
// 会被杀软扫描拖到数秒且完全串行阻塞首屏），选定的端口由 Chromium 写入用户
// 数据目录的 DevToolsActivePort 文件，首行即端口号。
const DEVTOOLS_ACTIVE_PORT_FILENAME = 'DevToolsActivePort';
const CDP_PORT_FILE_TIMEOUT_MS = 5_000;
// packaged port=0 模式标记；轮询在 whenReady 后才启动，避免极端冷启动下
// Chromium 初始化时间吃掉超时预算（文件通常在 ready 前就已写好）。
let cdpPortPending = false;
let cdpPortResolution = null;

function devToolsActivePortPath() {
  return path.join(app.getPath('userData'), DEVTOOLS_ACTIVE_PORT_FILENAME);
}

function removeStaleDevToolsActivePortFile() {
  // 文件跨启动残留；在 Chromium 重写前删掉，避免读到上一次的陈旧端口。
  try {
    fsSync.rmSync(devToolsActivePortPath(), { force: true });
  } catch { /* best effort */ }
}

async function resolveCdpPortFromDevToolsActivePort() {
  const file = devToolsActivePortPath();
  const deadline = Date.now() + CDP_PORT_FILE_TIMEOUT_MS;
  while (Date.now() < deadline) {
    try {
      const content = fsSync.readFileSync(file, 'utf8');
      const port = Number.parseInt(content.split('\n')[0], 10);
      if (Number.isInteger(port) && port >= 1 && port <= 65535) return port;
    } catch { /* not written yet */ }
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  return 0;
}

let hasCdp = cdpPort > 0;

function mainLogPath() {
  return path.join(app.getPath('home'), '.jiuwenswarm', 'logs', 'electron-main.log');
}

function installMainConsoleTee() {
  // 双击启动的 GUI 应用没有控制台，主进程日志（含启动失败的完整原因）会全部
  // 丢失——首次冷启动子进程崩溃的诊断曾因此只剩旁证。打包模式把 console 输出
  // 同步落盘到与子服务日志同目录的 electron-main.log（对应 Python 桌面的
  // desktop.log）。dev 模式保持原样。
  if (!app.isPackaged) return;
  try {
    fsSync.mkdirSync(path.dirname(mainLogPath()), { recursive: true });
    const fd = fsSync.openSync(mainLogPath(), 'a');
    const format = args => args
      .map(arg => (typeof arg === 'string' ? arg : inspect(arg, { depth: 4 })))
      .join(' ');
    const append = (level, args) => {
      try {
        fsSync.appendFileSync(fd, `${new Date().toISOString()} ${level} ${format(args)}\n`);
      } catch { /* 日志失败绝不影响主流程 */ }
    };
    const originalLog = console.log.bind(console);
    const originalError = console.error.bind(console);
    console.log = (...args) => { append('INFO', args); originalLog(...args); };
    console.error = (...args) => { append('ERROR', args); originalError(...args); };
    console.log(`[electron] === main start pid=${process.pid} version=${app.getVersion()} argv=${JSON.stringify(process.argv)} ===`);
  } catch { /* best effort */ }
}

if (app.isPackaged) {
  if (hasCdp) {
    app.commandLine.appendSwitch('remote-debugging-address', BACKEND_HOST);
    app.commandLine.appendSwitch('remote-debugging-port', String(cdpPort));
  } else {
    removeStaleDevToolsActivePortFile();
    app.commandLine.appendSwitch('remote-debugging-address', BACKEND_HOST);
    app.commandLine.appendSwitch('remote-debugging-port', '0');
    // 先乐观启用 sideview；端口文件超时未出现时降级禁用（沿用预留失败语义）。
    hasCdp = true;
    cdpPortPending = true;
  }
} else {
  if (!hasCdp) {
    throw new Error('Electron must be started through launch.cjs with a valid loopback CDP port');
  }
  app.commandLine.appendSwitch('remote-debugging-address', BACKEND_HOST);
  app.commandLine.appendSwitch('remote-debugging-port', String(cdpPort));
}

installMainConsoleTee();

let mainWindow = null;
// 每会话一个隔离的浏览上下文：惰性创建、独立 partition（cookie/存储互不可见）。
// 同一时刻只显示当前会话的视图；Browser Agent 经 target resolver 绑定本会话视图。
const browserViews = new Map();
const MAX_BROWSER_SESSION_VIEWS = 8;
// 每会话最后浏览的页面 URL：视图被 LRU 回收后重建时还原用。持久化到
// userData/browser-session-urls.json，重启后同样还原（防抖 2s 落盘 + 退出冲刷）。
const sessionLastUrls = new Map();
const BROWSER_SESSION_URLS_FILENAME = 'browser-session-urls.json';
const SESSION_URLS_SAVE_DEBOUNCE_MS = 2_000;
let sessionUrlsSaveTimer = null;
let activePaneSessionId = '';
let lastBrowserBounds = null;
let browserTargetResolver = null;
let currentFrontendUrl = '';
let shuttingDown = false;
let shutdownComplete = false;
let shutdownPromise = null;
let requestedExitCode = 0;
const serviceProcesses = new Map();
// Resolved session port group (null before startWebService / FrontendOnly mode).
let sessionPorts = null;

function repositoryRoot() {
  return path.resolve(__dirname, '..', '..', '..', '..');
}

function backendExecutable() {
  // Backend exe name follows build_config.executable_name; probe current name
  // first and keep the legacy name as fallback so older bundles still launch.
  const candidates = process.platform === 'win32'
    ? ['workswarm.exe', 'jiuwenswarm.exe']
    : ['workswarm', 'jiuwenswarm'];
  const backendDir = path.join(process.resourcesPath, 'backend');
  for (const executableName of candidates) {
    const candidate = path.join(backendDir, executableName);
    if (fsSync.existsSync(candidate)) return candidate;
  }
  return path.join(backendDir, candidates[0]);
}

function serviceWorkingDirectory() {
  return app.isPackaged ? path.dirname(backendExecutable()) : repositoryRoot();
}

function frontendDir() {
  return path.join(__dirname, '..', '..', 'web', 'frontend');
}

const logStreams = new Map();

function logStreamFor(name) {
  if (!app.isPackaged) return 'inherit';
  if (logStreams.has(name)) return logStreams.get(name);
  const logDir = path.join(app.getPath('home'), '.jiuwenswarm', 'logs');
  const logFile = path.join(logDir, `electron-${name}.log`);
  let fd;
  try {
    fsSync.mkdirSync(logDir, { recursive: true });
    fd = fsSync.openSync(logFile, 'a');
  } catch (error) {
    console.error(`[electron] failed to open ${name} service log, falling back to 'ignore'`, error);
    return 'ignore';
  }
  logStreams.set(name, fd);
  console.log(`[electron] ${name} service log: ${logFile}`);
  return fd;
}

function serviceCommand(name, extraArgs = []) {
  if (app.isPackaged) {
    const flags = { web: '--desktop-run-web', agent: '--desktop-run-agent', gateway: '--desktop-run-gateway' };
    const flag = flags[name];
    if (!flag) throw new Error(`Unknown service name: ${name}`);
    return { command: backendExecutable(), args: [flag, ...extraArgs] };
  }

  if (name === 'web' && VITE_DEV_MODE) {
    const viteBin = path.join(frontendDir(), 'node_modules', 'vite', 'bin', 'vite.js');
    return { command: process.execPath, args: [viteBin] };
  }
  const moduleNames = {
    agent: 'jiuwenswarm.server.app_agentserver',
    gateway: 'jiuwenswarm.gateway.app_gateway',
    web: 'jiuwenswarm.channels.web.app_web',
  };
  const moduleName = moduleNames[name];
  if (!moduleName) throw new Error(`Unknown service name: ${name}`);
  return {
    command: 'uv',
    args: ['run', 'python', '-m', moduleName, ...extraArgs],
  };
}

function spawnService(name, ports, extraArgs = []) {
  const { command, args } = serviceCommand(name, extraArgs);
  const env = {
    ...process.env,
    JIUWENSWARM_DESKTOP: '1',
    JIUWENSWARM_ELECTRON: '1',
  };
  if (browserTargetResolver) {
    const targetMcpDiagnosticLog = path.join(
      app.getPath('home'),
      '.jiuwenswarm',
      'agent',
      '.logs',
      'target_mcp_wrapper.log',
    );
    env.BROWSER_DRIVER = 'remote';
    env.BROWSER_SHARED_CONTROL = '1';
    env.PLAYWRIGHT_MCP_CDP_ENDPOINT = `http://${BACKEND_HOST}:${cdpPort}`;
    // 每会话隔离：不固定全局 TargetID。Python 侧按会话请求 resolver 拿到本会话
    // 视图的 TargetID，再注入该会话 MCP 配置的 env（原静态 TARGET_ID 通道废弃）。
    env.PLAYWRIGHT_MCP_TARGET_RESOLVER = `http://${BACKEND_HOST}:${browserTargetResolver.port}`;
    env.PLAYWRIGHT_MCP_DIAGNOSTIC_LOG = targetMcpDiagnosticLog;
    // openjiuwen intentionally forwards only an allowlisted MCP subprocess
    // environment. Use its supported extension map so the target adapter sees
    // the exact TargetID as well as the Electron CDP endpoint.
    env.PLAYWRIGHT_MCP_ENV_JSON = JSON.stringify({
      PLAYWRIGHT_MCP_CDP_ENDPOINT: env.PLAYWRIGHT_MCP_CDP_ENDPOINT,
      PLAYWRIGHT_MCP_TARGET_RESOLVER: env.PLAYWRIGHT_MCP_TARGET_RESOLVER,
      PLAYWRIGHT_MCP_DIAGNOSTIC_LOG: targetMcpDiagnosticLog,
      // 打包版 MCP 子进程即本应用 exe 以 Node 模式运行 wrapper，需要经由
      // openjiuwen 的 env 白名单转发该开关。
      ...(app.isPackaged ? { ELECTRON_RUN_AS_NODE: '1' } : {}),
    });
    // The upstream BrowserAgent honors PLAYWRIGHT_MCP_ARGS. Route it through
    // our exact-target adapter here, at the Electron/Python process boundary,
    // so an unmodified openjiuwen installation cannot see the trusted UI.
    if (app.isPackaged) {
      // 打包版不依赖用户机器的 Node.js/npx，也无需首启联网下载：MCP 运行时
      // （@playwright/mcp 及其依赖）由构建脚本装进 resources/app/node_modules，
      // wrapper 与其同级，require.resolve 可直接命中；用自身 exe 以 Node 模式
      // 运行 wrapper（ELECTRON_RUN_AS_NODE 经 ENV_JSON 转发）。
      env.PLAYWRIGHT_MCP_COMMAND = process.execPath;
      env.PLAYWRIGHT_MCP_ARGS = JSON.stringify([TARGET_MCP_WRAPPER_PATH]);
    } else {
      env.PLAYWRIGHT_MCP_COMMAND = 'npx';
      env.PLAYWRIGHT_MCP_ARGS = JSON.stringify([
        '-y',
        '--package',
        PLAYWRIGHT_MCP_PACKAGE,
        'node',
        TARGET_MCP_WRAPPER_PATH,
      ]);
    }
  }
  // Mirror the Python desktop child env contract (desktop_app._build_child_env):
  // inject the full session port group so agent/gateway/web agree, and let the
  // children skip workspace preparation because the launcher did it once.
  env.JIUWENSWARM_RUNTIME_WORKSPACE_READY = '1';
  env.WEB_HOST = BACKEND_HOST;
  env.WEB_PORT = String(ports.gatewayApi);
  env.GATEWAY_PORT = String(ports.gatewayInternal);
  env.AGENT_SERVER_PORT = String(ports.agentServer);
  env.AGENT_PORT = String(ports.agentServer);
  env.FRONTEND_PORT = String(ports.frontend);
  // Gateway prefers AGENT_SERVER_URL over AGENT_SERVER_PORT; drop any stale
  // URL from the parent shell so the remapped port is used.
  delete env.AGENT_SERVER_URL;
  if (!env.JIUWENSWARM_START_CMD) {
    env.JIUWENSWARM_START_CMD = JSON.stringify([process.execPath, ...process.argv.slice(1)]);
  }
  if (name === 'web' && VITE_DEV_MODE) {
    env.ELECTRON_RUN_AS_NODE = '1';
  }

  const isViteDev = name === 'web' && VITE_DEV_MODE;
  const child = spawn(command, args, {
    cwd: isViteDev ? frontendDir() : serviceWorkingDirectory(),
    env,
    detached: process.platform !== 'win32',
    stdio: app.isPackaged ? ['ignore', logStreamFor(name), logStreamFor(name)] : 'inherit',
    windowsHide: !isViteDev,
    shell: false,
  });
  serviceProcesses.set(name, child);
  child.once('exit', (code, signal) => {
    if (!shuttingDown) {
      console.error(`[electron] ${name} service exited early`, {
        code,
        signal,
      });
      // Keep the process-group identity until Electron exits. A uv wrapper can
      // exit before its Python descendants, and deleting this entry would make
      // the final cleanup lose ownership of that group.
      void terminateService(child);
    }
  });
  child.once('error', error => {
    console.error(`[electron] failed to start ${name} service`, error);
  });
  return child;
}

function waitForTcp(host, port, child, timeoutMs = STARTUP_TIMEOUT_MS) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      if (child.exitCode !== null) {
        reject(new Error(`Service for ${host}:${port} exited with code ${child.exitCode}`));
        return;
      }
      const socket = nodeNet.createConnection({ host, port });
      socket.setTimeout(1_500);
      socket.once('connect', () => {
        socket.destroy();
        resolve();
      });
      const retry = error => {
        socket.destroy();
        if (Date.now() >= deadline) {
          reject(new Error(`Timed out waiting for ${host}:${port}: ${error?.message || 'unavailable'}`));
          return;
        }
        setTimeout(attempt, 100);
      };
      socket.once('timeout', () => retry(new Error('socket timeout')));
      socket.once('error', retry);
    };
    attempt();
  });
}

async function waitForHttp(host, port, child, timeoutMs = STARTUP_TIMEOUT_MS) {
  const deadline = Date.now() + timeoutMs;
  const url = `http://${host}:${port}/`;
  for (;;) {
    if (child && child.exitCode !== null) {
      throw new Error(`Service for HTTP ${url} exited with code ${child.exitCode}`);
    }
    try {
      const response = await net.fetch(url);
      if (response.ok || response.status > 0) return;
    } catch (error) {
      if (Date.now() >= deadline) {
        throw new Error(`Timed out waiting for HTTP ${url}: ${error?.message || 'unavailable'}`);
      }
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
}

function runToExit(command, args, { cwd } = {}) {
  const name = 'workspace-prepare';
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: cwd ?? serviceWorkingDirectory(),
      env: { ...process.env, JIUWENSWARM_DESKTOP: '1', JIUWENSWARM_ELECTRON: '1' },
      detached: process.platform !== 'win32',
      stdio: app.isPackaged ? ['ignore', logStreamFor(name), logStreamFor(name)] : 'inherit',
      windowsHide: true,
      shell: false,
    });
    serviceProcesses.set(name, child);
    child.once('error', reject);
    child.once('exit', code => {
      if (code === 0) resolve();
      else reject(new Error(`${command} workspace preparation exited with code ${code ?? 'signal'}`));
    });
  });
}

function prepareRuntimeWorkspace() {
  // Aligned with the Python desktop launcher (desktop_app.start_services):
  // workspace migration/repair runs exactly once in the launcher, then
  // agent/gateway skip the same disk work via
  // JIUWENSWARM_RUNTIME_WORKSPACE_READY=1. This removes the app supervisor
  // process and its full cold-start import from the startup path.
  if (app.isPackaged) {
    return runToExit(backendExecutable(), ['--desktop-prepare-runtime-workspace']);
  }
  return runToExit('uv', [
    'run',
    'python',
    '-c',
    'from jiuwenswarm.common.utils import prepare_runtime_workspace; prepare_runtime_workspace(cleanup_stale_descs=False)',
  ]);
}

function watchBackendPair(agentProcess, gatewayProcess) {
  // Same paired-lifecycle rule as the Python desktop: if either backend exits
  // after startup, promptly stop its peer so it cannot keep ports, cron jobs,
  // or the gateway singleton lock alive.
  const watcher = setInterval(() => {
    if (shuttingDown) {
      clearInterval(watcher);
      return;
    }
    const agentExited = agentProcess.exitCode !== null;
    const gatewayExited = gatewayProcess.exitCode !== null;
    if (!agentExited && !gatewayExited) return;
    clearInterval(watcher);
    console.error('[electron] backend service exited after startup; terminating peer', {
      agentExited,
      gatewayExited,
    });
    if (agentExited && gatewayProcess.exitCode === null) void terminateService(gatewayProcess);
    if (gatewayExited && agentProcess.exitCode === null) void terminateService(agentProcess);
  }, 250);
}

const WARMUP_PACKAGES = [
  'jiuwenswarm',
  'openjiuwen',
  'faiss',
  'pymilvus',
  'google',
  'a2ui',
  'sqlite_vec',
  'tree_sitter',
  'tiktoken',
  'tiktoken_ext',
];
const WARMUP_READ_BYTES = 64 * 1024;

async function warmupPackageDir(pkgDir) {
  let entries = [];
  try {
    entries = await fs.readdir(pkgDir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    const entryPath = path.join(pkgDir, entry.name);
    if (entry.isDirectory()) {
      await warmupPackageDir(entryPath);
      continue;
    }
    try {
      const handle = await fs.open(entryPath, 'r');
      try {
        await handle.read(Buffer.alloc(WARMUP_READ_BYTES), 0, WARMUP_READ_BYTES, 0);
      } finally {
        await handle.close();
      }
    } catch { /* best-effort prefetch */ }
  }
}

function startBackendPageCacheWarmup() {
  // Frozen backend children pay a slow first read of .pyd/.py files from disk.
  // Prefetch the head of each file in the key packages into the OS page cache
  // in the background (aligned with desktop_app._warmup_page_cache_background)
  // so the reads overlap with process spawning instead of serializing it.
  // Dev mode skips this: uv already has warm pyc and OS cache.
  if (!app.isPackaged) return;
  const internalDir = path.join(process.resourcesPath, 'backend', '_internal');
  void (async () => {
    for (const pkg of WARMUP_PACKAGES) {
      await warmupPackageDir(path.join(internalDir, pkg));
    }
  })().catch(() => {});
}

function frontendOnlyUrl() {
  const localIndex = path.join(__dirname, 'dist', 'index.html');
  if (!fsSync.existsSync(localIndex)) {
    throw new Error(`FrontendOnly: dist/index.html not found at ${localIndex}`);
  }
  return `file://${localIndex.replace(/\\/g, '/')}`;
}

async function startWebService(onWebReady) {
  const ports = await findAvailablePorts();
  sessionPorts = ports;
  console.log('[electron] Ports:', ports);
  startBackendPageCacheWarmup();

  // Aligned with the Python desktop flow: the web service only depends on
  // static assets/proxying, so it starts first and triggers early navigation
  // as soon as its HTTP server answers - the frontend reconnect logic covers
  // the remaining backend bring-up, so there is no need to serialize on it.
  const webProcess = spawnService('web', ports, [
    '--host',
    FRONTEND_HOST,
    '--port',
    String(ports.frontend),
    '--proxy-target',
    `http://${BACKEND_HOST}:${ports.gatewayApi}`,
  ]);
  const webReady = waitForHttp(FRONTEND_HOST, ports.frontend, webProcess);
  void webReady.then(() => {
    console.log(`[electron] web ready, navigating early to http://${FRONTEND_HOST}:${ports.frontend}`);
    if (typeof onWebReady === 'function') onWebReady(`http://${FRONTEND_HOST}:${ports.frontend}`);
  }, () => {});
  return { ports, webProcess, webReady };
}

async function startBackendServices({ ports, webProcess, webReady }) {
  // Spawned after the per-session target resolver is listening: agent/gateway
  // consume PLAYWRIGHT_MCP_TARGET_RESOLVER through their spawn env.
  let agentProcess = null;
  let gatewayProcess = null;
  try {
    await prepareRuntimeWorkspace();
    agentProcess = spawnService('agent', ports);
    gatewayProcess = spawnService('gateway', ports);

    // Both backend readiness waits run in parallel; the first failure tears
    // down the whole group so the other wait exits via its child exit check
    // instead of waiting for the full timeout.
    await Promise.all([
      waitForTcp(BACKEND_HOST, ports.agentServer, agentProcess),
      waitForTcp(BACKEND_HOST, ports.gatewayApi, gatewayProcess),
      webReady,
    ]);
  } catch (error) {
    for (const child of [webProcess, agentProcess, gatewayProcess]) {
      if (child) void terminateService(child);
    }
    throw error;
  }

  watchBackendPair(agentProcess, gatewayProcess);
  console.log(`[electron] services ready: http://${FRONTEND_HOST}:${ports.frontend}`);
}

function signalServiceTree(child, signal) {
  if (!child?.pid) return false;
  try {
    if (process.platform === 'win32') {
      const taskkill = spawn('taskkill', ['/pid', String(child.pid), '/t', '/f'], {
        windowsHide: true,
        stdio: 'ignore',
      });
      taskkill.unref();
    } else {
      process.kill(-child.pid, signal);
    }
    return true;
  } catch (error) {
    if (error?.code !== 'ESRCH') {
      console.warn(`[electron] failed to send ${signal} to service tree`, error);
    }
    return false;
  }
}

function serviceTreeRunning(child) {
  if (!child?.pid) return false;
  if (process.platform === 'win32') return child.exitCode === null;
  try {
    process.kill(-child.pid, 0);
    return true;
  } catch (error) {
    return error?.code !== 'ESRCH';
  }
}

function waitForServiceTreeExit(child, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise(resolve => {
    const poll = () => {
      if (!serviceTreeRunning(child)) {
        resolve(true);
        return;
      }
      if (Date.now() >= deadline) {
        resolve(false);
        return;
      }
      setTimeout(poll, 100);
    };
    poll();
  });
}

async function terminateService(child) {
  if (!child?.pid || !serviceTreeRunning(child)) return;
  signalServiceTree(child, 'SIGTERM');
  if (await waitForServiceTreeExit(child, SERVICE_SHUTDOWN_TIMEOUT_MS)) return;

  console.warn(`[electron] service tree ${child.pid} did not stop after SIGTERM; forcing shutdown`);
  signalServiceTree(child, 'SIGKILL');
  if (!(await waitForServiceTreeExit(child, SERVICE_KILL_TIMEOUT_MS))) {
    console.error(`[electron] service tree ${child.pid} is still running after SIGKILL`);
  }
}

function stopServices() {
  if (shutdownPromise) return shutdownPromise;
  shuttingDown = true;
  const services = [...serviceProcesses.entries()];
  for (const [name, child] of services) {
    console.log(`[electron] stopping ${name} service tree`, { pid: child.pid });
  }
  shutdownPromise = Promise.allSettled(services.map(([, child]) => terminateService(child))).then(results => {
    for (const result of results) {
      if (result.status === 'rejected') console.error('[electron] service shutdown failed', result.reason);
    }
    serviceProcesses.clear();
  });
  return shutdownPromise;
}

function forceStopServices() {
  for (const child of serviceProcesses.values()) signalServiceTree(child, 'SIGKILL');
}

function requestShutdown(exitCode = 0) {
  requestedExitCode = Math.max(requestedExitCode, exitCode);
  app.quit();
}

function resolveLogoSvg() {
  const logoCandidates = [
    path.join(__dirname, 'logo.svg'),
    path.join(__dirname, 'dist', 'logo.svg'),
    path.join(__dirname, '..', '..', 'web', 'frontend', 'public', 'logo.svg'),
    path.join(__dirname, '..', '..', 'web', 'frontend', 'dist', 'logo.svg'),
  ];
  for (const candidate of logoCandidates) {
    try {
      if (fsSync.existsSync(candidate)) {
        return fsSync.readFileSync(candidate, 'utf-8');
      }
    } catch { /* try next */ }
  }
  return '';
}

function resolveIconPath() {
  const bundledIcon = path.join(__dirname, process.platform === 'win32' ? 'logo.ico' : 'logo.icns');
  if (fsSync.existsSync(bundledIcon)) return bundledIcon;
  const publicIcon = path.join(__dirname, '..', '..', 'web', 'frontend', 'public', process.platform === 'win32' ? 'logo.ico' : 'logo.icns');
  if (fsSync.existsSync(publicIcon)) return publicIcon;
  const fallback = path.join(__dirname, '..', '..', 'web', 'frontend', 'public', 'logo.ico');
  return fsSync.existsSync(fallback) ? fallback : undefined;
}

function loadingHtml() {
  const logoSvg = resolveLogoSvg();
  const loadingHtml = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden;background:#0f172a;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
color:#e2e8f0;display:flex;align-items:center;justify-content:center}
.root{display:flex;flex-direction:column;align-items:center;gap:32px;padding:40px}
.logo{width:64px;height:64px;border-radius:16px;
background:linear-gradient(135deg,#3b82f6,#8b5cf6);
display:flex;align-items:center;justify-content:center;
box-shadow:0 8px 24px rgba(59,130,246,.25)}
.logo svg{width:64px;height:64px;border-radius:16px}
.app-name{font-size:22px;font-weight:700;letter-spacing:-.3px;color:#f1f5f9}
.spinner{width:32px;height:32px;border:3px solid rgba(148,163,184,.2);
border-top-color:#60a5fa;border-radius:50%;animation:spin 1.5s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.tip-area{margin-top:8px;text-align:center;min-height:60px;
display:flex;flex-direction:column;align-items:center;gap:8px}
.tip-label{font-size:11px;text-transform:uppercase;letter-spacing:1.5px;color:#475569}
.tip-text{font-size:13px;color:#94a3b8;max-width:320px;line-height:1.5;
transition:opacity .4s ease,transform .4s ease}
.tip-text.fade-out{opacity:0;transform:translateY(-8px)}
.tip-text.fade-in{opacity:1;transform:translateY(0)}
.dots{display:flex;gap:4px;justify-content:center}
.dot{width:4px;height:4px;border-radius:50%;background:#475569}
.dot.active{background:#60a5fa;animation:pulse 1.2s ease infinite}
@keyframes pulse{0%,100%{opacity:.4}50%{opacity:1}}
</style>
</head>
<body>
<div class="root">
<div class="logo">${logoSvg}</div>
<div class="app-name">JiuwenSwarm</div>
<div class="spinner"></div>
<div class="tip-area">
    <div class="tip-label">专业智能AI Agent助理</div>
    <div class="tip-text" id="tip"></div>
</div>
<div class="dots" id="dots"></div>
<div class="tip-label" style="margin-top:16px">服务启动加载中</div>
</div>
<script>
const tips=[
"多智能体协作 —— 编排多个专业 Agent 协同工作，群体智能涌现",
"多端接入 —— 支持 Web、飞书、钉钉、Telegram 等多种交互方式",
"贴身任务管家 —— 精准理解析复杂指令，智能排期，有条不紊完成任务",
"自主演进 —— 根据你的反馈自动调整技能，持续进化，越用越懂你"
];
let idx=0;
const el=document.getElementById('tip');
const dotsEl=document.getElementById('dots');
tips.forEach((_,i)=>{
const d=document.createElement('div');
d.className='dot'+(i===0?' active':'');
dotsEl.appendChild(d);
});
function showTip(){
const dots=dotsEl.children;
for(let i=0;i<dots.length;i++) dots[i].className='dot'+(i===idx?' active':'');
el.className='tip-text fade-out';
setTimeout(()=>{
    el.textContent=tips[idx];
    el.className='tip-text fade-in';
},400);
idx=(idx+1)%tips.length;
}
showTip();
setInterval(showTip,3500);
</script>
</body>
</html>`;
  return 'data:text/html;charset=utf-8;base64,' + Buffer.from(loadingHtml, 'utf-8').toString('base64');
}

function currentBrowserState(entry) {
  const view = entry?.view;
  if (!view || view.webContents.isDestroyed()) {
    return {
      sessionId: entry?.sessionId ?? '',
      url: '',
      title: '',
      loading: false,
      canGoBack: false,
      canGoForward: false,
    };
  }
  const history = view.webContents.navigationHistory;
  return {
    sessionId: entry.sessionId,
    url: view.webContents.getURL(),
    title: view.webContents.getTitle(),
    loading: view.webContents.isLoading(),
    canGoBack: history.canGoBack(),
    canGoForward: history.canGoForward(),
  };
}

function emitBrowserState(entry) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('browser:state-changed', currentBrowserState(entry));
  }
}

function emitLayoutInvalidated() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('desktop:layout-invalidated');
  }
}

function normalizeBrowserTarget(rawValue) {
  const raw = String(rawValue || '').trim();
  if (!raw) return 'about:blank';
  if (/\s/.test(raw) && !/^https?:\/\//i.test(raw)) {
    return `https://cn.bing.com/search?q=${encodeURIComponent(raw)}`;
  }
  const candidate = /^[a-z][a-z\d+.-]*:/i.test(raw) ? raw : `https://${raw}`;
  const parsed = new URL(candidate);
  if (!['https:', 'http:', 'about:'].includes(parsed.protocol)) {
    throw new Error(`Unsupported browser URL protocol: ${parsed.protocol}`);
  }
  if (parsed.protocol === 'about:' && parsed.href !== 'about:blank') {
    throw new Error('Only about:blank is allowed');
  }
  return parsed.href;
}

async function waitForCdpTarget(targetId, timeoutMs = CDP_TARGET_TIMEOUT_MS) {
  const endpoint = `http://${BACKEND_HOST}:${cdpPort}/json/list`;
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  do {
    try {
      const response = await net.fetch(endpoint);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const targets = await response.json();
      if (Array.isArray(targets) && targets.some(target => target?.id === targetId)) return;
      lastError = new Error(`target ${targetId} is not present`);
    } catch (error) {
      lastError = error;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  } while (Date.now() < deadline);
  throw new Error(
    `Electron CDP target did not become ready at ${endpoint}: ${lastError?.message || 'unknown error'}`,
  );
}

function normalizeBrowserSessionId(rawValue) {
  const normalized = String(rawValue || '').trim();
  return normalized || 'default';
}

function browserPartitionToken(sessionId) {
  // partition 名只保留 id 安全字符；异常 session id 折叠为 '_'，最长 64。
  const token = String(sessionId).replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 64);
  return token || 'default';
}

function browserSessionUrlsPath() {
  return path.join(app.getPath('userData'), BROWSER_SESSION_URLS_FILENAME);
}

function loadSessionLastUrls() {
  try {
    const parsed = JSON.parse(fsSync.readFileSync(browserSessionUrlsPath(), 'utf8'));
    if (parsed && typeof parsed === 'object') {
      for (const [sessionId, url] of Object.entries(parsed)) {
        if (typeof url === 'string' && /^https?:/i.test(url)) {
          sessionLastUrls.set(String(sessionId), url);
        }
      }
    }
  } catch {
    // 首次启动或文件损坏：从空开始，不影响任何启动路径。
  }
}

function saveSessionLastUrls() {
  if (sessionUrlsSaveTimer) {
    clearTimeout(sessionUrlsSaveTimer);
    sessionUrlsSaveTimer = null;
  }
  try {
    fsSync.mkdirSync(app.getPath('userData'), { recursive: true });
    fsSync.writeFileSync(
      browserSessionUrlsPath(),
      JSON.stringify(Object.fromEntries(sessionLastUrls), null, 2),
      'utf8',
    );
  } catch (error) {
    console.warn('[electron] failed to persist browser session urls', error);
  }
}

function scheduleSessionUrlsSave() {
  if (sessionUrlsSaveTimer) return;
  sessionUrlsSaveTimer = setTimeout(() => {
    sessionUrlsSaveTimer = null;
    saveSessionLastUrls();
  }, SESSION_URLS_SAVE_DEBOUNCE_MS);
}

function evictIdleBrowserViews(preserveSessionId) {
  // 上限保护：桌面长会话里视图不能无限累积；优先回收最久未用且未显示的。
  // 被回收视图的最后页面 URL 已由 recordLastUrl 记入 sessionLastUrls，
  // 该会话再次打开时 ensureBrowserView 会还原页面（cookie/登录态随 partition 保留）。
  while (browserViews.size >= MAX_BROWSER_SESSION_VIEWS) {
    const candidates = [...browserViews.entries()]
      .filter(([sid, entry]) => sid !== preserveSessionId && sid !== activePaneSessionId && !entry.visible)
      .sort(([, a], [, b]) => a.lastActive - b.lastActive);
    if (candidates.length === 0) return;
    const [victimId, victim] = candidates[0];
    browserViews.delete(victimId);
    try {
      mainWindow?.contentView?.removeChildView(victim.view);
      victim.view.webContents.close();
    } catch (error) {
      console.warn('[electron] failed to close evicted sideview', victimId, error);
    }
  }
}

async function ensureBrowserView(sessionId) {
  if (!hasCdp || !mainWindow || mainWindow.isDestroyed()) return null;
  const key = normalizeBrowserSessionId(sessionId);
  const existing = browserViews.get(key);
  if (existing && !existing.view.webContents.isDestroyed()) {
    existing.lastActive = Date.now();
    return existing;
  }
  if (existing) browserViews.delete(key);
  evictIdleBrowserViews(key);

  const partition = `persist:jiuwenswarm-browser-${browserPartitionToken(key)}`;
  const partitionSession = session.fromPartition(partition);
  partitionSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  const view = new WebContentsView({
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      partition,
    },
  });
  mainWindow.contentView.addChildView(view);
  view.setVisible(false);
  const entry = { sessionId: key, view, targetId: '', crashCount: 0, lastActive: Date.now(), visible: false };
  browserViews.set(key, entry);

  for (const eventName of ['did-start-loading', 'did-stop-loading', 'did-navigate', 'did-navigate-in-page', 'page-title-updated']) {
    view.webContents.on(eventName, () => emitBrowserState(entry));
  }
  // 记录最后浏览的页面：视图被 LRU 回收后重建时据此还原，而不是回到默认页。
  // 仅记录 http(s)；about:blank#jiuwen-session 标记页与空白态不入册。
  const recordLastUrl = () => {
    try {
      const url = view.webContents.getURL();
      if (/^https?:/i.test(url)) {
        sessionLastUrls.set(key, url);
        scheduleSessionUrlsSave();
      }
    } catch { /* webContents 可能正在销毁 */ }
  };
  view.webContents.on('did-navigate-in-page', recordLastUrl);
  view.webContents.setWindowOpenHandler(details => {
    try {
      void view.webContents.loadURL(normalizeBrowserTarget(details.url));
    } catch (error) {
      console.warn('[electron] blocked popup URL', details.url, error);
    }
    return { action: 'deny' };
  });
  view.webContents.on('render-process-gone', (_event, details) => {
    if (shuttingDown) return;
    // 连续崩溃（如显卡/站点问题）时放弃无限 reload，避免 crash loop。
    entry.crashCount += 1;
    if (entry.crashCount > BROWSER_VIEW_MAX_CRASHES) {
      console.error('[electron] sideview renderer keeps crashing; leaving it down', { sessionId: key, details });
      return;
    }
    console.warn('[electron] sideview renderer exited; reloading the owned WebContents', { sessionId: key, details });
    view.webContents.reload();
  });
  view.webContents.on('did-navigate', () => {
    entry.crashCount = 0;
    recordLastUrl();
  });
  // 首屏提交本地空白页：CDP target 立即注册。此前的 `loadURL(DEFAULT_BROWSER_URL)`
  // 会阻塞创建流程——外网不可达时 Chromium 连接超时曾把加载卡住数十秒。
  // 会话 id 藏在 fragment 里，只出现在首次导航，供 resolver 侧诊断定位。
  try {
    await view.webContents.loadURL(`about:blank#jiuwen-session=${encodeURIComponent(key)}`);
  } catch (error) {
    console.warn('[electron] sideview about:blank load failed', { sessionId: key, error });
  }
  entry.targetId = view.webContents.getOrCreateDevToolsTargetId();
  await waitForCdpTarget(entry.targetId);
  // 后台加载最后浏览的页面（视图被回收过则还原，否则默认页）：失败（如离线/
  // 代理受限）不影响启动，也不影响浏览器 Agent 的 target 绑定——TargetID 随
  // WebContents 不随导航变化。
  const restoreUrl = sessionLastUrls.get(key) || DEFAULT_BROWSER_URL;
  void view.webContents.loadURL(restoreUrl).catch(error => {
    console.warn('[electron] initial sideview navigation failed', { sessionId: key, error });
  });
  console.log('[electron] sideview CDP target ready', {
    sessionId: key,
    endpoint: `http://${BACKEND_HOST}:${cdpPort}`,
    targetId: entry.targetId,
  });
  return entry;
}

function applyBrowserBounds(entry, bounds) {
  if (!entry?.view || !mainWindow || mainWindow.isDestroyed()) return { x: 0, y: 0, width: 0, height: 0 };
  const contentBounds = mainWindow.getContentBounds();
  // Renderer rectangles are expressed in CSS pixels. Electron View bounds
  // use device-independent pixels, which only match CSS pixels at 100% zoom.
  const zoomFactor = mainWindow.webContents.getZoomFactor();
  const scaledX = Math.round((Number(bounds?.x) || 0) * zoomFactor);
  const scaledY = Math.round((Number(bounds?.y) || 0) * zoomFactor);
  const x = Math.max(0, Math.min(scaledX, contentBounds.width));
  const y = Math.max(0, Math.min(scaledY, contentBounds.height));
  const width = Math.max(0, Math.min(Math.round((Number(bounds?.width) || 0) * zoomFactor), contentBounds.width - x));
  const height = Math.max(0, Math.min(Math.round((Number(bounds?.height) || 0) * zoomFactor), contentBounds.height - y));
  entry.view.setBounds({ x, y, width, height });
  if (entry.visible) entry.view.setVisible(true);
  return { x, y, width, height };
}

function setBrowserPaneVisible(sessionId, visible) {
  const key = normalizeBrowserSessionId(sessionId);
  if (!visible) {
    const entry = browserViews.get(key);
    if (entry) {
      entry.visible = false;
      entry.view.setVisible(false);
    }
    if (activePaneSessionId === key) activePaneSessionId = '';
    return false;
  }
  activePaneSessionId = key;
  // 同屏只允许一个会话的视图：激活前先隐藏其它会话视图。
  for (const [sid, entry] of browserViews) {
    if (sid !== key) {
      entry.visible = false;
      entry.view.setVisible(false);
    }
  }
  // 视图可能尚未创建（首次切到 browser 页签）：创建完成后兜底应用边界与可见性。
  void ensureBrowserView(key)
    .then(entry => {
      if (!entry || activePaneSessionId !== key) return;
      entry.visible = true;
      entry.view.setVisible(true);
      if (lastBrowserBounds) applyBrowserBounds(entry, lastBrowserBounds);
      if (entry.visible) entry.view.webContents.focus();
      emitBrowserState(entry);
    })
    .catch(error => console.warn('[electron] sideview activation failed', { sessionId: key, error }));
  return true;
}

function startBrowserTargetResolver() {
  // Browser Agent 的 target 绑定入口：GET /<sessionId> → 惰性创建该会话视图并
  // 返回其 CDP TargetID。仅绑定 127.0.0.1，与 CDP 调试端口同一信任边界。
  return new Promise((resolve, reject) => {
    const server = nodeHttp.createServer((req, res) => {
      res.setHeader('Content-Type', 'application/json; charset=utf-8');
      try {
        const url = new URL(req.url || '/', `http://${BACKEND_HOST}`);
        const sessionId = decodeURIComponent(url.pathname.replace(/^\/+/, ''));
        if (!sessionId) {
          res.statusCode = 404;
          res.end(JSON.stringify({ error: 'session id path segment required' }));
          return;
        }
        ensureBrowserView(sessionId)
          .then(entry => {
            if (!entry) {
              res.statusCode = 503;
              res.end(JSON.stringify({ error: 'browser sideview disabled' }));
              return;
            }
            res.end(JSON.stringify({ sessionId: entry.sessionId, targetId: entry.targetId }));
          })
          .catch(error => {
            res.statusCode = 500;
            res.end(JSON.stringify({ error: String(error?.message || error) }));
          });
      } catch (error) {
        res.statusCode = 400;
        res.end(JSON.stringify({ error: String(error?.message || error) }));
      }
    });
    server.once('error', reject);
    server.listen({ host: BACKEND_HOST, port: 0 }, () => {
      const address = server.address();
      resolve({ server, port: address.port });
    });
  });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function failureHtml(detail) {
  // 对齐 Python 桌面失败页（desktop_app._build_loading_html 的 error-panel）：
  // 深色主题、错误原因可选中复制、日志路径、退出按钮。窗口保留由用户退出，
  // 不做"弹原生框即退出"。
  const logoSvg = resolveLogoSvg();
  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden;background:#0f172a;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
color:#e2e8f0;display:flex;align-items:center;justify-content:center}
.root{width:min(680px,calc(100% - 48px));padding:40px}
.panel{display:flex;flex-direction:column;align-items:center;gap:24px;text-align:center}
.error-icon{width:52px;height:52px;border-radius:50%;display:flex;align-items:center;
justify-content:center;background:rgba(239,68,68,.14);color:#f87171;font-size:30px;font-weight:700}
.error-title{font-size:20px;font-weight:700;color:#f8fafc}
.error-message{max-width:620px;color:#cbd5e1;font-size:13px;line-height:1.7;
white-space:pre-wrap;overflow-wrap:anywhere;user-select:text;text-align:left;
width:100%;padding:14px 16px;border:1px solid #334155;border-radius:10px;background:#111827}
.error-meta{width:100%;padding:12px 16px;border:1px solid #334155;border-radius:10px;
background:#111827;color:#94a3b8;font-size:12px;line-height:1.6;text-align:left;
white-space:pre-wrap;overflow-wrap:anywhere;user-select:text}
.hint{color:#64748b;font-size:12px;max-width:560px;line-height:1.6}
.close-button{border:0;border-radius:8px;padding:10px 24px;background:#2563eb;color:white;
font-size:14px;cursor:pointer;margin-top:4px}
.close-button:hover{background:#1d4ed8}
</style>
</head>
<body>
<div class="root">
<div class="panel">
<div class="logo">${logoSvg}</div>
<div class="error-icon">!</div>
<div class="error-title">JiuwenSwarm 启动失败</div>
<div class="error-message">${escapeHtml(detail)}</div>
<div class="error-meta">日志文件：${escapeHtml(mainLogPath())}

排查时可将上述日志文件提供给支持人员。</div>
<div class="hint">若为安装后的首次启动，常见原因是安全软件扫描新文件导致的瞬时故障，重新启动应用通常可恢复。</div>
<button class="close-button" id="close-button" type="button">退出 JiuwenSwarm</button>
</div>
</div>
<script>
document.getElementById('close-button').addEventListener('click', () => {
  if (window.jiuwenDesktop && typeof window.jiuwenDesktop.closeWindow === 'function') {
    window.jiuwenDesktop.closeWindow();
  } else {
    window.close();
  }
});
</script>
</body>
</html>`;
  return 'data:text/html;charset=utf-8;base64,' + Buffer.from(html, 'utf-8').toString('base64');
}

async function showStartupFailure(error) {
  // 对齐 Python 桌面的失败呈现：诊断信息留在窗口内、用户自行退出；
  // 残余服务树立即清理（Python 侧呈现失败页后同样调用 shutdown()），
  // 但不退出应用。仅当窗口本身不可用时才回退到原生错误框 + 退出。
  console.error('[electron] startup failed', error);
  const detail = error instanceof Error ? `${error.message}\n${error.stack || ''}` : String(error);
  if (!mainWindow || mainWindow.isDestroyed()) {
    dialog.showErrorBox('JiuwenSwarm failed to start', detail);
    requestShutdown(1);
    return;
  }
  void stopServices();
  try {
    await mainWindow.loadURL(failureHtml(detail));
    mainWindow.show();
    mainWindow.focus();
  } catch (loadError) {
    console.error('[electron] failed to render startup failure page', loadError);
    dialog.showErrorBox('JiuwenSwarm failed to start', detail);
    requestShutdown(1);
  }
}

function trustedSender(event) {
  return Boolean(mainWindow && !mainWindow.isDestroyed() && event.sender === mainWindow.webContents);
}

function registerHandler(channel, handler) {
  ipcMain.handle(channel, async (event, ...args) => {
    if (!trustedSender(event)) throw new Error('Untrusted IPC sender');
    return handler(...args);
  });
}

function sanitizeFilename(filename, fallback = 'download') {
  const base = path
    .basename(String(filename || ''))
    .replace(/[\0<>:"/\\|?*]/g, '_')
    .trim();
  return base || fallback;
}

async function uniqueDownloadPath(filename) {
  const downloads = app.getPath('downloads');
  const safeName = sanitizeFilename(filename);
  const extension = path.extname(safeName);
  const stem = path.basename(safeName, extension);
  let candidate = path.join(downloads, safeName);
  for (let counter = 1; ; counter += 1) {
    try {
      await fs.access(candidate);
      candidate = path.join(downloads, `${stem} (${counter})${extension}`);
    } catch {
      return candidate;
    }
  }
}

function canUseBackendUpdateHelper() {
  if (process.platform !== 'win32' || !app.isPackaged || isFrontendOnly || !sessionPorts) {
    return false;
  }
  try {
    return fsSync.existsSync(backendExecutable());
  } catch {
    return false;
  }
}

function registerIpcHandlers() {
  ipcMain.on('desktop:is-frontend-only', event => {
    if (mainWindow && !mainWindow.isDestroyed() && event.sender === mainWindow.webContents) {
      event.returnValue = isFrontendOnly;
    } else {
      event.returnValue = false;
    }
  });
  registerHandler('desktop:minimize-window', () => {
    mainWindow.minimize();
    return true;
  });
  registerHandler('desktop:toggle-fullscreen-window', () => {
    mainWindow.setFullScreen(!mainWindow.isFullScreen());
    return true;
  });
  registerHandler('desktop:close-window', () => {
    mainWindow.close();
    return true;
  });
  registerHandler('desktop:select-project-directory', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ['openDirectory', 'createDirectory'],
    });
    return result.canceled ? null : result.filePaths[0] || null;
  });
  registerHandler('desktop:save-data-url', async (dataUrl, filename) => {
    if (typeof dataUrl !== 'string' || !dataUrl.startsWith(PNG_DATA_URL_PREFIX)) {
      return { ok: false, cancelled: false };
    }
    const result = await dialog.showSaveDialog(mainWindow, {
      defaultPath: path.join(app.getPath('downloads'), sanitizeFilename(filename, 'share.png')),
      filters: [{ name: 'PNG Image', extensions: ['png'] }],
    });
    if (result.canceled || !result.filePath) return { ok: false, cancelled: true };
    const bytes = Buffer.from(dataUrl.slice(PNG_DATA_URL_PREFIX.length), 'base64');
    if (!bytes.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
      return { ok: false, cancelled: false };
    }
    await fs.writeFile(result.filePath, bytes);
    return { ok: true, cancelled: false };
  });
  registerHandler('desktop:download-file', async (url, filename) => {
    const targetUrl = new URL(String(url), currentFrontendUrl);
    // 仅允许 http/https：渲染器传入 file: 等本地协议会把任意本地文件写进下载目录。
    if (targetUrl.protocol !== 'http:' && targetUrl.protocol !== 'https:') {
      throw new Error(`Unsupported download protocol: ${targetUrl.protocol}`);
    }
    const response = await net.fetch(targetUrl);
    if (!response.ok) throw new Error(`Download failed with HTTP ${response.status}`);
    if (!response.body) throw new Error('Download response has no body');
    const targetPath = await uniqueDownloadPath(filename);
    // 流式落盘，避免大文件整体驻留内存。
    await pipeline(Readable.fromWeb(response.body), fsSync.createWriteStream(targetPath));
    shell.showItemInFolder(targetPath);
    return true;
  });
  registerHandler('desktop:install-update', async installerPath => {
    const target = path.resolve(String(installerPath));
    if (canUseBackendUpdateHelper()) {
      // Windows packaged builds delegate to the bundled backend's update
      // helper (workswarm.exe --desktop-install-update): it waits for this
      // process to exit and for the backend/frontend ports to release before
      // launching the installer — the same contract as the Python desktop
      // update flow in desktop_app._launch_windows_install_helper.
      const helper = spawn(backendExecutable(), [
        '--desktop-install-update',
        '--installer-path', target,
        '--app-executable', process.execPath,
        '--parent-pid', String(process.pid),
        '--backend-port', String(sessionPorts.gatewayApi),
        '--frontend-port', String(sessionPorts.frontend),
      ], {
        cwd: serviceWorkingDirectory(),
        env: { ...process.env },
        detached: true,
        stdio: 'ignore',
        windowsHide: true,
        shell: false,
      });
      helper.once('error', error => {
        console.error('[electron] failed to launch backend update helper', error);
        // Helper could not be spawned: fall back to opening the installer
        // directly (dev-equivalent behavior) instead of leaving the app
        // running with no update at all.
        void shell.openPath(target).then(errorMessage => {
          if (!errorMessage) setTimeout(() => app.quit(), 250);
        });
      });
      helper.once('spawn', () => {
        setTimeout(() => requestShutdown(0), 250);
      });
      helper.unref();
      return true;
    }
    const errorMessage = await shell.openPath(target);
    if (errorMessage) return false;
    setTimeout(() => app.quit(), 250);
    return true;
  });

  registerHandler('browser:navigate', async (url, sessionId) => {
    const entry = await ensureBrowserView(sessionId);
    if (!entry) return currentBrowserState({ sessionId: normalizeBrowserSessionId(sessionId) });
    await entry.view.webContents.loadURL(normalizeBrowserTarget(url));
    // 地址栏发起导航后把焦点交给页面，免去手动点击才能交互。
    if (entry.visible) entry.view.webContents.focus();
    return currentBrowserState(entry);
  });
  registerHandler('browser:go-back', sessionId => {
    const entry = browserViews.get(normalizeBrowserSessionId(sessionId));
    if (entry?.view && entry.view.webContents.navigationHistory.canGoBack()) {
      entry.view.webContents.navigationHistory.goBack();
    }
    return currentBrowserState(entry);
  });
  registerHandler('browser:go-forward', sessionId => {
    const entry = browserViews.get(normalizeBrowserSessionId(sessionId));
    if (entry?.view && entry.view.webContents.navigationHistory.canGoForward()) {
      entry.view.webContents.navigationHistory.goForward();
    }
    return currentBrowserState(entry);
  });
  registerHandler('browser:reload', sessionId => {
    const entry = browserViews.get(normalizeBrowserSessionId(sessionId));
    if (entry?.view) entry.view.webContents.reload();
    return currentBrowserState(entry);
  });
  registerHandler('browser:stop', sessionId => {
    const entry = browserViews.get(normalizeBrowserSessionId(sessionId));
    if (entry?.view) entry.view.webContents.stop();
    return currentBrowserState(entry);
  });
  registerHandler('browser:get-state', sessionId => {
    const entry = browserViews.get(normalizeBrowserSessionId(sessionId));
    return currentBrowserState(entry);
  });
  registerHandler('browser:set-visible', (visible, sessionId) => {
    return setBrowserPaneVisible(sessionId, Boolean(visible));
  });
  registerHandler('browser:set-bounds', (bounds, sessionId) => {
    lastBrowserBounds = bounds;
    const entry = browserViews.get(normalizeBrowserSessionId(sessionId));
    return applyBrowserBounds(entry, bounds);
  });
}

async function createMainWindow() {
  mainWindow = new BrowserWindow({
    title: 'JiuwenSwarm',
    width: 1600,
    height: 1000,
    minWidth: 1100,
    minHeight: 720,
    backgroundColor: '#0f172a',
    show: false,
    autoHideMenuBar: true,
    icon: resolveIconPath(),
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.webContents.on('zoom-changed', () => {
    setTimeout(emitLayoutInvalidated, 0);
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.key === 'F12' && input.type === 'keyDown' && (!app.isPackaged || isTestBuild)) {
      mainWindow?.webContents.toggleDevTools();
      event.preventDefault();
    }
    if (input.key === 'r' && (input.control || input.meta) && input.type === 'keyDown') {
      mainWindow?.webContents.reload();
      event.preventDefault();
    }
  });
  registerIpcHandlers();

  // TEMP-MEASURE (remove after timing test)
  mainWindow.webContents.on('dom-ready', () => console.log('[electron][measure] dom-ready:', mainWindow.webContents.getURL()));
  mainWindow.webContents.on('did-finish-load', () => console.log('[electron][measure] did-finish-load:', mainWindow.webContents.getURL()));
  // END TEMP-MEASURE

  let navigated = false;
  const onWebReady = earlyUrl => {
    // Early navigation: the web static server answering is enough to show the
    // frontend skeleton; API/WS are reconnected by the frontend once the
    // gateway is up (same contract as the Python desktop on_web_ready).
    navigated = true;
    currentFrontendUrl = earlyUrl;
    console.log('[electron] early loading frontend:', earlyUrl);
    void mainWindow
      .loadURL(earlyUrl)
      .then(() => {
        mainWindow.show();
        mainWindow.focus();
        if (process.platform === 'darwin') app.focus({ steal: true });
      })
      .catch(error => console.error('[electron] early frontend load failed', error));
  };

  let webStartup = null;
  if (isFrontendOnly) {
    await mainWindow.loadURL(loadingHtml());
    console.log('[electron] loading screen shown');
  } else {
    // The loading page is transitional only: do not block on its first paint.
    // The frozen web service is the longest pole of first paint and does not
    // consume the CDP target env, so spawn it before the sideview bring-up —
    // its multi-second Python boot then overlaps renderer/CDP initialization.
    void mainWindow.loadURL(loadingHtml()).catch(error => {
      console.error('[electron] loading page load failed', error);
    });
    console.log('[electron] starting web service...');
    webStartup = await startWebService(onWebReady);
  }

  // Packaged port=0 mode: finalize the Chromium-chosen CDP port (must precede
  // the target resolver's ensureBrowserView and any agent/gateway spawn).
  if (cdpPortResolution) {
    cdpPort = await cdpPortResolution;
    cdpPortResolution = null;
    if (!(cdpPort > 0)) {
      hasCdp = false;
      console.warn('[electron] DevToolsActivePort did not appear; browser sideview disabled');
    }
  }
  if (hasCdp) {
    console.log('[electron] starting browser target resolver...');
    browserTargetResolver = await startBrowserTargetResolver();
    console.log('[electron] browser target resolver ready', {
      endpoint: `http://${BACKEND_HOST}:${browserTargetResolver.port}`,
    });
  } else {
    console.log('[electron] CDP port not configured; browser sideview disabled (packaged mode)');
  }

  if (isFrontendOnly) {
    console.log('[electron] FrontendOnly mode: loading local dist, connecting to local backend at 127.0.0.1:19000');
    const frontendUrl = frontendOnlyUrl();
    currentFrontendUrl = frontendUrl;
    console.log('[electron] loading frontend:', frontendUrl);
    await mainWindow.loadURL(frontendUrl);
    mainWindow.show();
    mainWindow.focus();
    if (process.platform === 'darwin') app.focus({ steal: true });
    return;
  }

  console.log('[electron] starting backend services...');
  await startBackendServices(webStartup);
  const frontendUrl = `http://${FRONTEND_HOST}:${webStartup.ports.frontend}`;
  currentFrontendUrl = frontendUrl;
  if (!navigated) {
    console.log('[electron] loading frontend:', frontendUrl);
    await mainWindow.loadURL(frontendUrl);
    mainWindow.show();
    mainWindow.focus();
    if (process.platform === 'darwin') app.focus({ steal: true });
  }
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  });

  app.whenReady().then(async () => {
    // 每会话 partition 的 permission handler 在 ensureBrowserView 内按需注册。
    // 先读回上次运行保存的会话页面 URL（重启还原），再进入启动流程。
    loadSessionLastUrls();
    if (cdpPortPending) {
      cdpPortResolution = resolveCdpPortFromDevToolsActivePort();
    }
    if (VITE_DEV_MODE) {
      await session.defaultSession.clearCache();
      await session.defaultSession.clearStorageData({ storages: ['serviceworkers'] });
    }
    try {
      await createMainWindow();
    } catch (error) {
      if (shuttingDown) return;
      await showStartupFailure(error);
    }
  });
}

for (const [signal, exitCode] of [
  ['SIGINT', 130],
  ['SIGTERM', 143],
]) {
  process.on(signal, () => requestShutdown(exitCode));
}
process.on('message', message => {
  if (message?.type === 'jiuwenswarm:shutdown') {
    requestShutdown(Number(message.exitCode) || 0);
  }
});

app.on('before-quit', event => {
  if (shutdownComplete) return;
  event.preventDefault();
  // 冲刷待写的会话页面 URL（取消防抖定时器），保证重启还原不丢最后一次导航。
  saveSessionLastUrls();
  void stopServices().finally(() => {
    shutdownComplete = true;
    app.exit(requestedExitCode);
  });
});
app.on('window-all-closed', () => app.quit());
process.once('exit', forceStopServices);
