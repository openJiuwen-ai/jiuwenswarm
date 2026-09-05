import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { WsClient } from './client/WsClient';
import { SessionManager } from './client/SessionManager';
import { ChatPanel } from './ui/ChatPanel';
import { StatusBar } from './ui/StatusBar';
import {
  FixWithAiCodeActionProvider,
  buildDiagnosticPrefill,
} from './codeActions/FixWithAiCodeActionProvider';
import { disposeTerminal } from './terminal/TerminalManager';
import { registerDiffProvider } from './editor/DiffViewer';

let ws: WsClient | undefined;
let session: SessionManager | undefined;
let statusBar: StatusBar | undefined;
let chatPanel: ChatPanel | undefined;

export function activate(context: vscode.ExtensionContext): void {
  const cfg = vscode.workspace.getConfiguration('jiuwenswarm');
  const host = cfg.get<string>('host', '127.0.0.1');
  const port = cfg.get<number>('port', 19000);
  const channelId = cfg.get<string>('channelId', 'ide');
  const autoConnect = cfg.get<boolean>('autoConnect', true);
  const keepAliveEnabled = cfg.get<boolean>('keepAlive.enabled', true);
  const keepAliveInterval = cfg.get<number>('keepAlive.interval', 30);
  const url = `ws://${host}:${port}/ws`;

  // Copy shared webview HTML into extension resources
  ensureWebviewHtml(context);

  // Create core singletons
  const pingIntervalMs = keepAliveEnabled ? keepAliveInterval * 1000 : 0;
  ws = new WsClient(url, pingIntervalMs);
  session = new SessionManager(ws, channelId);
  statusBar = new StatusBar(ws);
  chatPanel = new ChatPanel(context, ws, session, statusBar);

  // Auto-connect on startup
  if (autoConnect) {
    ws.connect();
  }

  // If the server isn't ready at connection time it skips connection.ack.
  // Schedule a one-time reconnect after 15s so the IDE retries once the agent
  // prewarm finishes (typically ~8s after startup).
  let ackRetryPending = false;
  ws.on('status', (s) => {
    if (s === 'connected') {
      if (!ackRetryPending) {
        ackRetryPending = true;
        setTimeout(() => {
          ackRetryPending = false;
          if (ws?.isConnected() && !session?.sessionId) {
            ws.reconnect();
          }
        }, 15_000);
      }
    } else if (s === 'disconnected' || s === 'reconnecting') {
      ackRetryPending = false;
    }
  });

  // Register code-action lightbulb before any other interactions
  registerDiffProvider(context);
  context.subscriptions.push(
    vscode.languages.registerCodeActionsProvider(
      { scheme: 'file' },
      new FixWithAiCodeActionProvider(),
      {
        providedCodeActionKinds: FixWithAiCodeActionProvider.providedCodeActionKinds,
      },
    ),
  );

  // ── Commands ──

  context.subscriptions.push(
    vscode.commands.registerCommand('jiuwenswarm.openChat', () => {
      chatPanel?.show();
    }),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('jiuwenswarm.newSession', () => {
      if (!ws?.isConnected()) {
        vscode.window.showWarningMessage('JiuwenSwarm: Not connected');
        return;
      }
      chatPanel?.show();
      chatPanel?.resetMetrics();
      ws.reconnect();
    }),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('jiuwenswarm.sendSelection', () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor || editor.selection.isEmpty) {
        vscode.window.showInformationMessage('JiuwenSwarm: No selection');
        return;
      }
      const selection = editor.document.getText(editor.selection);
      const fileName = path.basename(editor.document.fileName);
      const prefill = `[File: ${fileName}]\n\`\`\`\n${selection}\n\`\`\`\n\n`;

      chatPanel?.show();
      chatPanel?.postToWebview({ type: 'prefill', content: prefill });
    }),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('jiuwenswarm.reconnect', () => {
      ws?.reconnect();
    }),
  );

  // Fix with JiuwenSwarm — triggered from Code Action lightbulb
  context.subscriptions.push(
    vscode.commands.registerCommand(
      'jiuwenswarm.fixDiagnostic',
      (document: vscode.TextDocument, diagnostics: vscode.Diagnostic[]) => {
        const prefill = buildDiagnosticPrefill(document, diagnostics);
        chatPanel?.show();
        chatPanel?.postToWebview({ type: 'prefill', content: prefill });
      },
    ),
  );

  context.subscriptions.push(
    vscode.window.onDidChangeActiveTextEditor(() => chatPanel?.refreshMetrics()),
    vscode.window.onDidChangeTextEditorSelection(() => chatPanel?.refreshMetrics()),
    vscode.window.tabGroups.onDidChangeTabs(() => chatPanel?.refreshMetrics()),
    vscode.languages.onDidChangeDiagnostics(() => chatPanel?.refreshMetrics()),
  );

  // Re-read config if changed
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration('jiuwenswarm')) {
        vscode.window.showInformationMessage(
          'JiuwenSwarm: Settings changed — reload window to apply.',
          'Reload',
        ).then((action) => {
          if (action === 'Reload') {
            vscode.commands.executeCommand('workbench.action.reloadWindow');
          }
        });
      }
    }),
  );

  context.subscriptions.push({ dispose: () => cleanup() });
}

export function deactivate(): void {
  cleanup();
}

function cleanup(): void {
  ws?.disconnect();
  chatPanel?.dispose();
  statusBar?.dispose();
  disposeTerminal();
}

/**
 * Copy the shared chat.html from the monorepo into extension resources
 * so that it can be served from the extension's localResourceRoots.
 * In production builds, this file is packaged alongside the extension.
 */
function ensureWebviewHtml(context: vscode.ExtensionContext): void {
  const resourceDir = path.join(context.extensionPath, 'resources');
  const destPath = path.join(resourceDir, 'chat.html');

  if (fs.existsSync(destPath)) return; // already there

  // Try to find the shared webview relative to this extension
  const candidates = [
    // Development (monorepo): extension is packages/vscode-extension
    path.join(context.extensionPath, '..', '..', 'packages', 'shared-webview', 'chat.html'),
    path.join(context.extensionPath, '..', 'shared-webview', 'chat.html'),
    // Check jetbrains-plugin resources as canonical source
    path.join(context.extensionPath, '..', 'jetbrains-plugin', 'src', 'main', 'resources', 'webview', 'chat.html'),
  ];

  for (const src of candidates) {
    if (fs.existsSync(src)) {
      try {
        fs.mkdirSync(resourceDir, { recursive: true });
        fs.copyFileSync(src, destPath);
        return;
      } catch {
        // ignore
      }
    }
  }

  // If not found during development, generate a minimal placeholder
  if (!fs.existsSync(resourceDir)) fs.mkdirSync(resourceDir, { recursive: true });
  if (!fs.existsSync(destPath)) {
    fs.writeFileSync(destPath, '<html><body style="color:#d4d4d4;background:#1e1e1e;font-family:sans-serif;padding:16px">Loading JiuwenSwarm…</body></html>');
  }
}
