/**
 * HandoffPort 实现：/switch 预检 + 请求退出。
 *
 * 关键约束：
 * - checkHandoff 是无副作用预检；不发送 interrupt、不改变任务状态、不清理 UI、不退出进程。
 * - requestHandoff 在真正清理前再次执行相同校验。
 * - 调用统一的 UiLifecyclePort.closeUi，而不是在命令中直接 process.exit()。
 * - 拒绝并发 handoff，也拒绝在重新认证生命周期动作已经开始后执行。
 * - 不 import、解析或启动 cc-tui。
 * - 任一必需清理步骤失败时，继续尽力恢复终端并以非动作错误码（70）退出。
 */
import type {
  HandoffCheckResult,
  HandoffPort,
  HandoffTarget,
  UiLifecyclePort,
} from "./protocol.js";
import { HANDOFF_TARGET_CC_TUI } from "./protocol.js";
import type { SupervisionEnv } from "./supervised-env.js";

/** launcher 自身退出码：未分类内部错误。 */
const LAUNCHER_EXIT_INTERNAL_ERROR = 70;

export class HandoffPortImpl implements HandoffPort {
  /** handoff 生命周期动作已开始；拒绝第二次调用，避免并发。 */
  private handoffInProgress = false;
  /** 重新认证生命周期动作已开始；与 handoff 互斥。 */
  private reauthInProgress = false;

  constructor(
    private readonly env: SupervisionEnv,
    private readonly lifecycle: UiLifecyclePort,
  ) {}

  checkHandoff(target: HandoffTarget): HandoffCheckResult {
    if (!this.env.supervised) {
      return {
        ok: false,
        code: "NOT_SUPERVISED",
        message: "Running outside agentos-tui launcher; start via `agentos-tui` to use /switch",
      };
    }
    if (this.env.switchCcExitCode === null) {
      return {
        ok: false,
        code: "INVALID_EXIT_CODE",
        message: "Switch exit code not provided by launcher",
      };
    }
    if (target === HANDOFF_TARGET_CC_TUI && !this.env.ccTuiExecutable) {
      return {
        ok: false,
        code: "TARGET_UNAVAILABLE",
        message: "cc-tui executable not available",
      };
    }
    if (this.handoffInProgress || this.reauthInProgress) {
      return {
        ok: false,
        code: "HANDOFF_IN_PROGRESS",
        message: "Another handoff or reauthentication already in progress",
      };
    }
    return { ok: true };
  }

  async requestHandoff(target: HandoffTarget, switchContent: string): Promise<void> {
    // requestHandoff 在询问/取消任务之后才被调用；必须二次校验。
    const check = this.checkHandoff(target);
    if (!check.ok) {
      throw new Error(check.message ?? check.code);
    }

    this.handoffInProgress = true;
    try {
      // checkHandoff 已校验 switchCcExitCode 非 null；显式断言以通过类型检查
      const exitCode = this.env.switchCcExitCode;
      if (exitCode === null) {
        throw new Error("Switch exit code unexpectedly null after checkHandoff");
      }
      // 构造 handoff JSON，供 launcher 从 stdout 读取后发起 3rdagent.switch RPC。
      // content 是完整命令文本（如 "switch claude"），parsed 是目标名（如 "claude"）。
      const parsed = switchContent.replace(/^switch\s+/i, "").trim();
      const handoffMessage = JSON.stringify({
        action: "switch",
        content: switchContent,
        parsed,
      });
      await this.lifecycle.closeUi({
        reason: "switch",
        exitCode,
        handoffMessage,
      });
    } catch (err) {
      // 清理失败：尽力恢复终端，以非动作错误码退出；不得使用切换动作码。
      this.handoffInProgress = false;
      await this.lifecycle.closeUi({
        reason: "fatal",
        exitCode: LAUNCHER_EXIT_INTERNAL_ERROR,
      });
      throw err;
    }
    // closeUi 成功路径不返回（process.exit）
  }

  /** 供 ReauthenticationPort 共享的并发互斥标记。 */
  markReauthInProgress(): void {
    this.reauthInProgress = true;
  }

  /** 测试用：handoff 是否已开始。 */
  isHandoffInProgress(): boolean {
    return this.handoffInProgress;
  }
}
