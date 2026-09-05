import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import { SwarmSnapshot } from './SwarmState';

/**
 * Manages the VS Code WebviewPanel that renders the Live Swarm Map.
 * Mirrors SwarmMapPanel.kt for JetBrains.
 *
 * The webview signals readiness via { type: "swarm_ready" }; snapshots
 * posted before that are buffered and flushed on ready.
 */
export class SwarmMapPanel {
  private panel?: vscode.WebviewPanel;
  private pendingSnapshot?: SwarmSnapshot;
  private pendingDebugLines: string[] = [];
  private webviewReady = false;

  /** Called by ChatPanel to handle open_lane and future steerable messages. */
  onMessage?: (msg: Record<string, unknown>) => void;

  constructor(private readonly context: vscode.ExtensionContext) {}

  /** Open the panel (or bring it to front if already open). */
  show(): void {
    if (this.panel) {
      this.panel.reveal(vscode.ViewColumn.Beside, true);
      return;
    }

    this.webviewReady = false;
    this.panel = vscode.window.createWebviewPanel(
      'jiuwenswarm.swarmMap',
      'Swarm Map',
      { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(this.context.extensionUri, 'resources')],
      },
    );
    this.panel.iconPath = vscode.Uri.joinPath(this.context.extensionUri, 'resources', 'icon.svg');
    this.panel.webview.html = this.getHtml(this.panel.webview);

    this.panel.webview.onDidReceiveMessage((msg: Record<string, unknown>) => {
      if (msg['type'] === 'swarm_ready') {
        this.webviewReady = true;
        if (this.pendingSnapshot) {
          void this.panel?.webview.postMessage({ type: 'swarm_snapshot', snapshot: this.pendingSnapshot });
          this.pendingSnapshot = undefined;
        }
        const pendingLines = this.pendingDebugLines;
        this.pendingDebugLines = [];
        for (const line of pendingLines) {
          void this.panel?.webview.postMessage({ type: 'swarm_debug', line });
        }
        return;
      }
      // Delegate all other messages (open_lane, etc.) to ChatPanel
      this.onMessage?.(msg);
    });

    this.panel.onDidDispose(() => {
      this.panel = undefined;
      this.webviewReady = false;
    });
  }

  /** Thread-safe: buffers snapshot if the panel is not yet ready. */
  postSnapshot(snapshot: SwarmSnapshot): void {
    if (!this.panel) return;
    if (!this.webviewReady) {
      this.pendingSnapshot = snapshot;
      return;
    }
    void this.panel.webview.postMessage({ type: 'swarm_snapshot', snapshot });
  }

  /** Send a debug-log line to the webview; buffered until the webview is ready. */
  postDebug(line: string): void {
    if (!this.panel || !this.webviewReady) {
      this.pendingDebugLines.push(line);
      if (this.pendingDebugLines.length > 500) this.pendingDebugLines.shift();
      return;
    }
    void this.panel.webview.postMessage({ type: 'swarm_debug', line });
  }

  dispose(): void {
    this.panel?.dispose();
    this.panel = undefined;
  }

  private getHtml(webview: vscode.Webview): string {
    const htmlPath = path.join(this.context.extensionPath, 'resources', 'swarm_map.html');
    try {
      let html = fs.readFileSync(htmlPath, 'utf-8');
      html = html.replace(
        /<meta http-equiv="Content-Security-Policy"[^>]*>/,
        `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline' ${webview.cspSource}; script-src 'unsafe-inline' ${webview.cspSource}; img-src data: blob: ${webview.cspSource};">`,
      );
      return html;
    } catch {
      return this.getFallbackHtml();
    }
  }

  private getFallbackHtml(): string {
    return [
      '<!DOCTYPE html><html><head><meta charset="UTF-8">',
      '<style>body{background:#1e1e1e;color:#ccc;font-family:sans-serif;padding:12px}</style>',
      '</head><body><b>Swarm Map</b><br><br>swarm_map.html not found in extension resources.</body></html>',
    ].join('');
  }
}
