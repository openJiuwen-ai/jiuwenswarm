/**
 * /switch 公共契约类型与常量。
 *
 * 这些类型是 JiuwenSwarm TUI 与 agentos-tui launcher 之间的稳定公共边界，
 * 不得通过私有约定改变。
 */

/** launcher 当前唯一支持的 handoff 目标。 */
export const HANDOFF_TARGET_CC_TUI = "cc-tui" as const;
export type HandoffTarget = typeof HANDOFF_TARGET_CC_TUI;

/** HandoffPort 预检失败原因。 */
export type HandoffErrorCode =
  | "NOT_SUPERVISED"
  | "INVALID_EXIT_CODE"
  | "TARGET_UNAVAILABLE"
  | "HANDOFF_IN_PROGRESS";

export interface HandoffCheckResult {
  ok: boolean;
  code?: HandoffErrorCode;
  /** 已脱敏的可展示消息，不含路径细节、token 或完整 argv。 */
  message?: string;
}

/** 等待型取消失败原因。 */
export type CancelErrorCode =
  | "CANCEL_REJECTED"
  | "CANCEL_TIMEOUT"
  | "CANCEL_CONNECTION_LOST"
  | "CANCEL_STATE_STOPPED"
  | "CANCEL_ALREADY_PENDING";

export class CancelError extends Error {
  constructor(
    public readonly code: CancelErrorCode,
    message?: string,
  ) {
    super(message ?? code);
    this.name = "CancelError";
  }
}

export interface CancelAndWaitOptions {
  /** 是否显示普通的“任务已中断”提示；不影响 Promise 完成语义。 */
  showNotice?: boolean;
  /** 等待服务端权威停止的超时时间，默认 5000ms。 */
  timeoutMs?: number;
}

/** 统一任务快照与等待型取消端口。 */
export interface TaskLifecyclePort {
  /** 读取统一、同步的任务快照；存在任一活动工作时返回 true。 */
  hasServerTask(): boolean;
  /** 同步发送取消请求，立即返回；Ctrl+C、Esc 和普通取消路径共用。 */
  cancel(options?: { showNotice?: boolean }): boolean;
  /**
   * 等待型取消；只有“确认停止后才能继续”的生命周期动作（/switch）使用。
   * 默认 timeoutMs=5000。没有可取消任务时立即成功且不发送 interrupt。
   * 同一时刻只允许一个等待型取消。
   */
  cancelAndWaitForIdle(options?: CancelAndWaitOptions): Promise<void>;
}

/** Handoff 预检与请求端口。 */
export interface HandoffPort {
  /** 无副作用预检：读取并校验托管标记、动作退出码和目标能力。 */
  checkHandoff(target: HandoffTarget): HandoffCheckResult;
  /**
   * 请求 handoff：二次校验后调用统一顶层关闭路径，以 launcher 注入的动作退出码退出。
   * switchContent 是原始命令文本（如 "switch claude"），会在退出前以 handoff JSON
   * 输出到 stdout，供 launcher 读取并解析后发起 3rdagent.switch RPC。
   * 成功路径不会返回（process.exit）。
   */
  requestHandoff(target: HandoffTarget, switchContent: string): Promise<void>;
}

/** 重新认证触发原因。 */
export type ReauthenticationReason = "access-token-expired";

/**
 * 重新认证端口；不由 /switch 调用，而由连接认证适配层在权威认证过期时调用。
 * 不发送 interrupt、不取消任务；以 launcher 注入的重新认证动作退出码退出。
 */
export interface ReauthenticationPort {
  requestReauthentication(reason: ReauthenticationReason): Promise<void>;
}

/** 顶层关闭原因；决定退出通知的 reason 字段与最终退出码。 */
export type UiExitReason =
  | "normal"
  | "switch"
  | "reauth-required"
  | "signal"
  | "fatal";

/**
 * 统一顶层关闭路径：串行完成退出通知 → screen.dispose → appState.stop →
 * wsClient.disconnect → tui.stop → stdout handoff JSON（如有）→ process.exit。
 * 任一时刻只允许一次关闭调用；重复调用直接返回已存在的 Promise。
 */
export interface UiLifecyclePort {
  closeUi(options: {
    reason: UiExitReason;
    exitCode: number;
    /** 退出前输出到 stdout 的 handoff JSON，供 launcher 读取。仅 switch reason 使用。 */
    handoffMessage?: string;
  }): Promise<void>;
}
