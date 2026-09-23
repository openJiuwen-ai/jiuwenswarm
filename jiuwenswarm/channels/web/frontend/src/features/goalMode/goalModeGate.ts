/**
 * Goal「武装」开关的**唯一决策层**——形状参照 `features/planMode/planModeGate.ts`。
 *
 * 背景：设置目标目前有两个用户入口——输入框旁"+"菜单的「目标」开关（图形化）、
 * `/goal` 斜杠命令（命令行，不带目标正文时选中即代表"接下来发的这条消息就是
 * 目标正文"）。在这个模块出现之前，只有"+"菜单在 `InputArea.tsx` 里手写了一份
 * "能不能武装"的判断，命令行入口完全没有对应逻辑——`/goal` 选中后只会插入一个
 * 占位 chip，不会像"+"菜单那样立刻进入武装态、显示"目标"tag，这正是
 * bugfix 2026092201 bug001 子问题 3 的根因。
 *
 * 这里把"目标能不能进入/退出武装态"收敛到一处，图形化开关和 `/goal` 命令的
 * "选中即武装"分支都改为调用这里，不再各自定制一份判断。
 *
 * 与 Plan 的关系：Goal 与 Plan 互斥（同一时间只能有一个未完成的目标或已提交的
 * 计划）。"是否有未完成目标"直接复用 `slashCommands/semantics.ts` 里已经导出的
 * `hasUnfinishedGoal`，不再像 `planModeGate.ts` 之前那样重复定义一份同名函数。
 */

import { useGoalStore } from '../../stores/goalStore';
import { usePlanStore } from '../../stores/planStore';
import { hasUnfinishedGoal } from '../../components/ChatPanel/slashCommands/semantics';
// 复用 Plan 那边已有的"会话是否忙"判断——名字带 PlanToggle 是历史遗留，逻辑本身跟 Plan 无关，
// 纯粹是"这个会话进行中 / 暂停中 / 等待用户回答"，Goal 关闭方向的忙态保护直接拿来用即可，
// 不再重复定义一份（bugfix 2026092201 bug001 第2轮）。
import { isSessionBusyForPlanToggle as isSessionBusyForModeToggle } from '../planMode/planModeGate';

/** 命中限制时给用户的提示文案 i18n key（都是项目已有 key，不新增）。 */
export type GoalArmBlockReason =
  | 'goal.toolbarUnavailable'
  | 'goal.toolbarUnavailablePlan'
  | 'goal.closeTagDisabled';

export interface GoalArmDecision {
  /** 是否允许切到目标武装状态。 */
  ok: boolean;
  /** `ok` 为 false 时命中的提示文案 key。 */
  reason?: GoalArmBlockReason;
}

function sessionHasUnfinishedGoal(sessionId: string | null | undefined): boolean {
  if (!sessionId) return false;
  const goal = useGoalStore.getState().runtimes[sessionId]?.goal ?? null;
  return hasUnfinishedGoal(goal);
}

/**
 * 目标是否处于 `active` 状态——bugfix 2026092201 bug001 第6轮，用 Playwright 实测出来的
 * 真正根因。
 *
 * 第3轮以为 `hasPendingGoalAction || isSessionBusyForModeToggle` 已经拼出了"从消息发出到
 * 执行结束"的完整忙态窗口，但用 Playwright 实测"/goal set"整条链路后发现完全不是这样：
 * `pendingAction` 和 `goal` 对象是在**同一次** `applyIncomingGoal` 里一起落地的
 * （`goalStore.setGoal(sessionId, goal); goalStore.setPendingAction(sessionId, null);`
 * 紧挨着执行，中间没有任何 await）——也就是说"目标对象第一次出现、tag 从无到有"的那一刻，
 * `pendingAction` 已经被同一次更新清空为 null 了，`hasPendingGoalAction` 永远不可能在
 * "目标已存在"之后还观测到 true。而 `isSessionBusyForModeToggle` 依赖的 `isProcessing`
 * 只在后端真正开始流式吐字（第一个 `chat.delta`）时才置位——真机实测这一步比"目标对象创建
 * 完成、状态变成 active"晚了 **17 秒以上**（后端在真正开始流式输出前显然还有一段自己的准备
 * 时间）。也就是说"目标已经是 active、tag 已经显示出来，但模型还没真正开始吐字"这一整段
 * （实测数十秒量级，不是毫秒级竞态）完全没有任何忙态信号覆盖，`evaluateGoalArm` 第3轮的
 * 判断在这段时间里全程判定"不忙"——这正是第6轮 Playwright 复现出来的"tag 一直没被禁用"。
 *
 * 真正稳妥的信号其实一直都在：`goal.status === 'active'` 本身就是后端权威下发的"目标正在
 * 被追求"状态，GoalBar 自己的"进行中 · Ns"计时器（`formatElapsedFromGoal`）也是直接认这个
 * 字段，不依赖 `isProcessing`。`paused`/`blocked` 是用户/Agent 主动让出的非执行态，
 * GoalBar 允许在这两种状态下编辑/删除，不应该被这个忙态判断拦住，只有 `active` 才要挡。
 */
function isGoalStatusActive(sessionId: string | null | undefined): boolean {
  if (!sessionId) return false;
  return useGoalStore.getState().runtimes[sessionId]?.goal?.status === 'active';
}

/**
 * 目标是否处于"上一次操作（set/pause/resume/clear 任一个）已经发出去、还没收到回执"的
 * 窗口——单纯的"双击/重复提交"防护，不代表"目标正在被 Agent 执行"。
 *
 * bugfix 2026092201 bug001 第7轮教训：这两件事必须分开。`pendingAction` 从 bugfix 之前
 * 就一直是 GoalBar 编辑/暂停/删除三个按钮共用的唯一忙态信号（`isBusy = pendingAction !==
 * null`），语义就是"我刚点过一个操作，回执还没回来，先别让我再点"——跟"目标当前是不是
 * active、Agent 是不是正在忙"完全是两回事。第6轮为了堵住"目标 active 时被随手删除/关闭"
 * 这个漏洞，把 `isGoalStatusActive` 塞进了 `isGoalSessionBusy`，但 `isGoalSessionBusy`
 * 被同时套用在删除（该挡）和编辑/暂停（不该挡——"暂停"存在的意义就是在 active 时点它，
 * active 时反而禁用是自相矛盾）上，把两种不同语义的"忙"混成了一个开关。
 *
 * 现在的分工（第9轮定稿）：
 *   - 这个函数（`hasPendingGoalAction`，纯双击防护）供 GoalBar 全部三个按钮（编辑/
 *     暂停(恢复)/删除）使用——这三个都是用户专门打开 GoalBar 后有意识点击的正式操作，
 *     不需要"防手滑"这层更严格的保护；
 *   - `isGoalSessionBusy`（含 `isGoalStatusActive`，"目标正在执行、不该被随手终止"）只用于
 *     "轻量切换/随手一点"性质的终止入口——"+"菜单目标开关的关闭方向、目标 tag 的 ×
 *     关闭按钮、`/goal clear` 命令行，这几个操作用户可能"顺手一点/一行命令"就触发，
 *     active 时要挡，防止手滑关掉正在执行的目标。
 *
 * `goalAction`（`useWebSocket.ts`）在 `await sendGoalStreamCommand(...)`/`await
 * requestGoalAction(...)` 之前就同步 `setPendingAction`，请求一发出去就置位，一直到
 * `goal.snapshot`/`goal.updated`/`runtime.accepted` 等回执事件落地才清空。
 */
export function hasPendingGoalAction(sessionId: string | null | undefined): boolean {
  if (!sessionId) return false;
  return useGoalStore.getState().runtimes[sessionId]?.pendingAction != null;
}

/**
 * 目标是否处于"不该被随手终止/清除"的忙态——**只给"轻量切换/随手一点"性质的终止入口用**：
 * 目标 tag 的 × 关闭按钮、"+"菜单目标开关的关闭方向、`/goal clear` 命令行。这几个入口
 * 共同点是用户可能"顺手一点/一行命令"就触发，active 时要挡，防止手滑关掉正在执行的目标。
 *
 * **不要**拿这个函数去挡：
 *   - "编辑"/"暂停"（GoalBar）——这两个操作恰恰是给 active 目标用的（尤其暂停：只有
 *     active 才需要暂停，用这个函数挡会把暂停按钮变成摆设），只用 `hasPendingGoalAction`
 *     做双击防护即可（bugfix 2026092201 bug001 第7轮教训）；
 *   - GoalBar 的"删除"按钮——第7轮曾经把它归到这里、第9轮改了回去：删除是用户专门打开
 *     GoalBar 悬浮条后有意识点击的正式终止入口，跟"随手一点"的三个入口语义不同，不该被
 *     锁死，否则用户没有任何正式渠道能主动终止一个正在跑偏的 active 目标（同样只用
 *     `hasPendingGoalAction` 做双击防护）。
 *
 * 三段取或，覆盖"从消息发出到执行结束"全程（第3/6轮，第6轮用 Playwright 实测补齐了中间
 * 那一大段真正的空白）：
 *   1. `hasPendingGoalAction`——请求刚发出、`goal` 对象还没创建出来这一小段（几乎瞬间）；
 *   2. `isGoalStatusActive`——`goal.status === 'active'` 期间，覆盖"目标已创建、Agent
 *      可能正在或即将执行"这一大段，是这里最主要、最可靠的信号；
 *   3. `isSessionBusyForModeToggle`——兜底 `isProcessing`/暂停中/等待 ask_user 回答，
 *      覆盖 `goal.status` 万一没能及时反映真实执行态的边缘情况。
 */
export function isGoalSessionBusy(sessionId: string | null | undefined): boolean {
  return (
    hasPendingGoalAction(sessionId) ||
    isGoalStatusActive(sessionId) ||
    isSessionBusyForModeToggle(sessionId)
  );
}

/**
 * Plan 是否已经真正生效（开关打开 + 至少发过一条 Plan 消息、`pendingExplicitEntry`
 * 已被消费）。只有这种"已提交"态才挡 Goal——刚打开但还没发消息的 Plan 属于未提交
 * 态，武装 Goal 时可以顺手把它顶掉（与"+"菜单原有行为一致，见 `applyGoalArm`）。
 */
function isPlanCommittedForSession(sessionId: string | null | undefined): boolean {
  if (!sessionId) return false;
  const runtime = usePlanStore.getState().runtimes[sessionId];
  return Boolean(runtime?.active) && !runtime?.pendingExplicitEntry;
}

/**
 * Goal 能否切到 `next` 武装状态。
 *   - 打开方向（`next === true`）：已有未完成目标 → 不可；Plan 已提交 → 不可；
 *   - 关闭方向（`next === false`）：`isGoalSessionBusy` 判定为忙 → 不可，见该函数注释
 *     （拼了三段信号才覆盖"从消息发出到执行结束"全程，第6轮 Playwright 实测才最终定稿）。
 *
 * bugfix 2026092201 bug001 第2轮修订：关闭方向原来不受任何限制，导致目标真正执行期间
 * 仍能通过"+"菜单开关 / tag 关闭按钮一键清除、把正在跑的会话搞停——用户测试后明确要求
 * 对齐 Plan。第3轮补了 `hasPendingGoalAction`，第6轮又补了 `isGoalStatusActive`——
 * 前两轮都只靠静态读代码推理，Playwright 实测才发现真正的大段空白在哪里，具体见
 * `isGoalStatusActive` 的详细注释。
 */
export function evaluateGoalArm(
  sessionId: string | null | undefined,
  next: boolean,
): GoalArmDecision {
  if (!next) {
    if (isGoalSessionBusy(sessionId)) {
      return { ok: false, reason: 'goal.closeTagDisabled' };
    }
    return { ok: true };
  }
  if (sessionHasUnfinishedGoal(sessionId)) {
    return { ok: false, reason: 'goal.toolbarUnavailable' };
  }
  if (isPlanCommittedForSession(sessionId)) {
    return { ok: false, reason: 'goal.toolbarUnavailablePlan' };
  }
  return { ok: true };
}

export interface ApplyGoalArmOptions {
  /** 命中限制时的回调（`/goal` 命令用它弹轻量提示条；"+"菜单开关不传、靠 disabled 视觉）。 */
  onBlocked?: (reason: GoalArmBlockReason) => void;
}

/**
 * 校验通过才真正翻 `goalStore.armed`。
 *
 * 打开方向命中"Plan 未提交"态时顺手把 Plan 关掉（已提交的 Plan 在
 * `evaluateGoalArm` 那层已经拦下了，走到这里的 Plan 只可能是未提交态）。
 *
 * 关闭方向只负责把 `armed` 收回 false；如果同时存在一个已经真实创建的目标需要
 * 清除，那是调用方的职责——`onClearGoal` 是个外部回调，这个纯函数模块拿不到，
 * 调用方仍需在调这里之前/之后自行处理。
 *
 * @returns 是否真的执行了状态变更（false 表示被闸门拦下）。
 */
export function applyGoalArm(
  sessionId: string | null | undefined,
  next: boolean,
  options: ApplyGoalArmOptions = {},
): boolean {
  if (!sessionId) return false;
  const decision = evaluateGoalArm(sessionId, next);
  if (!decision.ok) {
    if (decision.reason) options.onBlocked?.(decision.reason);
    return false;
  }
  if (next) {
    const planStore = usePlanStore.getState();
    if (planStore.runtimes[sessionId]?.active) {
      planStore.setActive(sessionId, false);
    }
  }
  useGoalStore.getState().setArmed(sessionId, next);
  return true;
}
