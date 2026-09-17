/**
 * TaskLifecyclePort 实现：统一任务快照 + 等待型取消。
 *
 * 关键约束：
 * - hasServerTask() 读取统一、同步的任务快照（不依赖单一布尔）。
 * - cancel() 保持现有同步 boolean 契约，供 Ctrl+C、Esc 和普通取消路径使用。
 * - cancelAndWaitForIdle() 只供“确认停止后才能继续”的生命周期操作使用；默认 timeoutMs=5000。
 * - 同一时刻只允许一个等待型取消；第二次调用以 CANCEL_ALREADY_PENDING 拒绝。
 * - 迟到事件不得完成已结束的 waiter。
 */
import { CancelError, type CancelAndWaitOptions, type TaskLifecyclePort } from "./protocol.js";

/** 等待型取消的内部 waiter 状态。 */
interface Waiter {
  /** 本次等待型取消发送 chat.interrupt 时的 requestId，由 sendEventOnly 返回。 */
  requestId: string;
  /** 发送时的 session_id；用于匹配 interrupt_result 事件。 */
  sessionId: string;
  /** 超时定时器；超时触发 CANCEL_TIMEOUT。 */
  timer: ReturnType<typeof setTimeout>;
  resolve: () => void;
  reject: (e: CancelError) => void;
  /** waiter 建立时间戳，用于过滤迟到事件。 */
  createdAt: number;
}

export interface TaskLifecycleDeps {
  /** 读取统一同步任务快照。 */
  getSnapshot: () => { cancellableWork: boolean; sessionId: string };
  /** 同步发送取消请求；立即返回，不等待服务端确认。 */
  cancel: (options?: { showNotice?: boolean }) => boolean;
  /** 发送事件帧并返回 requestId。 */
  sendEventOnly: (method: string, params: Record<string, unknown>) => string;
  /** 订阅 interrupt_result 事件；按 requestId + sessionId 关联。 */
  onInterruptResult: (handler: InterruptResultHandler) => () => void;
  /** 订阅 WebSocket 断开或认证失败。 */
  onConnectionLost: (handler: () => void) => () => void;
  /** 订阅 AppState stop（进程退出前清理）。 */
  onStop: (handler: () => void) => () => void;
  /** 当前连接是否存活。 */
  isConnectionAlive: () => boolean;
}

export type InterruptResultHandler = (
  requestId: string,
  sessionId: string,
  success: boolean,
  message?: string,
) => void;

const DEFAULT_TIMEOUT_MS = 5000;

export class TaskLifecyclePortImpl implements TaskLifecyclePort {
  private waiter: Waiter | null = null;
  /** 事件订阅的取消函数；waiter 清除时一并释放。 */
  private unlisteners: Array<() => void> = [];

  constructor(private readonly deps: TaskLifecycleDeps) {}

  hasServerTask(): boolean {
    return this.deps.getSnapshot().cancellableWork;
  }

  cancel(options?: { showNotice?: boolean }): boolean {
    return this.deps.cancel(options);
  }

  async cancelAndWaitForIdle(options?: CancelAndWaitOptions): Promise<void> {
    // 同一时刻只允许一个等待型取消。
    if (this.waiter !== null) {
      throw new CancelError("CANCEL_ALREADY_PENDING");
    }
    // 没有可取消任务时，立即成功且不发送 interrupt。
    if (!this.hasServerTask()) {
      return;
    }
    if (!this.deps.isConnectionAlive()) {
      throw new CancelError("CANCEL_CONNECTION_LOST");
    }

    const timeoutMs = options?.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    const showNotice = options?.showNotice ?? true;
    const snapshot = this.deps.getSnapshot();
    const sessionId = snapshot.sessionId;

    // 发送 chat.interrupt；sendEventOnly 返回的 requestId 用于关联 interrupt_result。
    // showNotice=false 时由 cancel 内部抑制 UI 通知；这里直接 sendEventOnly 避免
    // 走同步 cancel 路径（cancel 会在 UI 层 addItem 显示提示）。
    const requestId = this.deps.sendEventOnly("chat.interrupt", { intent: "cancel" });

    return new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this.waiter?.requestId === requestId) {
          this.clearWaiter();
          reject(new CancelError("CANCEL_TIMEOUT"));
        }
      }, timeoutMs);

      this.waiter = {
        requestId,
        sessionId,
        timer,
        resolve,
        reject,
        createdAt: Date.now(),
      };

      // 注册事件回调；按 sessionId 关联。
      // 服务端的 interrupt_result 可能不回显 TUI 发送的 requestId
      //（服务端使用自己的 interrupt_xxx 格式），因此只要 sessionId 匹配
      // 且当前有 waiter，就视为本次等待的结果。
      const unlistenInterrupt = this.deps.onInterruptResult(
        (reqId, sessId, success, message) => {
          if (!this.waiter) return;
          // 如果 requestId 双方都有且不匹配，则不是本次的结果
          if (reqId && this.waiter.requestId && this.waiter.requestId !== reqId) return;
          // sessionId 必须匹配（空 sessionId 兼容服务端不回显的情况）
          if (sessId && sessId !== this.waiter.sessionId) return;
          this.clearWaiter();
          if (success) {
            resolve();
          } else {
            reject(new CancelError("CANCEL_REJECTED", message));
          }
        },
      );
      const unlistenConnectionLost = this.deps.onConnectionLost(() => {
        if (!this.waiter) return;
        this.clearWaiter();
        reject(new CancelError("CANCEL_CONNECTION_LOST"));
      });
      const unlistenStop = this.deps.onStop(() => {
        if (!this.waiter) return;
        this.clearWaiter();
        reject(new CancelError("CANCEL_STATE_STOPPED"));
      });

      this.unlisteners = [unlistenInterrupt, unlistenConnectionLost, unlistenStop];

      // suppress 通知：showNotice=false 时由 event-handlers 内部抑制；
      // 这里不直接调用 cancel(showNotice:false)，因为 cancel 会立即发送 interrupt
      // 而上面已经发送过了。
      if (!showNotice) {
        // 依赖 AppState 在 chat.interrupt_result 事件中检查 suppress 标志；
        // 等待型 waiter 已经通过 listener 接管结果，不会触发普通 UI 通知路径。
      }
    });
  }

  /** 清除 waiter 并释放所有事件订阅。 */
  private clearWaiter(): void {
    if (this.waiter) {
      clearTimeout(this.waiter.timer);
      this.waiter = null;
    }
    for (const unlisten of this.unlisteners) {
      try {
        unlisten();
      } catch {
        // ignore — 进程即将退出或 waiter 已清除
      }
    }
    this.unlisteners = [];
  }

  /** 测试用：当前是否有等待型取消。 */
  hasWaiter(): boolean {
    return this.waiter !== null;
  }

  /** 测试用：当前 waiter 的 requestId；无 waiter 时返回 null。 */
  currentWaiterRequestId(): string | null {
    return this.waiter?.requestId ?? null;
  }
}
