/**
 * 统一顶层关闭路径。
 *
 * 串行完成：退出通知 → screen.dispose → appState.stop → wsClient.disconnect →
 * tui.stop → process.exit。任一时刻只允许一次关闭调用。
 */
import type { UiExitReason, UiLifecyclePort } from "./protocol.js";

export interface UiLifecycleDeps {
  /** 退出前显式通知服务端；携带 reason 字段。best-effort，失败不阻塞退出。 */
  notifyDisconnect: (reason: UiExitReason) => Promise<void>;
  /** 释放 screen：关闭 alternate screen / mouse tracking 等。 */
  disposeScreen: () => void;
  /** 停止 AppState：清理 timer、关闭 WebSocket。 */
  stopAppState: () => void;
  /** 释放 TUI：恢复 raw mode。 */
  stopTui: () => void;
}

export class UiLifecyclePortImpl implements UiLifecyclePort {
  private closed = false;
  private closingPromise: Promise<void> | null = null;

  constructor(private readonly deps: UiLifecycleDeps) {}

  async closeUi(options: {
    reason: UiExitReason;
    exitCode: number;
    handoffMessage?: string;
  }): Promise<void> {
    // 并发保护：正在关闭时第二次调用直接返回已存在的 Promise。
    if (this.closed) {
      return this.closingPromise ?? Promise.resolve();
    }
    this.closed = true;

    if (this.closingPromise) {
      return this.closingPromise;
    }

    this.closingPromise = (async () => {
      // 1. 退出通知（携带 reason，由 AppState 转换为 tui.disconnect 参数）
      try {
        await this.deps.notifyDisconnect(options.reason);
      } catch {
        // best effort only; 进程即将退出
      }
      // 2. screen.dispose — 释放 alternate screen / mouse tracking
      try {
        this.deps.disposeScreen();
      } catch {
        // ignore — 进程即将退出
      }
      // 3. appState.stop — 清理 timer、关闭 WebSocket
      try {
        this.deps.stopAppState();
      } catch {
        // ignore — 进程即将退出
      }
      // 4. tui.stop — 释放 raw mode
      try {
        this.deps.stopTui();
      } catch {
        // ignore — 进程即将退出
      }
      // 5. stdout handoff JSON — 供 launcher 读取（仅 switch reason）
      //    必须在 screen.dispose 之后输出，避免污染 alternate screen；
      //    必须在 process.exit 之前输出，确保 launcher 能从 stdout 管道读取。
      if (options.handoffMessage) {
        try {
          process.stdout.write(options.handoffMessage + "\n");
        } catch {
          // ignore — 进程即将退出
        }
      }
      // 6. process.exit — 使用 launcher 注入的动作退出码
      process.exit(options.exitCode);
    })();
    return this.closingPromise;
  }

  /** 用于测试：是否已触发关闭。 */
  isClosed(): boolean {
    return this.closed;
  }
}
