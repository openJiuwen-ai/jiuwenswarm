import * as vscode from 'vscode';
import { WsClient, WsStatus } from '../client/WsClient';
import { SessionMetrics } from '../client/protocol';

export class StatusBar {
  private readonly item: vscode.StatusBarItem;
  private tokenCount = 0;
  private promptEstimatedTokens = 0;
  private costUsd = 0;
  private hasCost = false;
  private sessionId: string | null = null;

  constructor(private readonly ws: WsClient) {
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    this.item.show();
    this.ws.on('status', (s) => this.update(s));
    this.update(ws.getStatus());
  }

  setTokenCount(n: number): void {
    this.tokenCount = n;
    this.update(this.ws.getStatus());
  }

  setMetrics(metrics: SessionMetrics): void {
    this.tokenCount = metrics.sessionTokens;
    this.promptEstimatedTokens = metrics.promptEstimatedTokens;
    this.costUsd = metrics.sessionCostUsd;
    this.hasCost = metrics.hasCost;
    this.update(this.ws.getStatus());
  }

  setSessionId(sid: string | null): void {
    this.sessionId = sid;
    this.update(this.ws.getStatus());
  }

  private update(s: WsStatus): void {
    const tokenLabel = this.tokenCount > 0
      ? ` \u00b7 ${formatTokenCount(this.tokenCount)}`
      : '';
    const contextLabel = this.promptEstimatedTokens > 0
      ? ` \u00b7 ctx ${formatTokenCount(this.promptEstimatedTokens)}`
      : '';
    const costLabel = this.hasCost
      ? ` \u00b7 $${this.costUsd.toFixed(3)}`
      : '';
    const metricsTooltip = [
      this.tokenCount > 0 ? `${this.tokenCount} session tokens` : undefined,
      this.promptEstimatedTokens > 0 ? `${this.promptEstimatedTokens} estimated prompt context tokens` : undefined,
      this.hasCost ? `$${this.costUsd.toFixed(3)} session cost` : undefined,
    ].filter(Boolean).join(' — ');

    switch (s) {
      case 'connected':
        this.item.text = `$(check) JiuwenSwarm${tokenLabel}${contextLabel}${costLabel}`;
        this.item.tooltip = `JiuwenSwarm: Connected${this.sessionId ? ` — session ${this.sessionId.slice(0, 8)}` : ''}${metricsTooltip ? ` — ${metricsTooltip}` : ''}`;
        this.item.backgroundColor = undefined;
        this.item.command = 'jiuwenswarm.openChat';
        break;
      case 'connecting':
        this.item.text = `$(loading~spin) JiuwenSwarm`;
        this.item.tooltip = 'JiuwenSwarm: Connecting…';
        this.item.backgroundColor = undefined;
        this.item.command = undefined;
        break;
      case 'reconnecting':
        this.item.text = `$(sync~spin) JiuwenSwarm${tokenLabel}${contextLabel}${costLabel}`;
        this.item.tooltip = `JiuwenSwarm: Reconnecting…${metricsTooltip ? ` — ${metricsTooltip}` : ''}`;
        this.item.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
        this.item.command = undefined;
        break;
      case 'disconnected':
        this.item.text = `$(circle-slash) JiuwenSwarm`;
        this.item.tooltip = 'JiuwenSwarm: Disconnected — click to reconnect';
        this.item.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
        this.item.command = 'jiuwenswarm.reconnect';
        break;
    }
  }

  dispose(): void {
    this.item.dispose();
  }
}

function formatTokenCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toString();
}
