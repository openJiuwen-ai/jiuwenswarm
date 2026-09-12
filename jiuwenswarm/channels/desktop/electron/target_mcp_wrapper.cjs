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

const BLOCKED_TOOLS = new Set([
  'browser_close',
  'browser_install',
  'browser_run_code',
  'browser_run_code_unsafe',
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
  return {
    ...message,
    result: {
      ...message.result,
      tools: tools.filter(tool => !BLOCKED_TOOLS.has(tool?.name)),
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

  contextProxy = new Proxy(realContext, {
    get(target, property) {
      if (property === 'pages') return () => [pageProxy];
      if (property === 'newPage') return async () => pageProxy;
      if (property === 'close') return noClose;
      if (property === 'browser') {
        return () => {
          const realBrowser = target.browser();
          if (!realBrowser) return null;
          return new Proxy(realBrowser, {
            get(browser, browserProperty) {
              if (browserProperty === 'contexts') return () => [contextProxy];
              if (browserProperty === 'newContext') return async () => contextProxy;
              if (browserProperty === 'close') return noClose;
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
      return bind(target, Reflect.get(target, property, target));
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

async function main() {
  const endpoint = String(process.env.PLAYWRIGHT_MCP_CDP_ENDPOINT || '').trim();
  const targetId = String(process.env.PLAYWRIGHT_MCP_TARGET_ID || '').trim();
  writeDiagnostic('startup', { endpoint, targetId });
  if (!endpoint || !targetId) {
    throw new Error('PLAYWRIGHT_MCP_CDP_ENDPOINT and PLAYWRIGHT_MCP_TARGET_ID are required');
  }
  const mcpEntryPoint = findMcpEntryPoint();
  writeDiagnostic('mcp-package-resolved', { mcpEntryPoint });
  const requireFromMcp = createRequire(mcpEntryPoint);
  const { createConnection } = requireFromMcp(mcpEntryPoint);
  const { chromium } = requireFromMcp('playwright');
  const browser = await chromium.connectOverCDP(endpoint, {
    timeout: Number(process.env.PLAYWRIGHT_MCP_CDP_TIMEOUT || 30_000),
  });
  writeDiagnostic('cdp-connected');
  const { context, page } = await findTargetPage(browser, targetId);
  writeDiagnostic('target-resolved', { url: page.url() });
  const contextProxy = guardedContext(context, page);
  const connection = await createConnection(
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

  const shutdown = async () => {
    await connection.close().catch(() => {});
    // Do not call Browser.close() for an Electron-owned endpoint. Exiting this
    // wrapper drops its CDP socket without sending a browser-close command.
  };
  process.once('SIGINT', () => void shutdown().finally(() => process.exit(130)));
  process.once('SIGTERM', () => void shutdown().finally(() => process.exit(143)));
  process.stdin.once('end', () => void shutdown().finally(() => process.exit(0)));
}

if (require.main === module) {
  main().catch(error => {
    writeDiagnostic('fatal', {
      message: String(error?.message || error),
      stack: String(error?.stack || error),
    });
    process.stderr.write(`[playwright-target-mcp] ${error?.stack || error}\n`);
    process.exitCode = 1;
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
};
