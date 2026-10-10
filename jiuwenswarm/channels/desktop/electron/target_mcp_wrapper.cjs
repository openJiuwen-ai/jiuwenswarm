#!/usr/bin/env node
'use strict';

// Target-scoped stdio adapter for @playwright/mcp. Electron exposes every
// WebContents on its CDP endpoint, so handing the raw BrowserContext to MCP
// would also expose Jiuwen's trusted UI. This adapter resolves one exact CDP
// TargetID and presents a guarded one-page context to MCP.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { createRequire } = require('node:module');
const { randomUUID } = require('node:crypto');

const BLOCKED_TOOLS = new Set([
  'browser_close',
  'browser_install',
  'browser_tabs',
]);

function resolveDiagnosticPath(env = process.env, homeDir = os.homedir()) {
  const configured = String(env.PLAYWRIGHT_MCP_DIAGNOSTIC_LOG || '').trim();
  return configured || path.join(homeDir, '.jiuwenswarm', 'agent', '.logs', 'target_mcp_wrapper.log');
}

function writeDiagnostic(event, details = {}) {
  try {
    const diagnosticPath = resolveDiagnosticPath();
    fs.mkdirSync(path.dirname(diagnosticPath), { recursive: true });
    fs.appendFileSync(
      diagnosticPath,
      `${JSON.stringify({
        timestamp: new Date().toISOString(),
        pid: process.pid,
        event,
        ...details,
      })}\n`,
      'utf8',
    );
  } catch {
    // Diagnostics must never interfere with the MCP stdio protocol.
  }
}

function filterServerMessage(message) {
  const tools = message?.result?.tools;
  if (!Array.isArray(tools)) return message;
  const visibleTools = tools.filter(tool => !BLOCKED_TOOLS.has(tool?.name));
  // 0.0.78 renamed this tool; keep the SDK's older name as an honest alias.
  const runCode = visibleTools.find(tool => tool.name === 'browser_run_code_unsafe');
  if (runCode && !visibleTools.some(tool => tool.name === 'browser_run_code')) {
    visibleTools.push({ ...runCode, name: 'browser_run_code' });
  }
  return {
    ...message,
    result: {
      ...message.result,
      tools: visibleTools,
    },
  };
}

function blockedToolResponse(message) {
  const toolName = message?.params?.name;
  if (message?.method !== 'tools/call' || !BLOCKED_TOOLS.has(toolName)) return null;
  return {
    jsonrpc: '2.0',
    id: message.id,
    result: {
      content: [
        {
          type: 'text',
          text: `${toolName} is disabled for the Electron-owned sideview`,
        },
      ],
      isError: true,
    },
  };
}

function findMcpEntryPoint() {
  const candidates = [];
  for (const binDir of String(process.env.PATH || '').split(path.delimiter)) {
    if (!binDir) continue;
    candidates.push(path.resolve(binDir, '..', '@playwright', 'mcp', 'index.js'));
  }
  try {
    candidates.unshift(require.resolve('@playwright/mcp'));
  } catch {
    // npx installs the package beside the temporary .bin directory rather
    // than in this script's ancestor tree, so PATH discovery is expected.
  }
  const entryPoint = candidates.find(candidate => fs.existsSync(candidate));
  if (!entryPoint) {
    throw new Error(
      'Unable to locate the pinned @playwright/mcp package: packaged builds install it beside this script (resources/app/node_modules); dev builds resolve it from the npx PATH',
    );
  }
  return entryPoint;
}

async function findTargetPage(browser, targetId) {
  const seenTargetIds = [];
  for (const context of browser.contexts()) {
    for (const page of context.pages()) {
      let cdpSession;
      try {
        cdpSession = await context.newCDPSession(page);
        const result = await cdpSession.send('Target.getTargetInfo');
        const candidateId = String(result?.targetInfo?.targetId || '');
        if (candidateId) seenTargetIds.push(candidateId);
        if (candidateId === targetId) return { context, page };
      } catch {
        // A target may disappear while Electron is navigating or recovering a
        // crashed renderer. Keep scanning the remaining targets.
      } finally {
        if (cdpSession) await cdpSession.detach().catch(() => {});
      }
    }
  }
  throw new Error(
    `Electron sideview target ${targetId} is unavailable; visible CDP targets: ${seenTargetIds.join(', ') || '(none)'}`,
  );
}

function guardedContext(realContext, realPage) {
  let contextProxy;
  let pageProxy;

  const bind = (target, value) => (typeof value === 'function' ? value.bind(target) : value);
  const noClose = async () => undefined;
  const withPageCdp = async action => {
    const cdp = await realContext.newCDPSession(realPage);
    try {
      return await action(cdp);
    } finally {
      await cdp.detach().catch(() => {});
    }
  };
  // Electron can expose all partitions as one Playwright default context.
  // Network commands on the exact page address its partition's cookie store.
  const cookies = urls => withPageCdp(async cdp => {
    const result = urls === undefined
      ? await cdp.send('Network.getAllCookies')
      : await cdp.send('Network.getCookies', { urls: Array.isArray(urls) ? urls : [urls] });
    return result.cookies;
  });
  const denyCdp = () => { throw new Error('Browser-wide CDP is unavailable in a target-bound panel'); };
  const wrapped = new WeakMap();
  // Scope ordinary locator/frame operations to this page. This is NOT a
  // JavaScript sandbox: upstream run_code_unsafe intentionally retains its RCE semantics.
  const wrap = value => {
    if (value === realPage) return pageProxy;
    if (value === realContext) return contextProxy;
    if (value instanceof Promise) return value.then(wrap);
    if (Array.isArray(value)) return value.map(wrap);
    if (!value || typeof value !== 'object') return value;
    if (wrapped.has(value)) return wrapped.get(value);
    if (Object.getPrototypeOf(value) === Object.prototype || Buffer.isBuffer(value)) return value;
    const proxy = new Proxy(value, {
      get(target, property) {
        const result = Reflect.get(target, property, target);
        return typeof result === 'function' ? (...args) => wrap(result.apply(target, args)) : wrap(result);
      },
    });
    wrapped.set(value, proxy);
    return proxy;
  };

  contextProxy = new Proxy(realContext, {
    get(target, property) {
      if (property === 'pages') return () => [pageProxy];
      if (property === 'newPage') return async () => pageProxy;
      if (property === 'close') return noClose;
      if (property === 'cookies') return cookies;
      if (property === 'addCookies') return values => withPageCdp(cdp => cdp.send('Network.setCookies', { cookies: values }));
      if (property === 'clearCookies') return (filters = {}) => withPageCdp(async cdp => {
        const result = await cdp.send('Network.getAllCookies');
        for (const cookie of result.cookies) {
          const matches = Object.entries(filters).every(([key, value]) =>
            Object.prototype.toString.call(value) === '[object RegExp]'
              ? new RegExp(value.source, value.flags).test(cookie[key]) : value === cookie[key]);
          if (matches) await cdp.send('Network.deleteCookies', {
            name: cookie.name, domain: cookie.domain, path: cookie.path,
            ...(cookie.partitionKey ? { partitionKey: cookie.partitionKey } : {}),
          });
        }
      });
      if (property === 'newCDPSession') return denyCdp;
      if (property === 'backgroundPages' || property === 'serviceWorkers') return () => [];
      if (['addInitScript', 'route', 'unroute', 'unrouteAll'].includes(property)) {
        return bind(realPage, realPage[property]);
      }
      if (property === 'browser') {
        return () => {
          const realBrowser = target.browser();
          if (!realBrowser) return null;
          return new Proxy(realBrowser, {
            get(browser, browserProperty) {
              if (browserProperty === 'contexts') return () => [contextProxy];
              if (browserProperty === 'newContext') return async () => contextProxy;
              if (browserProperty === 'close') return noClose;
              if (browserProperty === 'newPage') return async () => pageProxy;
              if (browserProperty === 'newBrowserCDPSession') return denyCdp;
              return bind(browser, Reflect.get(browser, browserProperty, browser));
            },
          });
        };
      }
      if (['on', 'once', 'addListener', 'prependListener', 'prependOnceListener'].includes(property)) {
        return (eventName, listener) => {
          // A page event can only represent a detached target. Never surface it.
          if (eventName === 'page') return contextProxy;
          target[property](eventName, listener);
          return contextProxy;
        };
      }
      return bind(target, Reflect.get(target, property, target));
    },
  });

  pageProxy = new Proxy(realPage, {
    get(target, property) {
      if (property === 'close') return noClose;
      if (property === 'context') return () => contextProxy;
      const value = Reflect.get(target, property, target);
      return typeof value === 'function' ? (...args) => wrap(value.apply(target, args)) : wrap(value);
    },
  });
  return contextProxy;
}

class JsonLineStdioTransport {
  constructor() {
    this.onclose = undefined;
    this.onerror = undefined;
    this.onmessage = undefined;
    this._readline = undefined;
  }

  async start() {
    if (this._readline) throw new Error('stdio transport already started');
    this._readline = readline.createInterface({ input: process.stdin, terminal: false });
    this._readline.on('line', line => {
      try {
        if (!line.trim()) return;
        const message = JSON.parse(line);
        if (message.method === 'tools/call' && message.params?.name === 'browser_run_code') {
          message.params.name = 'browser_run_code_unsafe';
        }
        const blockedResponse = blockedToolResponse(message);
        if (blockedResponse) {
          void this.send(blockedResponse).catch(error => this.onerror?.(error));
          return;
        }
        this.onmessage?.(message);
      } catch (error) {
        this.onerror?.(error);
      }
    });
    this._readline.on('close', () => this.onclose?.());
  }

  async send(message) {
    const filteredMessage = filterServerMessage(message);
    await new Promise((resolve, reject) => {
      process.stdout.write(`${JSON.stringify(filteredMessage)}\n`, error =>
        error ? reject(error) : resolve(),
      );
    });
  }

  async close() {
    this._readline?.close();
  }
}

async function acquireTarget(env = process.env) {
  const resolver = String(env.PLAYWRIGHT_MCP_TARGET_RESOLVER || '').trim();
  if (!resolver) {
    const targetId = String(env.PLAYWRIGHT_MCP_TARGET_ID || '').trim();
    if (!targetId) throw new Error('An Electron target resolver or exact TargetID is required');
    return { targetId, release: async () => {} };
  }
  const sid = String(env.PLAYWRIGHT_MCP_SESSION_ID || '').trim();
  if (!sid) throw new Error('PLAYWRIGHT_MCP_SESSION_ID is required with the Electron resolver');
  const url = new URL(`${resolver.replace(/\/$/, '')}/${encodeURIComponent(sid)}`);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1') {
    throw new Error('Electron resolver must use loopback HTTP');
  }
  url.search = new URLSearchParams({
    member: env.PLAYWRIGHT_MCP_MEMBER_ID || '',
    label: env.PLAYWRIGHT_MCP_PANEL_LABEL || '',
    lease: randomUUID(),
    pid: String(process.pid),
  }).toString();
  const headers = { Authorization: `Bearer ${env.PLAYWRIGHT_MCP_TARGET_RESOLVER_TOKEN || ''}` };
  const response = await fetch(url, { method: 'POST', headers, signal: AbortSignal.timeout(15_000) });
  if (!response.ok) throw new Error(`Electron target acquisition failed: HTTP ${response.status}`);
  const payload = await response.json();
  if (!payload.targetId) throw new Error('Electron resolver returned no TargetID');
  return {
    targetId: payload.targetId,
    release: async () => {
      await fetch(url, { method: 'DELETE', headers, signal: AbortSignal.timeout(3_000) }).catch(() => {});
    },
  };
}

async function main() {
  const endpoint = String(process.env.PLAYWRIGHT_MCP_CDP_ENDPOINT || '').trim();
  if (!endpoint) throw new Error('PLAYWRIGHT_MCP_CDP_ENDPOINT is required');
  const { targetId, release } = await acquireTarget();
  writeDiagnostic('startup', { endpoint, targetId });
  let connection;
  let closing;
  const shutdown = () => closing ||= (async () => {
    await connection?.close().catch(() => {});
    await release();
    // Never send Browser.close to the Electron-owned endpoint.
  })();
  process.once('SIGINT', () => void shutdown().finally(() => process.exit(130)));
  process.once('SIGTERM', () => void shutdown().finally(() => process.exit(143)));
  process.stdin.once('end', () => void shutdown().finally(() => process.exit(0)));
  try {
    const mcpEntryPoint = findMcpEntryPoint();
    writeDiagnostic('mcp-package-resolved', { mcpEntryPoint });
    const requireFromMcp = createRequire(mcpEntryPoint);
    const { createConnection } = requireFromMcp(mcpEntryPoint);
    const { chromium } = requireFromMcp('playwright');
    const browser = await chromium.connectOverCDP(endpoint, {
      timeout: Number(process.env.PLAYWRIGHT_MCP_CDP_TIMEOUT || 30_000),
    });
    browser.once('disconnected', () => void shutdown().finally(() => process.exit(1)));
    writeDiagnostic('cdp-connected');
    const { context, page } = await findTargetPage(browser, targetId);
    writeDiagnostic('target-resolved', { url: page.url() });
    const contextProxy = guardedContext(context, page);
    connection = await createConnection(
      {
        capabilities: ['core', 'core-navigation', 'core-input'],
        codegen: 'typescript',
        outputDir: process.cwd(),
      },
      async () => contextProxy,
    );
    const transport = new JsonLineStdioTransport();
    await connection.connect(transport);
    writeDiagnostic('mcp-ready');
  } catch (error) {
    await shutdown();
    throw error;
  }
}

if (require.main === module) {
  main().catch(error => {
    writeDiagnostic('fatal', {
      message: String(error?.message || error),
      stack: String(error?.stack || error),
    });
    process.stderr.write(`[playwright-target-mcp] ${error?.stack || error}\n`);
    process.exit(1);
  });
}

module.exports = {
  blockedToolResponse,
  filterServerMessage,
  findTargetPage,
  guardedContext,
  JsonLineStdioTransport,
  resolveDiagnosticPath,
  writeDiagnostic,
  acquireTarget,
};
