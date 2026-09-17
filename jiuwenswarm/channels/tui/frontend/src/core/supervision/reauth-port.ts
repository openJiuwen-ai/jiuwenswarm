/**
 * ReauthenticationPort 实现：权威认证过期 → 以重新认证动作退出码退出。
 *
 * 关键约束：
 * - 不由 /switch 调用；只由连接认证适配层在权威认证过期时调用。
 * - 校验托管标记和重新认证动作码；缺少或非法时拒绝执行，不自行使用固定 89。
 * - 拒绝与 handoff 或另一重新认证请求并发。
 * - 不发送 interrupt，不取消、删除或本地标记完成当前服务端任务。
 * - 调用统一顶层关闭路径；清理失败时以非动作错误码退出。
 */
import type { ReauthenticationPort, ReauthenticationReason, UiLifecyclePort } from "./protocol.js";
import type { SupervisionEnv } from "./supervised-env.js";
import type { HandoffPortImpl } from "./handoff-port.js";

/** launcher 自身退出码：未分类内部错误。 */
const LAUNCHER_EXIT_INTERNAL_ERROR = 70;

export class ReauthenticationPortImpl implements ReauthenticationPort {
  /** 重新认证生命周期动作已开始；拒绝并发调用。 */
  private reauthInProgress = false;

  constructor(
    private readonly env: SupervisionEnv,
    private readonly handoff: HandoffPortImpl,
    private readonly lifecycle: UiLifecyclePort,
  ) {}

  async requestReauthentication(_reason: ReauthenticationReason): Promise<void> {
    // 校验托管标记和重新认证动作码；缺少或非法时拒绝执行，不得自行使用固定 89。
    if (!this.env.supervised || this.env.reauthExitCode === null) {
      throw new Error(
        "Reauthentication unavailable: not running under agentos-tui launcher or reauth exit code not injected",
      );
    }
    // 拒绝与 handoff 或另一重新认证请求并发。
    if (this.handoff.isHandoffInProgress() || this.reauthInProgress) {
      throw new Error("Handoff or reauthentication already in progress");
    }

    this.reauthInProgress = true;
    // 通知 handoff 端口，使其拒绝后续 handoff 请求。
    this.handoff.markReauthInProgress();

    try {
      const exitCode = this.env.reauthExitCode;
      if (exitCode === null) {
        // 上面已校验，类型收窄失败时显式抛出
        throw new Error("Reauth exit code unexpectedly null after validation");
      }
      // 不发送 interrupt、不取消任务；直接走统一关闭路径。
      await this.lifecycle.closeUi({
        reason: "reauth-required",
        exitCode,
      });
    } catch (err) {
      // 清理失败：尽力恢复终端，以非动作错误码退出；不得让 launcher 在清理不完整时刷新并重启。
      this.reauthInProgress = false;
      await this.lifecycle.closeUi({
        reason: "fatal",
        exitCode: LAUNCHER_EXIT_INTERNAL_ERROR,
      });
      throw err;
    }
    // closeUi 成功路径不返回（process.exit）
  }

  /** 测试用：reauth 是否已开始。 */
  isReauthInProgress(): boolean {
    return this.reauthInProgress;
  }
}
