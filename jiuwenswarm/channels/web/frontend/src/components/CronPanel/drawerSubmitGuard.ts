// 定时任务抽屉防重复提交的纯逻辑，单独成模块便于在 node --test 里锁定行为
// （cron.job.create/update 走 WebSocket，前端超时上限 15s，慢响应窗口内双击
// "确认"会发出第二个独立请求，创建出两个同名任务）。

/**
 * 抽屉"确认"按钮的可用性判定：必填项齐全 && 执行计划未过期 && 无在途提交。
 * 与 HeartbeatTaskDrawer 的 canSubmit 推导同构；submitting 条件是本次修复新增的
 * 防重复提交核心，纯函数化后由 tests/cronDrawerSubmitGuard.test.mjs 锁定。
 */
export function deriveDrawerCanSubmit(
  missingFieldLabels: readonly string[],
  scheduleAlreadyExpired: boolean,
  submitting: boolean,
): boolean {
  return missingFieldLabels.length === 0 && !scheduleAlreadyExpired && !submitting;
}

export interface SubmitGuard {
  /**
   * 无在途提交时置位并放行；已在途时拒绝。同步方法兜底同一事件循环内的
   * 双击（React state 置位要等下一次渲染才反映到 disabled，兜不住同帧连点）。
   */
  tryBegin(): boolean;
  /** 提交结束（无论成败）复位，放行下一次提交。 */
  end(): void;
  isBusy(): boolean;
}

export function createSubmitGuard(): SubmitGuard {
  let busy = false;
  return {
    tryBegin() {
      if (busy) return false;
      busy = true;
      return true;
    },
    end() {
      busy = false;
    },
    isBusy() {
      return busy;
    },
  };
}
