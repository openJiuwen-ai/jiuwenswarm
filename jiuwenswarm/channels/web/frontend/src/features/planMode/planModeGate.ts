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
 *
 * `/plan` 的接入方式与另外三个入口不同，改动前先看清，别误判成漏了闸门：
 *   - 两条用户路径（输入框精确输入、建议菜单选中）都先过
 *     `InputArea.executeSlashCommand` 的前置守卫，那里调本闸门并把 `reason`
 *     弹成轻量提示条，命中就消费掉这次输入、不翻开关；
 *   - 集群会话更早被 `slashCommands/registry.ts` 的 `planCommand.execute`
 *     整体拦下（`ctx.mode === 'team'` 直接回一条"仅对单 agent 开放"），根本走不到
 *     切换逻辑；
 *   - 因此 `registry.ts` 的 `togglePlanFromSlash` 只是竞态兜底，它自己只判忙态
 *     （复用本文件的 `isSessionBusyForPlanToggle`）与 Goal 互锁，不判集群锁——它
 *     只对非集群会话可达，集群锁在那里恒为假，两侧结论不会分叉。
 */

import { useChatStore } from '../../stores/chatStore';
import { useGoalStore } from '../../stores/goalStore';
import { usePlanStore } from '../../stores/planStore';
import { useSessionStore } from '../../stores/sessionStore';
import { hasUnfinishedGoal } from '../../components/ChatPanel/slashCommands/semantics';
import {
  PLAN_ENTRY_SOURCE_PLAN_TOGGLE,
  PLAN_ENTRY_SOURCE_SLASH_COMMAND,
} from './planEntrySource';

/** 命中限制时给用户的提示文案 i18n key。 */
export type PlanToggleBlockReason =
  | 'plan.closeTagDisabled'
  | 'plan.toolbarUnavailableGoal'
  | 'plan.toolbarUnavailableProcessing'
  | 'plan.toolbarUnavailableTeamLocked';

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
function sessionHasUnfinishedGoal(sessionId: string | null | undefined): boolean {
  if (!sessionId) return false;
  const goal = useGoalStore.getState().runtimes[sessionId]?.goal ?? null;
  return hasUnfinishedGoal(goal);
}

/**
 * 新建对话页的占位 session id。
 *
 * 这里写字面量而不是 import `NEW_CONVERSATION_ID`：那个模块会拉进整个 stores
 * barrel，形成 import 环（`sessionStore.ts` 里的 `TRANSIENT_NEW_CONVERSATION_ID`
 * 同样这么处理）。改动前先核对 `multi-session/state/newConversationLifecycle.ts`。
 */
const NEW_CONVERSATION_SESSION_ID = 'new';

/** 该会话是否已发过消息（会话记录的 message_count 或已加载消息非空）。 */
function sessionHasHistory(sessionId: string): boolean {
  const store = useSessionStore.getState();
  const session = store.currentSession?.session_id === sessionId
    ? store.currentSession
    : store.sessions.find((item) => item.session_id === sessionId) ?? null;
  if ((session?.message_count ?? 0) > 0) return true;
  return (useChatStore.getState().runtimes[sessionId]?.messages.length ?? 0) > 0;
}

/**
 * 集群会话的计划开关是否已被建队冻结（双向只读）。
 *
 * 集群的计划能力是建队时的一次性决定：`enable_team_plan` 与 leader 的
 * `CODE_AGENT_MODE` / `TEAM_PLAN_APPROVAL` 两条 rail 由首条消息的 mode 决定
 * （`jiuwenswarm/agents/swarm/config_specs.py` 的 `is_team_plan_mode(mode)` 分支），
 * 建队后没有补挂 rail 的路径，运行期 plan 状态同步又对集群直接返回
 * （`jiuwenswarm/runtime/plan.py` 的 `if resolved.is_team: return`）。所以建队后
 * 拨动开关只会改变 wire mode 字符串，队伍行为分毫不动——是个假开关，还会让
 * 「开启计划」的互斥副作用绕过动态工作流的会话冻结锁（issue #6361）。
 *
 * 单 agent 不受此限：它的 plan rails 常挂、每轮走运行期状态同步，计划模式本就
 * 设计为可反复开关。
 *
 * 判定公式与动态工作流开关那把锁同构（`InputArea.tsx` 的
 * `swarmflowToggleDisabled`），只去掉其中的 `isProcessing`——忙态由本文件的
 * 忙态分支先行拦下，且提示文案不同。
 */
function isTeamPlanToggleLocked(sessionId: string): boolean {
  if (sessionId === NEW_CONVERSATION_SESSION_ID) return false;
  if (useSessionStore.getState().runtimes[sessionId]?.mode !== 'team') return false;
  return sessionHasHistory(sessionId);
}

/**
 * 计划开关能否切到 `next` 状态。逐条对齐输入框旁 UI 计划开关的禁用条件
 * （InputArea.tsx 内 `planDisabled` 一带）：
 *   - 两个方向共用：集群会话已建队 → 不可（见 `isTeamPlanToggleLocked`）；
 *   - 打开方向（`next === true`）：已有未完成目标 → 不可；会话忙 → 不可；
 *   - 关闭方向（`next === false`）：会话忙 → 不可。
 *
 * 集群锁排在忙态之前：忙是暂时的、文案等于"稍后可试"，而建队冻结是永久的，
 * 两者同时成立时报忙态会骗用户白等（集群会话在队伍活着期间常驻忙态，几乎永远
 * 轮不到集群锁）。调度模式的 `readonlyHint` 也是这么做的——只要开关被冻结就报
 * 冻结，不区分是否正忙。
 *
 * UI 开关的 disabled 视觉、下拉菜单项、chip 关闭按钮都直接调这里；`/plan` 经
 * `InputArea.executeSlashCommand` 的前置守卫调这里（接法与限制见文件头注释），
 * 保证「同一语义、多入口、同一套限制」。
 */
export function evaluatePlanToggle(
  sessionId: string | null | undefined,
  next: boolean,
): PlanToggleDecision {
  if (sessionId && isTeamPlanToggleLocked(sessionId)) {
    return { ok: false, reason: 'plan.toolbarUnavailableTeamLocked' };
  }
  const busy = isSessionBusyForPlanToggle(sessionId);
  if (next) {
    if (sessionHasUnfinishedGoal(sessionId)) {
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

/**
 * 刷新 / 切回会话时，后端回报的「这个会话在计划里」该不该点亮计划开关。
 *
 * 动态工作流优先：它是建队时冻结并落盘的会话级配置
 * （`session_swarmflow_config`），而后端的 `in_plan` 对集群只由 `metadata.mode`
 * 推导（见 `agent_ws_server._combine_session_in_plan`），那里的 plan 段可能来自
 * 修复前那次对已建队会话无效的开关拨动。两者都声明时以动态工作流为准，
 * 「计划 + 动态工作流同时点亮」的矛盾态与历史脏会话一并收敛（issue #6361）。
 */
export function resolveRestoredPlanActive(
  inPlan: boolean | undefined,
  enableSwarmflow: boolean,
): boolean {
  if (enableSwarmflow) return false;
  return inPlan === true;
}

/**
 * 动态工作流一旦确认开启，就把该会话的计划标签灭掉。
 *
 * `resolveRestoredPlanActive` 只能决定「要不要点亮」，管不了恢复链路的时序：
 * `handleRestoreSession` 先按 `metadata.mode` 点亮计划，而动态工作流配置要等
 * `loadSessionMetadata` 回来才 hydrate，那一刻读到的 `enableSwarmflow` 必然还是
 * false。所以把「动态工作流开 → 计划灭」这条不变量落在 swarmflow 置真的那一刻，
 * 两条恢复链路与任何先后顺序都成立（issue #6361 的历史脏会话）。
 *
 * @returns 是否真的灭掉了（false 表示该会话没开动态工作流，不插手）。
 */
export function enforceSwarmflowPlanPrecedence(sessionId: string | null | undefined): boolean {
  if (!sessionId) return false;
  if (!useSessionStore.getState().runtimes[sessionId]?.enableSwarmflow) return false;
  usePlanStore.getState().setActive(sessionId, false);
  return true;
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
