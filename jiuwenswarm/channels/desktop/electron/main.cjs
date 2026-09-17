const { app, BrowserWindow, clipboard, dialog, ipcMain, net, session, shell, WebContentsView } = require('electron');
const { spawn, execFile } = require('node:child_process');
const { randomBytes, randomUUID } = require('node:crypto');
const { inspect } = require('node:util');
const fs = require('node:fs/promises');
const fsSync = require('node:fs');
const nodeHttp = require('node:http');
const nodeNet = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { Readable } = require('node:stream');
const { pipeline } = require('node:stream/promises');
const { SHARED_BROWSER_PARTITION, panelIdentity, hasLiveLease, evictionCandidates, createTargetHandler } = require('./browser_panels.cjs');

const BACKEND_HOST = '127.0.0.1';
const FRONTEND_HOST = '127.0.0.1';
// Port-group layout mirrors jiuwenswarm.instance_manager.config.BASE_PORTS:
// one instance index occupies all four ports (+ index * 1000).
const BASE_PORTS = { agentServer: 18092, gatewayApi: 19000, gatewayInternal: 19001, frontend: 5173 };
// Aligned with desktop_app.STARTUP_TIMEOUT_SECONDS: gateway binds the API port only
// after its ~60s agent-connect budget, so this must stay above that.
const STARTUP_TIMEOUT_MS = 120_000;
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
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const DESKTOP_BLOB_CHUNK_SIZE = 1024 * 1024;
const MAX_JAVASCRIPT_SAFE_INTEGER = 9_007_199_254_740_991;
// 桌面锁定: 每次启动生成一次性 token(对齐 desktop_app 的 token_urlsafe(32)),
// 仅注入 web 静态服务子进程; 首导航 URL 携带 ?dt=<token> 换取 HttpOnly Cookie,
// 本机浏览器直开对话页时返回 403。
const desktopLockToken = randomBytes(32).toString('base64url');
const browserResolverToken = randomBytes(32).toString('base64url');
const forceManagedBrowser = /^(1|true|yes|on)$/i.test(process.env.JIUWENSWARM_BROWSER_FORCE_MANAGED || '');
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

// 对齐 desktop_app.DESKTOP_PORT_SCAN_RANGE = 10: base + index*1000 最多扫 10 组。
async function findAvailablePorts(scanRange = 10) {
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
// Pages are isolated by conversation/member; their persistent login profile is shared.
const browserViews = new Map();
const browserPanelIdentities = new Map();
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
  if (browserTargetResolver && !forceManagedBrowser) {
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
      PLAYWRIGHT_MCP_TARGET_RESOLVER_TOKEN: browserResolverToken,
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
  if (forceManagedBrowser) env.BROWSER_DRIVER = 'managed';
  // Mirror the Python desktop child env contract (desktop_app._build_child_env):
  // inject the full session port group so agent/gateway/web agree, and let the
  // children skip workspace preparation because the launcher did it once.
  env.JIUWENSWARM_RUNTIME_WORKSPACE_READY = '1';
  // 桌面锁定契约(desktop_app._build_child_env): 仅 web 静态服务注入 token,
  // 其他子进程不携带; Vite dev 模式的 web 子进程是 Vite 而非 app_web, 同样不注入。
  if (name === 'web' && !VITE_DEV_MODE) {
    env.JIUWENSWARM_DESKTOP_TOKEN = desktopLockToken;
  } else {
    delete env.JIUWENSWARM_DESKTOP_TOKEN;
  }
  // 启动诊断契约(desktop_app._build_child_env): 子进程失败时把 failure-*.json
  // 写入本会话诊断目录(冻结 exe 入口的 _write_child_error)。
  if (startupDiagnosticsDir) {
    env[STARTUP_DIAGNOSTICS_DIR_ENV] = startupDiagnosticsDir;
  }
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
      if (child.exitCode !== null || child.signalCode !== null) {
        reject(new Error(`Service for ${host}:${port} exited with code ${child.exitCode ?? `signal ${child.signalCode}`}`));
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
    if (child && (child.exitCode !== null || child.signalCode !== null)) {
      throw new Error(`Service for HTTP ${url} exited with code ${child.exitCode ?? `signal ${child.signalCode}`}`);
    }
    try {
      const response = await net.fetch(url);
      // 与 desktop_app._wait_for_http 一致: <500 即就绪(403 桌面锁定页等 4xx
      // 也算服务已起); 5xx 说明服务起来了但内部错误, 继续等待。
      if (response.status > 0 && response.status < 500) return;
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
    const env = {
      ...process.env,
      JIUWENSWARM_DESKTOP: '1',
      JIUWENSWARM_ELECTRON: '1',
    };
    if (startupDiagnosticsDir) {
      env[STARTUP_DIAGNOSTICS_DIR_ENV] = startupDiagnosticsDir;
    }
    const child = spawn(command, args, {
      cwd: cwd ?? serviceWorkingDirectory(),
      env,
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

// ─── Gateway 单例预检 ────────────────────────────────────────────────────────
// 对齐 desktop_app._preflight_gateway_singleton: 权威互斥由 Gateway 进程自身的
// OS 级文件锁(~/.jiuwenswarm/.gateway.lock.lock, portalocker 独占字节锁)保证;
// 预检只负责提前给出明确报错, 并等待升级重启中正在退出的旧 Gateway 释放
// (最多 15s, 每 0.5s 轮询)。
const GATEWAY_LOCK_FILENAME = '.gateway.lock';
const GATEWAY_PREFLIGHT_WAIT_MS = 15_000;

function userWorkspaceDir() {
  return path.join(app.getPath('home'), '.jiuwenswarm');
}

function isPidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === 'EPERM';
  }
}

function isGatewayOsLockHeld(workspaceDir) {
  // 伴生 OS 锁文件被持有方以 [0,0x10000) 独占字节锁锁住(实测 Node readSync
  // 报 EBUSY)。文件缺失视为无持有者(对齐 Python 探测的 a+ 创建语义);
  // 其他打开失败按已持有处理(安全默认, 对齐 Python 的 OSError→True)。
  const osLockPath = path.join(workspaceDir, `${GATEWAY_LOCK_FILENAME}.lock`);
  let fd;
  try {
    fd = fsSync.openSync(osLockPath, 'r+');
  } catch (error) {
    return error?.code !== 'ENOENT';
  }
  try {
    fsSync.readSync(fd, Buffer.alloc(1), 0, 1, 0);
    return false;
  } catch {
    return true;
  } finally {
    try { fsSync.closeSync(fd); } catch { /* ignore */ }
  }
}

function findGatewayLockHolder(workspaceDir) {
  // 对齐 GatewayLock.find_holder: 元数据 pid 存活且伴生 OS 锁仍被持有才算数
  // (防 PID 复用误报)。.gateway.lock 元数据文件永不加锁、永远可读。
  const lockPath = path.join(workspaceDir, GATEWAY_LOCK_FILENAME);
  let data;
  try {
    data = JSON.parse(fsSync.readFileSync(lockPath, 'utf8'));
  } catch {
    return null;
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  const pid = Number(data.pid) || 0;
  if (pid <= 0 || !isPidAlive(pid)) return null;
  if (!isGatewayOsLockHeld(workspaceDir)) return null;
  return data;
}

async function preflightGatewaySingleton() {
  const workspaceDir = userWorkspaceDir();
  let holder = findGatewayLockHolder(workspaceDir);
  if (holder === null) return;
  const deadline = Date.now() + GATEWAY_PREFLIGHT_WAIT_MS;
  while (holder !== null && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 500));
    holder = findGatewayLockHolder(workspaceDir);
  }
  if (holder !== null) {
    console.error('[electron] another Gateway is already serving this workspace', {
      pid: holder.pid,
      workspace: holder.workspace,
    });
    throw new Error(
      `Another Gateway instance is running (pid=${holder.pid}, `
      + `workspace=${holder.workspace}). Stop the existing one first.`,
    );
  }
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

function desktopEntryUrl(baseUrl) {
  // 对齐 desktop_app.DesktopRuntime.frontend_url: 首导航带 ?dt=<token>,
  // web 静态服务据此下发 HttpOnly Cookie 并 302 到去掉 token 的干净 URL。
  return `${baseUrl}/?dt=${encodeURIComponent(desktopLockToken)}`;
}

async function startWebService(onWebReady) {
  // 诊断会话目录必须在首个子进程 spawn 前创建(env 注入)。
  createStartupDiagnosticsSession();
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
    const baseUrl = `http://${FRONTEND_HOST}:${ports.frontend}`;
    // 日志只记不含 token 的展示 URL(desktop_app.frontend_display_url 语义)。
    console.log(`[electron] web ready, navigating early to ${baseUrl}`);
    const entryUrl = VITE_DEV_MODE ? baseUrl : desktopEntryUrl(baseUrl);
    if (typeof onWebReady === 'function') onWebReady(entryUrl);
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
  // 健康启动不产生 failure 记录, 删除空的会话诊断目录避免累积
  // (对齐 desktop_app; 非空目录保留)。
  if (startupDiagnosticsDir) {
    await fs.rmdir(startupDiagnosticsDir).catch(() => {});
  }
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
  stopBrowserEndpointsPublisher();
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
<div class="app-name">WorkSwarm</div>
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
  const identity = {
    panelId: entry?.panelId ?? '',
    sessionId: entry?.sessionId ?? '',
    memberId: entry?.memberId ?? '',
    label: entry?.label ?? '',
    busy: entry?.leases ? hasLiveLease(entry) : false,
  };
  const view = entry?.view;
  if (!view || view.webContents.isDestroyed()) {
    return {
      ...identity,
      url: sessionLastUrls.get(entry?.panelId) || '',
      title: '',
      loading: false,
      canGoBack: false,
      canGoForward: false,
    };
  }
  const history = view.webContents.navigationHistory;
  return {
    ...identity,
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

function emitBrowserPanels() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('browser:panels-changed', allBrowserPanels());
  }
}

function allBrowserPanels() {
  return [...browserPanelIdentities.values()].map(identity =>
    currentBrowserState(browserViews.get(identity.panelId) || identity));
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
    const candidates = evictionCandidates(browserViews, preserveSessionId, activePaneSessionId);
    // The limit is soft: live MCP owners are never evicted, even while hidden.
    if (candidates.length === 0) return;
    const [victimId, victim] = candidates[0];
    browserViews.delete(victimId);
    emitBrowserPanels();
    try {
      mainWindow?.contentView?.removeChildView(victim.view);
      victim.view.webContents.close();
    } catch (error) {
      console.warn('[electron] failed to close evicted sideview', victimId, error);
    }
  }
}

async function ensureBrowserView(sessionId, lease = null) {
  if (!hasCdp || !mainWindow || mainWindow.isDestroyed()) return null;
  const identity = typeof sessionId === 'object' ? sessionId
    : browserPanelIdentities.get(normalizeBrowserSessionId(sessionId)) || panelIdentity(sessionId);
  const key = identity.panelId;
  browserPanelIdentities.set(key, identity);
  const existing = browserViews.get(key);
  if (existing && !existing.view.webContents.isDestroyed()) {
    existing.lastActive = Date.now();
    if (lease) existing.leases.set(lease.leaseId, lease.ownerPid);
    return existing.ready;
  }
  if (existing) browserViews.delete(key);
  evictIdleBrowserViews(key);

  const partition = SHARED_BROWSER_PARTITION;
  const partitionSession = session.fromPartition(partition);
  partitionSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  const view = new WebContentsView({
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      backgroundThrottling: false,
      partition,
    },
  });
  mainWindow.contentView.addChildView(view);
  const entry = {
    ...identity, view, targetId: '', crashCount: 0, lastActive: Date.now(), visible: false,
    creating: true, leases: new Map(), ready: null,
  };
  // Initialize before clipping to the window: a minimized window or stale pane
  // rectangle may be empty, but a newly created background page must not be.
  const contentBounds = mainWindow.getContentBounds();
  const zoomFactor = mainWindow.webContents.getZoomFactor();
  const initialWidth = Math.round((Number(lastBrowserBounds?.width) || 0) * zoomFactor);
  const initialHeight = Math.round((Number(lastBrowserBounds?.height) || 0) * zoomFactor);
  entry.viewportSize = {
    width: initialWidth > 0 ? initialWidth : Math.max(contentBounds.width, 1024),
    height: initialHeight > 0 ? initialHeight : Math.max(contentBounds.height, 768),
  };
  view.setBounds({ x: 0, y: 0, ...entry.viewportSize });
  view.setVisible(false);
  applyBrowserBounds(entry, lastBrowserBounds?.width > 0 && lastBrowserBounds?.height > 0
    ? lastBrowserBounds : { x: 0, y: 0, width: contentBounds.width, height: contentBounds.height });
  if (lease) entry.leases.set(lease.leaseId, lease.ownerPid);
  browserViews.set(key, entry);
  entry.ready = initializeBrowserView(entry).catch(error => {
    if (browserViews.get(key) === entry) browserViews.delete(key);
    mainWindow?.contentView?.removeChildView(view);
    if (!view.webContents.isDestroyed()) view.webContents.close();
    emitBrowserPanels();
    throw error;
  }).finally(() => { entry.creating = false; });
  emitBrowserPanels();
  return entry.ready;
}

async function initializeBrowserView(entry) {
  const { view, panelId: key } = entry;
  view.webContents.on('did-finish-load', () => {
    entry.loaded = true;
    syncBrowserViewport(entry);
  });

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
    entry.loaded = false;
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
  if (!(Number(bounds?.width) > 0 && Number(bounds?.height) > 0)) return entry.view.getBounds();
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
  if (width === 0 || height === 0) return entry.view.getBounds();
  entry.view.setBounds({ x, y, width, height });
  // Native hidden views can have a zero layout viewport on Windows. Keep the
  // renderer's desktop viewport in sync with the pane even when not displayed.
  entry.viewportSize = { width, height };
  syncBrowserViewport(entry);
  entry.view.setVisible(entry.visible);
  return { x, y, width, height };
}

function syncBrowserViewport(entry) {
  if (!entry.loaded || !entry.viewportSize || entry.view.webContents.isDestroyed()) return;
  entry.view.webContents.enableDeviceEmulation({
    screenPosition: 'desktop', viewSize: entry.viewportSize, deviceScaleFactor: 0, scale: 1,
  });
}

function hideBrowserView(entry) {
  entry.visible = false;
  entry.view.setVisible(false);
}

function setBrowserPaneVisible(sessionId, visible, focus = true) {
  const key = normalizeBrowserSessionId(sessionId);
  if (!visible) {
    const entry = browserViews.get(key);
    if (entry) {
      hideBrowserView(entry);
    }
    if (activePaneSessionId === key) activePaneSessionId = '';
    return false;
  }
  activePaneSessionId = key;
  // 同屏只允许一个会话的视图：激活前先隐藏其它会话视图。
  for (const [sid, entry] of browserViews) {
    if (sid !== key) {
      hideBrowserView(entry);
    }
  }
  // 视图可能尚未创建（首次切到 browser 页签）：创建完成后兜底应用边界与可见性。
  void ensureBrowserView(key)
    .then(entry => {
      if (!entry || activePaneSessionId !== key) return;
      entry.visible = true;
      entry.view.setVisible(true);
      if (lastBrowserBounds) applyBrowserBounds(entry, lastBrowserBounds);
      // focus=false 用于模态弹窗关闭后的恢复显示：不抢主窗口焦点。
      if (entry.visible && focus) entry.view.webContents.focus();
      emitBrowserState(entry);
    })
    .catch(error => console.warn('[electron] sideview activation failed', { sessionId: key, error }));
  return true;
}

// ─── 浏览器端点发现文件（跨进程交接）──────────────────────────────────────
// Electron 的 CDP 端口与 target resolver 端口每次启动随机分配，且只在
// spawnService 里注入给亲自拉起的后端。FrontendOnly 场景后端由外部启动，
// 因此把端点心跳写入用户数据目录的 runtime_state/ 下；外部后端必须设置
// JIUWENSWARM_ELECTRON_BROWSER=1 才能读取，不能覆盖已有 managed 配置。
const BROWSER_ENDPOINTS_FILE_NAME = 'electron-browser-endpoints.json';
const BROWSER_ENDPOINTS_HEARTBEAT_MS = 10_000;
let browserEndpointsTimer = null;

function browserEndpointsFilePath() {
  const dataDir = process.env.JIUWENSWARM_DATA_DIR || path.join(app.getPath('home'), '.jiuwenswarm');
  return path.join(dataDir, 'runtime_state', BROWSER_ENDPOINTS_FILE_NAME);
}

function buildBrowserEndpointsPayload() {
  const cdpEndpoint = `http://${BACKEND_HOST}:${cdpPort}`;
  const targetResolver = `http://${BACKEND_HOST}:${browserTargetResolver.port}`;
  const envJson = {
    PLAYWRIGHT_MCP_CDP_ENDPOINT: cdpEndpoint,
    PLAYWRIGHT_MCP_TARGET_RESOLVER: targetResolver,
    PLAYWRIGHT_MCP_TARGET_RESOLVER_TOKEN: browserResolverToken,
    // 打包版 wrapper 以本应用 exe 的 Node 模式运行；该开关需经
    // PLAYWRIGHT_MCP_ENV_JSON 白名单转发进 MCP 子进程。
    ...(app.isPackaged ? { ELECTRON_RUN_AS_NODE: '1' } : {}),
  };
  return {
    pid: process.pid,
    app_type: isFrontendOnly ? 'frontend-only' : (app.isPackaged ? 'packaged' : 'dev'),
    cdp_endpoint: cdpEndpoint,
    target_resolver: targetResolver,
    // 与 spawnService 的 target wrapper 契约一致（main.cjs spawnService 分支）。
    mcp_command: app.isPackaged ? process.execPath : 'npx',
    mcp_args: app.isPackaged
      ? [TARGET_MCP_WRAPPER_PATH]
      : ['-y', '--package', PLAYWRIGHT_MCP_PACKAGE, 'node', TARGET_MCP_WRAPPER_PATH],
    env_json: envJson,
    written_at: Date.now(),
  };
}

function writeBrowserEndpointsFile() {
  const target = browserEndpointsFilePath();
  const tmp = `${target}.tmp-${process.pid}`;
  try {
    fsSync.mkdirSync(path.dirname(target), { recursive: true });
    fsSync.writeFileSync(tmp, JSON.stringify(buildBrowserEndpointsPayload()));
    fsSync.renameSync(tmp, target);
  } catch (error) {
    try { fsSync.rmSync(tmp, { force: true }); } catch { /* best effort */ }
    console.warn('[electron] failed to publish browser endpoints file', error);
  }
}

function startBrowserEndpointsPublisher() {
  writeBrowserEndpointsFile();
  if (browserEndpointsTimer) clearInterval(browserEndpointsTimer);
  browserEndpointsTimer = setInterval(writeBrowserEndpointsFile, BROWSER_ENDPOINTS_HEARTBEAT_MS);
  browserEndpointsTimer.unref();
}

function stopBrowserEndpointsPublisher() {
  if (browserEndpointsTimer) {
    clearInterval(browserEndpointsTimer);
    browserEndpointsTimer = null;
  }
  // 仅删除自己写的文件；另一只 Electron（last-writer-wins）的文件保留。
  const target = browserEndpointsFilePath();
  try {
    const raw = fsSync.readFileSync(target, 'utf8');
    if (JSON.parse(raw).pid === process.pid) fsSync.rmSync(target, { force: true });
  } catch { /* missing or not ours: leave it */ }
}

function startBrowserTargetResolver() {
  return new Promise((resolve, reject) => {
    const server = nodeHttp.createServer(createTargetHandler({
      token: browserResolverToken,
      ensureView: ensureBrowserView,
      getView: id => browserViews.get(id),
      changed: emitBrowserPanels,
    }));
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

function failureHtml(status) {
  // 对齐 Python 桌面失败页（desktop_app._build_loading_html 的 error-panel +
  // _build_failed_status 的标题/原因/诊断路径）：深色主题、错误原因可选中复制、
  // 日志与诊断文件路径、退出按钮。窗口保留由用户退出，
  // 不做"弹原生框即退出"。
  const logoSvg = resolveLogoSvg();
  const diagnosticMeta = status.diagnosticPath
    ? `\n诊断文件：${escapeHtml(status.diagnosticPath)}`
    : '';
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
<div class="error-title">${escapeHtml(status.title)}</div>
<div class="error-message">${escapeHtml(status.message)}</div>
<div class="error-meta">日志文件：${escapeHtml(mainLogPath())}${diagnosticMeta}

排查时可将上述文件提供给支持人员。</div>
<div class="hint">若为安装后的首次启动，常见原因是安全软件扫描新文件导致的瞬时故障，重新启动应用通常可恢复。</div>
<button class="close-button" id="close-button" type="button">退出 WorkSwarm</button>
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

function diagnosingHtml() {
  // 对齐 Python 桌面 diagnosing 态（"正在诊断启动失败原因"）：doctor 运行期间
  // （最长 60s）的过渡页，避免用户面对空白或旧页面。
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
.panel{display:flex;flex-direction:column;align-items:center;gap:24px;padding:40px}
.logo svg{width:64px;height:64px;border-radius:16px}
.error-icon{width:52px;height:52px;border-radius:50%;display:flex;align-items:center;
justify-content:center;background:rgba(239,68,68,.14);color:#f87171;font-size:30px;font-weight:700}
.error-title{font-size:20px;font-weight:700;color:#f8fafc}
.error-message{color:#cbd5e1;font-size:13px;line-height:1.7}
.spinner{width:32px;height:32px;border:3px solid rgba(148,163,184,.2);
border-top-color:#60a5fa;border-radius:50%;animation:spin 1.5s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="panel">
<div class="logo">${logoSvg}</div>
<div class="error-icon">!</div>
<div class="error-title">正在诊断启动失败原因</div>
<div class="error-message">服务启动失败，正在检查本机运行环境…</div>
<div class="spinner"></div>
</div>
</body>
</html>`;
  return 'data:text/html;charset=utf-8;base64,' + Buffer.from(html, 'utf8').toString('base64');
}

async function showStartupFailure(error) {
  // 对齐 Python 桌面的失败呈现链（start_services 异常分支）：读子进程 failure
  // 记录 → 按需运行 doctor（期间显示 diagnosing 页）→ 构建 failed status →
  // 失败页呈现诊断信息，用户自行退出；残余服务树立即清理，
  // 但不退出应用。仅当窗口本身不可用时才回退到原生错误框 + 退出。
  console.error('[electron] startup failed', error);
  if (!mainWindow || mainWindow.isDestroyed()) {
    const detail = error instanceof Error ? `${error.message}\n${error.stack || ''}` : String(error);
    dialog.showErrorBox('WorkSwarm failed to start', detail);
    requestShutdown(1);
    return;
  }
  void stopServices();
  let status;
  try {
    const records = await loadStartupFailures();
    const childFailure = selectStartupFailure(records);
    let doctorResult = null;
    if (shouldRunStartupDoctor(error, childFailure)) {
      try {
        await mainWindow.loadURL(diagnosingHtml());
        mainWindow.show();
        mainWindow.focus();
      } catch (loadError) {
        console.error('[electron] failed to render diagnosing page', loadError);
      }
      doctorResult = await runStartupDoctor();
    } else {
      console.log('[electron] startup doctor skipped; failure already has a non-native cause');
    }
    status = buildStartupFailureStatus(error, doctorResult, childFailure);
  } catch (diagnosticError) {
    console.error('[electron] failed to build startup diagnostics', diagnosticError);
    status = {
      title: 'WorkSwarm 服务启动失败',
      message: shortStartupError(error?.message) || '未知启动错误',
      component: 'desktop-startup',
      diagnosticPath: startupDiagnosticsDir,
    };
  }
  try {
    await mainWindow.loadURL(failureHtml(status));
    mainWindow.show();
    mainWindow.focus();
  } catch (loadError) {
    console.error('[electron] failed to render startup failure page', loadError);
    dialog.showErrorBox('WorkSwarm failed to start', status.message);
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

// ─── Blob 分块保存事务 ──────────────────────────────────────────────────────
// 契约对齐 Python 桌面 desktop_app 的 begin/append/finish/abort_blob_save：
// 元数据白名单、1MiB 分块、校验 PNG 签名、同目录临时文件 + 原子替换。
const BLOB_EXPORT_SPECS = {
  'image/png': { suffixes: ['.png'], parameters: [], filter: { name: 'PNG Image', extensions: ['png'] } },
  'image/svg+xml': { suffixes: ['.svg'], parameters: ['charset=utf-8'], filter: { name: 'SVG Image', extensions: ['svg'] } },
  'text/plain': { suffixes: ['.mmd'], parameters: ['charset=utf-8'], filter: { name: 'Mermaid Diagram', extensions: ['mmd'] } },
  'application/json': { suffixes: ['.json'], parameters: ['charset=utf-8'], filter: { name: 'JSON Archive', extensions: ['json'] } },
};
const BASE64_CHUNK_PATTERN = /^[A-Za-z0-9+/]+={0,2}$/;
const blobSaveTransfers = new Map();

function resolveBlobExport(filename, mimeType, totalSize) {
  const safeName = sanitizeFilename(filename, '');
  if (!safeName) throw new Error('empty_filename');
  if (typeof totalSize !== 'number' || !Number.isInteger(totalSize)
    || totalSize < 0 || totalSize > MAX_JAVASCRIPT_SAFE_INTEGER) {
    throw new Error('invalid_blob_size');
  }
  if (typeof mimeType !== 'string') throw new Error('invalid_blob_mime_type');
  const metadata = mimeType.split(';').map(part => part.trim().toLowerCase());
  const spec = BLOB_EXPORT_SPECS[metadata[0]];
  if (!spec) throw new Error('unsupported_blob_mime_type');
  const parameters = metadata.slice(1);
  if (new Set(parameters).size !== parameters.length
    || parameters.some(parameter => !spec.parameters.includes(parameter))) {
    throw new Error('unsupported_blob_mime_parameters');
  }
  if (!spec.suffixes.includes(path.extname(safeName).toLowerCase())) {
    throw new Error('blob_filename_extension_mismatch');
  }
  return { safeName, mimeType: metadata[0], spec };
}

async function discardBlobSaveTransfer(transfer) {
  try {
    await transfer.handle.close();
  } catch { /* already closed */ }
  try {
    await fs.rm(transfer.tempPath, { force: true });
  } catch (error) {
    console.warn('[electron] failed to remove partial blob export', transfer.tempPath, error);
  }
}

async function abortAllBlobSaves() {
  const transfers = [...blobSaveTransfers.values()];
  blobSaveTransfers.clear();
  for (const transfer of transfers) {
    await discardBlobSaveTransfer(transfer);
  }
}

// ─── 本机文件选择 / 附件元数据 ───────────────────────────────────────────────
// 契约对齐 desktop_app 的四个 pywebview API(select_local_files /
// select_local_file_path / describe_local_files / get_clipboard_files):
// 白名单对话框过滤、图片内联 base64(≤10MiB)、黑名单扩展名返回 error 字段、
// 上次选择目录记忆(~/.jiuwenswarm/last_file_picker_dir.txt, 与 Python 桌面
// 及 web 通道共享该状态文件)。
const ATTACHMENT_DIALOG_EXTENSIONS = [
  '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.svg', '.ico', '.jfif',
  '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
  '.txt', '.md', '.markdown', '.csv', '.tsv', '.rtf',
  '.odt', '.ods', '.odp', '.json', '.xml', '.yaml', '.yml',
  '.html', '.htm', '.css', '.js', '.ts', '.tsx', '.jsx',
  '.py', '.java', '.c', '.cpp', '.h', '.go', '.rs', '.rb', '.php', '.sql',
  '.ipynb', '.toml', '.ini', '.log',
  '.zip', '.rar', '.7z', '.tar', '.gz',
  '.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.wma',
  '.mp4', '.avi', '.mov', '.mkv', '.webm', '.wmv', '.flv',
];
// 与 file_picker.ATTACHMENT_DIALOG_EXTENSIONS 保持同步: 故意不含黑名单扩展名,
// 也不提供 All files 项。
const ATTACHMENT_DIALOG_FILTER = [{
  name: 'Allowed files',
  extensions: ATTACHMENT_DIALOG_EXTENSIONS.map(ext => ext.slice(1)),
}];
const IMAGE_PICK_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif', '.jfif']);
const MAX_IMAGE_PICK_BYTES = 10 * 1024 * 1024;
const FORBIDDEN_PICK_EXTENSIONS = new Set([
  '.exe', '.dll', '.msi', '.scr', '.bat', '.cmd', '.ps1', '.vbs', '.wsf', '.hta',
  '.jar', '.lnk', '.bin', '.so', '.dylib', '.app', '.dmg', '.pkg', '.command',
  '.scpt', '.scptd', '.workflow', '.xpc', '.bundle', '.framework', '.kext',
  '.prefpane', '.saver', '.component',
]);
const MIME_BY_EXTENSION = new Map(Object.entries({
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
  '.gif': 'image/gif', '.bmp': 'image/bmp', '.svg': 'image/svg+xml',
  '.ico': 'image/vnd.microsoft.icon', '.jfif': 'image/jpeg',
  '.pdf': 'application/pdf', '.doc': 'application/msword',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.xls': 'application/vnd.ms-excel',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.ppt': 'application/vnd.ms-powerpoint',
  '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  '.txt': 'text/plain', '.md': 'text/markdown', '.markdown': 'text/markdown',
  '.csv': 'text/csv', '.tsv': 'text/tab-separated-values', '.rtf': 'application/rtf',
  '.odt': 'application/vnd.oasis.opendocument.text',
  '.ods': 'application/vnd.oasis.opendocument.spreadsheet',
  '.odp': 'application/vnd.oasis.opendocument.presentation',
  '.json': 'application/json', '.xml': 'text/xml', '.yaml': 'text/yaml', '.yml': 'text/yaml',
  '.html': 'text/html', '.htm': 'text/html', '.css': 'text/css', '.js': 'text/javascript',
  '.ts': 'video/mp2t', '.py': 'text/x-python', '.java': 'text/x-java-source',
  '.c': 'text/x-c', '.cpp': 'text/x-c++', '.h': 'text/x-chdr', '.go': 'text/x-go',
  '.rs': 'text/rust', '.rb': 'text/x-ruby', '.php': 'application/x-httpd-php',
  '.sql': 'text/x-sql', '.ipynb': 'application/x-ipynb+json', '.toml': 'application/toml',
  '.log': 'text/plain', '.zip': 'application/zip', '.rar': 'application/vnd.rar',
  '.7z': 'application/x-7z-compressed', '.tar': 'application/x-tar', '.gz': 'application/gzip',
  '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.flac': 'audio/flac', '.aac': 'audio/aac',
  '.ogg': 'audio/ogg', '.m4a': 'audio/mp4', '.wma': 'audio/x-ms-wma',
  '.mp4': 'video/mp4', '.avi': 'video/x-msvideo', '.mov': 'video/quicktime',
  '.mkv': 'video/x-matroska', '.webm': 'video/webm', '.wmv': 'video/x-ms-wmv',
  '.flv': 'video/x-flv',
}));
const LAST_FILE_PICKER_DIR_FILENAME = 'last_file_picker_dir.txt';

function lastFilePickerDirPath() {
  return path.join(app.getPath('home'), '.jiuwenswarm', LAST_FILE_PICKER_DIR_FILENAME);
}

function expandHomePath(input) {
  const trimmed = String(input ?? '').trim();
  if (trimmed === '~' || trimmed.startsWith('~/') || trimmed.startsWith('~\\')) {
    return path.join(app.getPath('home'), trimmed.slice(1));
  }
  return trimmed;
}

async function readRememberedPickerDir() {
  // 与 file_picker.get_last_file_picker_dir 一致: 目录不存在时返回 null。
  try {
    const raw = (await fs.readFile(lastFilePickerDirPath(), 'utf8')).trim();
    if (!raw) return null;
    const stat = await fs.stat(raw).catch(() => null);
    if (!stat?.isDirectory()) return null;
    return raw;
  } catch {
    return null;
  }
}

async function rememberPickerDir(filePath) {
  // 与 file_picker.remember_file_picker_dir 一致: 记住所选文件的父目录。
  try {
    const directory = path.dirname(String(filePath || ''));
    if (!directory) return;
    const stat = await fs.stat(directory).catch(() => null);
    if (!stat?.isDirectory()) return;
    await fs.mkdir(path.dirname(lastFilePickerDirPath()), { recursive: true });
    await fs.writeFile(lastFilePickerDirPath(), `${directory}\n`, 'utf8');
  } catch { /* best effort */ }
}

async function resolveFilePickerInitialDir(initialDir) {
  // explicit → remembered → home(file_picker.resolve_file_picker_initial_dir)。
  for (const raw of [initialDir, await readRememberedPickerDir()]) {
    const expanded = expandHomePath(raw);
    if (!expanded) continue;
    const stat = await fs.stat(expanded).catch(() => null);
    if (stat?.isDirectory()) return expanded;
  }
  return app.getPath('home');
}

async function describeLocalFile(rawPath) {
  // 与 desktop_app._describe_local_file 同形: 非文件跳过; 图片内联 base64,
  // 超限/读失败/黑名单扩展名返回 error 字段, 其余为 document。
  const input = expandHomePath(rawPath);
  if (!input) return null;
  let absolute;
  try {
    absolute = await fs.realpath(input);
  } catch {
    absolute = path.resolve(input);
  }
  const stat = await fs.stat(absolute).catch(() => null);
  if (!stat?.isFile()) {
    console.warn(`[electron] picked path is not a file: ${absolute}`);
    return null;
  }
  const ext = path.extname(absolute).toLowerCase();
  const base = {
    path: absolute,
    filename: path.basename(absolute),
    size: stat.size,
    mime_type: MIME_BY_EXTENSION.get(ext) || 'application/octet-stream',
  };
  if (IMAGE_PICK_EXTENSIONS.has(ext)) {
    if (base.size > MAX_IMAGE_PICK_BYTES) {
      return { ...base, kind: 'image', error: 'image_too_large' };
    }
    try {
      const payload = await fs.readFile(absolute);
      return { ...base, kind: 'image', base64: payload.toString('base64') };
    } catch (error) {
      console.warn(`[electron] failed to read image ${absolute}:`, error?.message || error);
      return { ...base, kind: 'image', error: 'read_failed' };
    }
  }
  if (FORBIDDEN_PICK_EXTENSIONS.has(ext)) {
    return { ...base, kind: 'document', error: 'forbidden' };
  }
  return { ...base, kind: 'document' };
}

async function describeLocalPaths(paths) {
  const results = [];
  for (const raw of paths) {
    const item = await describeLocalFile(raw);
    if (item) results.push(item);
  }
  return results;
}

function clipboardFileNameW() {
  // Explorer 复制文件时写 FileNameW(宽字符、仅单个路径)。
  try {
    if (!clipboard.has('FileNameW')) return null;
    const first = clipboard.readBuffer('FileNameW').toString('ucs2').split('\0', 1)[0];
    const trimmed = first.trim();
    return trimmed || null;
  } catch {
    return null;
  }
}

function readClipboardFileDropList() {
  // Electron clipboard 无法按名读取 CF_HDROP(标准格式, 不经 RegisterClipboardFormat
  // 解析, 已实测 has('CF_HDROP')=false), 多文件列表经 PowerShell GetFileDropList
  // 读取, 与 desktop_app._clipboard_file_paths_windows 的 CF_HDROP 语义一致。
  // 仅在 FileNameW 门控命中(剪贴板确实有文件)后调用; 文本粘贴不触发。
  return new Promise(resolve => {
    const script = '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; '
      + 'Add-Type -AssemblyName System.Windows.Forms; '
      + '[System.Windows.Forms.Clipboard]::GetFileDropList() | ForEach-Object { $_ }';
    execFile(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
      { windowsHide: true, timeout: 8000, maxBuffer: 1024 * 1024, encoding: 'utf8' },
      (error, stdout) => {
        if (error) {
          console.warn('[electron] clipboard FileDropList read failed', error?.message || error);
          resolve([]);
          return;
        }
        resolve(stdout.split(/\r?\n/).map(item => item.trim()).filter(Boolean));
      },
    );
  });
}

function clipboardFilePathsMacos() {
  // best effort: NSFilenamesPboardType 是 plist XML, 提取 <string> 路径项。
  try {
    const raw = clipboard.read('NSFilenamesPboardType');
    if (!raw || !raw.includes('<string>')) return [];
    return [...raw.matchAll(/<string>([^<]*)<\/string>/g)]
      .map(match => match[1]
        .replaceAll('&lt;', '<')
        .replaceAll('&gt;', '>')
        .replaceAll('&quot;', '"')
        .replaceAll('&apos;', "'")
        .replaceAll('&amp;', '&')
        .trim())
      .filter(Boolean);
  } catch {
    return [];
  }
}

async function clipboardFilePaths() {
  // 对齐 desktop_app._clipboard_file_paths: Windows 多文件、macOS 尽力、Linux 空。
  if (process.platform === 'win32') {
    if (!clipboard.has('FileNameW')) return [];
    const multi = await readClipboardFileDropList();
    if (multi.length) return multi;
    const single = clipboardFileNameW();
    return single ? [single] : [];
  }
  if (process.platform === 'darwin') return clipboardFilePathsMacos();
  return [];
}

// ─── 启动诊断 / doctor ───────────────────────────────────────────────────────
// 对齐 desktop_app 的启动诊断链: 每次启动创建会话诊断目录并注入子进程
// (JIUWENSWARM_STARTUP_DIAGNOSTICS_DIR), 冻结 exe 子进程失败时把 failure-*.json
// 写入该目录; 启动失败后按需运行后端 --doctor 自检, 用 child failure + doctor
// 结果构建明确的失败页信息(_build_failed_status 同形契约)。
const STARTUP_DIAGNOSTICS_DIR_ENV = 'JIUWENSWARM_STARTUP_DIAGNOSTICS_DIR';
// desktop_app.STARTUP_DOCTOR_TIMEOUT_SECONDS = DOCTOR_TIMEOUT_SECONDS(45) + 15
const STARTUP_DOCTOR_TIMEOUT_MS = 60_000;
const FAILURE_MESSAGE_MAX_CHARS = 700;
const NATIVE_FAILURE_MARKERS = [
  'dll load failed',
  '.pyd',
  'dynamic module',
  'native extension',
  'cannot open shared object file',
  'failed to load shared library',
  'mach-o',
];
// startup_diagnostics._NATIVE_SYSTEM_DEPENDENCIES: 扩展与其已知系统级前置库,
// 两者同时失败时优先报系统库为根因。
const NATIVE_SYSTEM_DEPENDENCIES = new Map([
  ['grpc._cython.cygrpc', ['dbghelp.dll']],
]);
let startupDiagnosticsDir = null;
let doctorOutputPath = null;

function createStartupDiagnosticsSession() {
  // 对齐 DesktopRuntime.__init__: ~/.jiuwenswarm/agent/.logs/startup/<id>,
  // 创建失败时回退到系统临时目录。
  const startupId = randomUUID().replaceAll('-', '');
  const preferred = path.join(
    app.getPath('home'), '.jiuwenswarm', 'agent', '.logs', 'startup', startupId,
  );
  try {
    fsSync.mkdirSync(preferred, { recursive: true });
    startupDiagnosticsDir = preferred;
  } catch (error) {
    console.warn('[electron] failed to create startup diagnostics dir', error);
    const fallback = path.join(os.tmpdir(), 'jiuwenswarm-startup', startupId);
    fsSync.mkdirSync(fallback, { recursive: true });
    startupDiagnosticsDir = fallback;
  }
  doctorOutputPath = path.join(startupDiagnosticsDir, 'doctor.json');
}

async function loadStartupFailures() {
  // 对齐 load_startup_failures: 只读本会话目录, 按时间升序。
  const records = [];
  if (!startupDiagnosticsDir) return records;
  let entries;
  try {
    entries = await fs.readdir(startupDiagnosticsDir);
  } catch {
    return records;
  }
  for (const name of entries) {
    if (!name.startsWith('failure-') || !name.endsWith('.json')) continue;
    try {
      const payload = JSON.parse(await fs.readFile(path.join(startupDiagnosticsDir, name), 'utf8'));
      if (payload && typeof payload === 'object' && payload.type === 'startup_failure') {
        payload.diagnostic_path = path.join(startupDiagnosticsDir, name);
        records.push(payload);
      }
    } catch { /* unreadable record: skip */ }
  }
  records.sort((a, b) => String(a.timestamp_utc || '').localeCompare(String(b.timestamp_utc || '')));
  return records;
}

function isNativeStartupFailure(record) {
  // 对齐 is_native_startup_failure: import 错误或 native 加载特征。
  if (!record) return false;
  const errorType = String(record.error_type || '');
  if (errorType === 'ImportError' || errorType === 'ModuleNotFoundError') return true;
  const details = `${record.message || ''}\n${record.traceback || ''}`.toLowerCase();
  return NATIVE_FAILURE_MARKERS.some(marker => details.includes(marker));
}

function selectStartupFailure(records) {
  // 对齐 select_startup_failure: 优先可操作的 native/import 错误
  // (×10), 其次非 SystemExit(×1), 同分取时间序最早的(与 Python max 一致)。
  let best = null;
  let bestScore = -1;
  for (const record of records) {
    const score = (isNativeStartupFailure(record) ? 10 : 0)
      + (String(record.error_type || '') !== 'SystemExit' ? 1 : 0);
    if (score > bestScore) {
      bestScore = score;
      best = record;
    }
  }
  return best;
}

function shouldRunStartupDoctor(error, childFailure) {
  // 对齐 _should_run_startup_doctor: 仅打包版; native 失败必跑;
  // 无失败记录/SystemExit 且错误文本含子进程退出码时跑(诊断未知退出)。
  if (!app.isPackaged) return false;
  if (isNativeStartupFailure(childFailure)) return true;
  const childErrorType = String((childFailure || {}).error_type || '');
  const unexplainedChildExit = childFailure == null || childErrorType === 'SystemExit';
  return unexplainedChildExit && String(error?.message || '').includes('exited with code');
}

function runStartupDoctor() {
  // 对齐 _run_doctor_after_failure: 后端 exe --doctor(自带 45s supervisor 超时),
  // 结果写 doctor.json; 外层 60s 硬超时, 超时杀树。
  if (!app.isPackaged) return Promise.resolve(null);
  console.log('[electron] running startup doctor after service failure');
  return new Promise(resolve => {
    let child;
    try {
      const env = {
        ...process.env,
        JIUWENSWARM_DESKTOP: '1',
        JIUWENSWARM_ELECTRON: '1',
      };
      if (startupDiagnosticsDir) env[STARTUP_DIAGNOSTICS_DIR_ENV] = startupDiagnosticsDir;
      child = spawn(backendExecutable(), ['--doctor', '--doctor-output', doctorOutputPath], {
        cwd: serviceWorkingDirectory(),
        env,
        detached: process.platform !== 'win32',
        stdio: app.isPackaged ? ['ignore', logStreamFor('doctor'), logStreamFor('doctor')] : 'inherit',
        windowsHide: true,
        shell: false,
      });
    } catch (error) {
      console.warn('[electron] startup doctor failed to run', error);
      resolve(null);
      return;
    }
    serviceProcesses.set('doctor', child);
    const finish = () => {
      clearTimeout(timer);
      serviceProcesses.delete('doctor');
      resolve(loadDoctorResult());
    };
    const timer = setTimeout(() => {
      console.warn('[electron] startup doctor timed out');
      void terminateService(child);
    }, STARTUP_DOCTOR_TIMEOUT_MS);
    child.once('exit', finish);
    child.once('error', error => {
      console.warn('[electron] startup doctor failed to run', error);
      finish();
    });
  });
}

async function loadDoctorResult() {
  try {
    const payload = JSON.parse(await fs.readFile(doctorOutputPath, 'utf8'));
    if (payload && typeof payload === 'object' && payload.type === 'doctor_result') return payload;
  } catch { /* absent or invalid */ }
  return null;
}

function selectBlockingDoctorCheck(doctorResult, startupFailure) {
  // 对齐 select_blocking_doctor_check: 仅当 doctor 失败项能解释启动阻塞时
  // 上报(匹配失败记录中的 native 组件名; 系统库前置依赖优先报根因)。
  if (!doctorResult || doctorResult.status !== 'environment_error') return null;
  const checks = Array.isArray(doctorResult.checks) ? doctorResult.checks : [];
  const failedChecks = checks.filter(check => check && typeof check === 'object' && check.status === 'failed');
  if (!failedChecks.length) return null;
  if (startupFailure != null && !isNativeStartupFailure(startupFailure)) return null;
  const failureText = startupFailure
    ? `${startupFailure.message || ''}\n${startupFailure.traceback || ''}`.toLowerCase()
    : '';
  const nameAppears = (name, leafName) => failureText.includes(name)
    || (leafName.length >= 4 && failureText.includes(leafName));
  const matchedNativeChecks = [];
  for (const check of failedChecks) {
    if (check.kind !== 'native_import') continue;
    const name = String(check.name || '').toLowerCase();
    if (failureText && nameAppears(name, name.split('.').pop())) matchedNativeChecks.push(check);
  }
  const dependencyCandidates = startupFailure == null
    ? failedChecks.filter(check => check.kind === 'native_import')
    : matchedNativeChecks;
  const failedByName = new Map(failedChecks.map(check => [String(check.name || '').toLowerCase(), check]));
  for (const nativeCheck of dependencyCandidates) {
    const dependencies = NATIVE_SYSTEM_DEPENDENCIES.get(String(nativeCheck.name || '').toLowerCase()) || [];
    for (const dependency of dependencies) {
      const rootCheck = failedByName.get(dependency.toLowerCase());
      if (rootCheck) return rootCheck;
    }
  }
  if (matchedNativeChecks.length) return matchedNativeChecks[0];
  if (startupFailure == null && failedChecks.length === 1) return failedChecks[0];
  return null;
}

function shortStartupError(value, maxChars = FAILURE_MESSAGE_MAX_CHARS) {
  // 对齐 desktop_app._short_error。
  const message = String(value ?? '').trim();
  if (message.length <= maxChars) return message;
  return message.slice(0, maxChars - 3) + '...';
}

function buildStartupFailureStatus(error, doctorResult, childFailure) {
  // 对齐 _build_failed_status: doctor 阻塞项 > 子进程失败记录 > 原始异常。
  const blockingCheck = selectBlockingDoctorCheck(doctorResult, childFailure);
  if (blockingCheck != null) {
    const component = String(blockingCheck.name || 'unknown');
    const displayName = String(blockingCheck.display_name || component);
    return {
      title: '运行环境缺少必要组件',
      message: `${displayName} 无法加载：${shortStartupError(blockingCheck.message || '加载失败')}`,
      component,
      diagnosticPath: doctorOutputPath,
    };
  }
  if (childFailure != null) {
    const role = String(childFailure.process_role || 'service');
    const errorType = String(childFailure.error_type || 'Error');
    return {
      title: 'WorkSwarm 服务启动失败',
      message: `${role}: ${errorType}: ${shortStartupError(childFailure.message || error?.message)}`,
      component: role,
      diagnosticPath: childFailure.diagnostic_path || startupDiagnosticsDir,
    };
  }
  return {
    title: 'WorkSwarm 服务启动失败',
    message: shortStartupError(error?.message) || '未知启动错误',
    component: 'desktop-startup',
    diagnosticPath: startupDiagnosticsDir,
  };
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
  registerHandler('desktop:select-local-files', async (allowMultiple, initialDir) => {
    const startDir = await resolveFilePickerInitialDir(initialDir);
    let result;
    try {
      result = await dialog.showOpenDialog(mainWindow, {
        defaultPath: startDir,
        properties: allowMultiple === false ? ['openFile'] : ['openFile', 'multiSelections'],
        filters: ATTACHMENT_DIALOG_FILTER,
      });
    } catch (error) {
      console.error('[electron] local file picker failed', error);
      return [];
    }
    if (result.canceled || !result.filePaths.length) return [];
    const picks = await describeLocalPaths(result.filePaths);
    if (picks.length) await rememberPickerDir(picks[0].path || result.filePaths[0]);
    return picks;
  });
  registerHandler('desktop:select-local-file-path', async (initialPath, title) => {
    const options = {
      defaultPath: expandHomePath(initialPath) || app.getPath('home'),
      properties: ['openFile'],
    };
    if (typeof title === 'string' && title.trim()) options.title = title.trim();
    if (process.platform === 'win32') {
      options.filters = [
        { name: 'Executable files', extensions: ['exe'] },
        { name: 'All files', extensions: ['*'] },
      ];
    }
    const result = await dialog.showOpenDialog(mainWindow, options);
    if (result.canceled || !result.filePaths.length) return null;
    return path.resolve(result.filePaths[0]);
  });
  registerHandler('desktop:describe-local-files', async paths => {
    const list = typeof paths === 'string' ? [paths]
      : Array.isArray(paths) ? paths.filter(item => typeof item === 'string' && item.trim())
        : [];
    return describeLocalPaths(list);
  });
  registerHandler('desktop:get-clipboard-files', async () => {
    const paths = await clipboardFilePaths();
    return describeLocalPaths(paths);
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
    if (!bytes.subarray(0, 8).equals(PNG_SIGNATURE)) {
      return { ok: false, cancelled: false };
    }
    await fs.writeFile(result.filePath, bytes);
    return { ok: true, cancelled: false };
  });
  registerHandler('desktop:begin-blob-save', async (filename, mimeType, totalSize) => {
    let resolved;
    try {
      resolved = resolveBlobExport(filename, mimeType, totalSize);
    } catch (error) {
      console.error('[electron] invalid blob export metadata', error);
      return { ok: false, cancelled: false };
    }
    let targetPath = null;
    try {
      const result = await dialog.showSaveDialog(mainWindow, {
        defaultPath: path.join(app.getPath('downloads'), resolved.safeName),
        filters: [resolved.spec.filter],
      });
      if (!result.canceled && result.filePath) targetPath = result.filePath;
    } catch (error) {
      console.error('[electron] failed to begin blob export', error);
      return { ok: false, cancelled: false };
    }
    if (targetPath === null) return { ok: false, cancelled: true };
    const tempPath = path.join(
      path.dirname(targetPath),
      `.${path.basename(targetPath)}.${randomUUID().replaceAll('-', '')}.part`,
    );
    let handle;
    try {
      handle = await fs.open(tempPath, 'wx');
    } catch (error) {
      console.error('[electron] failed to create blob export transaction', error);
      return { ok: false, cancelled: false };
    }
    const transferId = randomUUID().replaceAll('-', '');
    blobSaveTransfers.set(transferId, {
      expectedSize: totalSize,
      mimeType: resolved.mimeType,
      targetPath,
      tempPath,
      handle,
      bytesWritten: 0,
      signature: Buffer.alloc(0),
    });
    return { ok: true, cancelled: false, transfer_id: transferId };
  });
  registerHandler('desktop:append-blob-save', async (transferId, encodedChunk) => {
    if (typeof transferId !== 'string') return false;
    const transfer = blobSaveTransfers.get(transferId);
    if (!transfer) return false;
    const maxEncodedSize = Math.floor((DESKTOP_BLOB_CHUNK_SIZE + 2) / 3) * 4;
    try {
      if (typeof encodedChunk !== 'string' || !encodedChunk
        || encodedChunk.length > maxEncodedSize
        || encodedChunk.length % 4 !== 0
        || !BASE64_CHUNK_PATTERN.test(encodedChunk)) {
        throw new Error('invalid_blob_chunk');
      }
      const chunk = Buffer.from(encodedChunk, 'base64');
      if (chunk.length === 0 || chunk.length > DESKTOP_BLOB_CHUNK_SIZE) {
        throw new Error('invalid_blob_chunk_size');
      }
      if (transfer.bytesWritten + chunk.length > transfer.expectedSize) {
        throw new Error('blob_size_exceeded');
      }
      const { bytesWritten } = await transfer.handle.write(chunk);
      if (bytesWritten !== chunk.length) throw new Error('incomplete_blob_chunk_write');
      transfer.bytesWritten += bytesWritten;
      if (transfer.signature.length < PNG_SIGNATURE.length) {
        transfer.signature = Buffer.concat([
          transfer.signature,
          chunk.subarray(0, PNG_SIGNATURE.length - transfer.signature.length),
        ]);
      }
      return true;
    } catch (error) {
      console.error('[electron] failed to append blob export', error);
      blobSaveTransfers.delete(transferId);
      await discardBlobSaveTransfer(transfer);
      return false;
    }
  });
  registerHandler('desktop:finish-blob-save', async transferId => {
    if (typeof transferId !== 'string') return { ok: false, cancelled: false };
    const transfer = blobSaveTransfers.get(transferId);
    if (!transfer) return { ok: false, cancelled: false };
    blobSaveTransfers.delete(transferId);
    try {
      if (transfer.bytesWritten !== transfer.expectedSize) throw new Error('blob_size_mismatch');
      if (transfer.mimeType === 'image/png' && !transfer.signature.equals(PNG_SIGNATURE)) {
        throw new Error('invalid_png_signature');
      }
      await transfer.handle.sync();
      await transfer.handle.close();
      await fs.rename(transfer.tempPath, transfer.targetPath);
      console.log(`[electron] blob export saved to: ${transfer.targetPath}`);
      return { ok: true, cancelled: false };
    } catch (error) {
      console.error('[electron] failed to finish blob export', error);
      await discardBlobSaveTransfer(transfer);
      return { ok: false, cancelled: false };
    }
  });
  registerHandler('desktop:abort-blob-save', async transferId => {
    if (typeof transferId !== 'string') return false;
    const transfer = blobSaveTransfers.get(transferId);
    if (!transfer) return false;
    blobSaveTransfers.delete(transferId);
    await discardBlobSaveTransfer(transfer);
    return true;
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
  registerHandler('browser:list-panels', sessionId => {
    const sid = normalizeBrowserSessionId(sessionId);
    return allBrowserPanels().filter(entry => entry.sessionId === sid);
  });
  registerHandler('browser:set-visible', (visible, sessionId, focus) => {
    return setBrowserPaneVisible(sessionId, Boolean(visible), focus !== false);
  });
  registerHandler('browser:set-bounds', (bounds, sessionId) => {
    lastBrowserBounds = bounds;
    const entry = browserViews.get(normalizeBrowserSessionId(sessionId));
    return applyBrowserBounds(entry, bounds);
  });
}

async function createMainWindow() {
  mainWindow = new BrowserWindow({
    title: 'WorkSwarm',
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
    // currentFrontendUrl 是 download-file 的相对 URL 解析基址, 保持不含 token。
    currentFrontendUrl = new URL(earlyUrl).origin;
    console.log('[electron] early loading frontend:', currentFrontendUrl);
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
    // 对齐 desktop_app.start_services: 任何子进程 spawn 前先做 per-workspace
    // Gateway 单例预检, 冲突时完全不占用端口组(两个 Gateway = 两套
    // CronScheduler = cron 重复执行)。
    await preflightGatewaySingleton();
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
    // 发布发现文件（dev / 完整包 / FrontendOnly 一致）：外部后端据此绑定壳内浏览器。
    startBrowserEndpointsPublisher();
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
  const frontendBase = `http://${FRONTEND_HOST}:${webStartup.ports.frontend}`;
  currentFrontendUrl = frontendBase;
  if (!navigated) {
    const entryUrl = VITE_DEV_MODE ? frontendBase : desktopEntryUrl(frontendBase);
    console.log('[electron] loading frontend:', frontendBase);
    await mainWindow.loadURL(entryUrl);
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
    // 共享浏览器 partition 的 permission handler 在 ensureBrowserView 内按需注册。
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
  void Promise.all([abortAllBlobSaves(), stopServices()]).finally(() => {
    shutdownComplete = true;
    app.exit(requestedExitCode);
  });
});
app.on('window-all-closed', () => app.quit());
app.on('will-quit', () => stopBrowserEndpointsPublisher());
process.once('exit', forceStopServices);
