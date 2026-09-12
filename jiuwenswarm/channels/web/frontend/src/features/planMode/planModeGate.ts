/**
 * 计划模式开关的**唯一决策层**。
 *
 * 背景：切换计划模式在 UI 上有多个入口（输入框旁的 Plan 开关 / 下拉菜单项 /
 * 「计划」chip 的关闭按钮 / `/plan` 斜杠命令）。历史实现里每个入口各自判断
 * 「现在能不能切」，而且都只看 `isProcessing` 一个信号——但 `ask_user` 弹出、
 * 等待用户回答时 `isProcessing` 会变回 `false`（`chat.ask_user_question` 事件只
 * 入队 `pendingQuestions`，不置 `isProcessing`），于是这些闸门在「会话其实还没结束」
 * 时全部放行，`/plan` 和 chip 关闭按钮都能绕过置灰限制关掉计划模式。
 *
 * 这里把「会话是否忙」的口径和「计划开关能否切换」的判断收敛到一处：
 *   - `isSessionBusyForPlanToggle`：一次性定义「会话进行中 / 等待回答 / 暂停中」；
 *   - `evaluatePlanToggle`：开 / 关两个方向逐条对齐输入框旁 Plan 开关的禁用条件；
 *   - `applyPlanToggle`：校验通过才真正翻 `planStore`，命中限制回调 `onBlocked`。
 *
 * 所有**用户主动**切换计划模式的入口都必须走这里。后端事件推送
 * （`plan.mode_exited`）与会话生命周期同步（刷新恢复 / 新会话迁移）是状态同步、
 * 不是用户操作，仍直接调 `planStore.setActive`，不经过本闸门。
 */

import { useChatStore } from '../../stores/chatStore';
import { useGoalStore } from '../../stores/goalStore';
import { usePlanStore } from '../../stores/planStore';
import {
  PLAN_ENTRY_SOURCE_PLAN_TOGGLE,
  PLAN_ENTRY_SOURCE_SLASH_COMMAND,
} from './planEntrySource';

/** 命中限制时给用户的提示文案 i18n key（都是项目已有 key，不新增）。 */
export type PlanToggleBlockReason =
  | 'plan.closeTagDisabled'
  | 'plan.toolbarUnavailableGoal'
  | 'plan.toolbarUnavailableProcessing';

export interface PlanToggleDecision {
  /** 是否允许切到目标状态。 */
  ok: boolean;
  /** `ok` 为 false 时命中的提示文案 key。 */
  reason?: PlanToggleBlockReason;
}

/**
 * 会话是否处于「不允许切换计划模式」的状态。
 *
 * 三个信号取或：
 *   - `isProcessing`：这一轮对话 / 计划正在生成或执行；
 *   - `isPaused`：这一轮被暂停（回合尚未结束）；
 *   - `pendingQuestions` 非空：仍在等用户回答——此时 `isProcessing`
 *     可能已是 false，但会话显然没结束，必须一并算「忙」。
 *
 * 这是唯一口径：以后要调整「会话忙」的判定只改这里。
 */
export function isSessionBusyForPlanToggle(sessionId: string | null | undefined): boolean {
  if (!sessionId) return false;
  const runtime = useChatStore.getState().runtimes[sessionId];
  if (!runtime) return false;
  return (
    Boolean(runtime.isProcessing) ||
    Boolean(runtime.isPaused) ||
    runtime.pendingQuestions.length > 0
  );
}

/** 该会话是否还有未完成目标（active / paused / blocked 都算，只有 completed 不算）。 */
function hasUnfinishedGoal(sessionId: string | null | undefined): boolean {
  if (!sessionId) return false;
  const goal = useGoalStore.getState().runtimes[sessionId]?.goal ?? null;
  return goal != null && goal.status !== 'completed';
}

/**
 * 计划开关能否切到 `next` 状态。逐条对齐输入框旁 UI 计划开关的禁用条件
 * （InputArea.tsx 内 `planDisabled` 一带）：
 *   - 打开方向（`next === true`）：已有未完成目标 → 不可；会话忙 → 不可；
 *   - 关闭方向（`next === false`）：会话忙 → 不可。
 *
 * UI 开关的 disabled 视觉、下拉菜单项、chip 关闭按钮、`/plan` 命令都调这里，
 * 保证「同一语义、多入口、同一套限制」。
 */
export function evaluatePlanToggle(
  sessionId: string | null | undefined,
  next: boolean,
): PlanToggleDecision {
  const busy = isSessionBusyForPlanToggle(sessionId);
  if (next) {
    if (hasUnfinishedGoal(sessionId)) {
      return { ok: false, reason: 'plan.toolbarUnavailableGoal' };
    }
    if (busy) {
      return { ok: false, reason: 'plan.toolbarUnavailableProcessing' };
    }
    return { ok: true };
  }
  if (busy) {
    return { ok: false, reason: 'plan.closeTagDisabled' };
  }
  return { ok: true };
}

export interface ApplyPlanToggleOptions {
  /** 是否来自用户手动打开开关（打开方向默认为 true，携带 plan_entry_source）。 */
  explicitEntry?: boolean;
  /** entry source：UI 开关 `plan_toggle`，`/plan` 命令 `slash_command`。 */
  entrySource?: typeof PLAN_ENTRY_SOURCE_PLAN_TOGGLE | typeof PLAN_ENTRY_SOURCE_SLASH_COMMAND;
  /** 命中限制时的回调（`/plan` 用它弹轻量提示条；UI 开关不传、靠 disabled 视觉）。 */
  onBlocked?: (reason: PlanToggleBlockReason) => void;
}

/**
 * 校验通过才真正翻 `planStore`。
 *
 * @returns 是否执行了状态变更（false 表示被闸门拦下）。
 */
export function applyPlanToggle(
  sessionId: string | null | undefined,
  next: boolean,
  options: ApplyPlanToggleOptions = {},
): boolean {
  if (!sessionId) return false;
  const decision = evaluatePlanToggle(sessionId, next);
  if (!decision.ok) {
    if (decision.reason) options.onBlocked?.(decision.reason);
    return false;
  }
  const store = usePlanStore.getState();
  store.ensureRuntime(sessionId);
  if (next) {
    store.setActive(sessionId, true, {
      explicitEntry: options.explicitEntry ?? true,
      entrySource: options.entrySource ?? PLAN_ENTRY_SOURCE_PLAN_TOGGLE,
    });
  } else {
    store.setActive(sessionId, false);
  }
  return true;
}
