import { webRequest } from '../../../services/webClient';
import type { Message } from '../../../types/message';
import { useGoalStore } from '../../../stores/goalStore';
import { usePlanStore } from '../../../stores/planStore';
import { isSessionBusyForPlanToggle } from '../../../features/planMode/planModeGate';
import { evaluateGoalArm } from '../../../features/goalMode/goalModeGate';
import { NEW_CONVERSATION_ID } from '../../../multi-session/state/newConversationLifecycle';
import { resolvePlanGoalInterlock } from './semantics';

/**
 * 斜杠命令注册表（/fork、/compact、/plan、/goal、/persist）。
 * 后端与 TUI 共用 agent_ws_server；命令结果以 system 消息留痕，
 * 第一行回显命令行，MessageItem 按 isCommandOutput 渲染。
 */

/** 斜杠命令执行上下文：由 InputArea 在提交期构造并注入 */
export type SlashCommandContext = {
  sessionId: string;
  /** 当前会话模式（'agent' / 'team' 等），随请求带给后端做 agent 解析 */
  mode: string;
  /** 用户原始输入行（如 "/persist 跟进发布"），用于在结果消息第一行回显 */
  inputLine: string;
  addMessage: (sessionId: string, message: Message) => void;
  submitMessage?: (content: string) => void;
  forkConversation: (sourceSessionId: string) => Promise<void>;
  runGoalAction: (
    sessionId: string,
    action: GoalSlashAction,
    objective?: string,
  ) => Promise<GoalSlashSnapshot>;
  confirmGoalOverwrite: (
    currentObjective: string,
    requestedObjective: string,
  ) => boolean | Promise<boolean>;
};

export interface SlashCommand {
  name: string;
  /** 是否要求真实会话；纯本地命令（/plan）设 false，欢迎页也能用 */
  requiresSession?: boolean;
  execute: (ctx: SlashCommandContext, args: string) => Promise<void>;
}

interface PlanSlashStore {
  ensureRuntime: (sessionId: string) => unknown;
  isActive: (sessionId: string) => boolean;
  setActive: (
    sessionId: string,
    active: boolean,
    options?: { explicitEntry?: boolean; entrySource?: 'slash_command' },
  ) => void;
}

interface GoalSlashStore {
  getRuntime: (sessionId: string) =>
    | { goal: { status: string; objective?: string } | null; armed: boolean }
    | undefined;
  setArmed: (sessionId: string, armed: boolean) => void;
}

interface GoalPlanSlashStore {
  isActive: (sessionId: string) => boolean;
  hasPendingExplicitEntry: (sessionId: string) => boolean;
  setActive: (sessionId: string, active: boolean) => void;
}

export type GoalSlashAction = 'get' | 'set' | 'pause' | 'resume' | 'clear';

export type GoalSlashSnapshot = {
  objective: string;
  status: string;
} | null;

export type GoalSlashIntent =
  | { action: 'get' | 'pause' | 'resume' | 'clear' }
  | { action: 'set'; objective: string };

export type GoalSetPreparationResult =
  | 'ready'
  | 'confirm_overwrite'
  | 'blocked_by_plan';

export type PlanSlashToggleResult =
  | 'activated'
  | 'deactivated'
  | 'blocked_by_goal'
  | 'blocked_by_busy';

/** Apply `/plan` through the same Goal interlock used by the toolbar. */
export function togglePlanFromSlash(
  sessionId: string,
  planStore: PlanSlashStore = usePlanStore.getState(),
  goalStore: GoalSlashStore = useGoalStore.getState(),
  // 会话进行中 / 暂停 / 等待 ask_user 回答时不允许切换——与 InputArea 的
  // executeSlashCommand 守卫、输入框旁 Plan 开关、「计划」chip 关闭按钮共用
  // 同一套 planModeGate 判断（ask_user 待回答时 isProcessing 已回到 false，
  // 只看它的旧闸门会漏放）。默认参数便于单测注入。
  sessionBusy: boolean = isSessionBusyForPlanToggle(sessionId),
): PlanSlashToggleResult {
  planStore.ensureRuntime(sessionId);
  if (sessionBusy) return 'blocked_by_busy';
  if (planStore.isActive(sessionId)) {
    planStore.setActive(sessionId, false);
    return 'deactivated';
  }

  const goalRuntime = goalStore.getRuntime(sessionId);
  const goalInterlock = resolvePlanGoalInterlock(goalRuntime?.goal, goalRuntime?.armed ?? false);
  if (goalInterlock === 'block') return 'blocked_by_goal';
  if (goalInterlock === 'clear_goal_armed') {
    goalStore.setArmed(sessionId, false);
  }
  planStore.setActive(sessionId, true, {
    explicitEntry: true,
    entrySource: 'slash_command',
  });
  return 'activated';
}

/** Parse `/goal` exactly like the TUI command handler. */
export function parseGoalSlashArgs(args: string): GoalSlashIntent {
  const normalizedArgs = args.trim();
  if (!normalizedArgs) return { action: 'get' };

  const lower = normalizedArgs.toLowerCase();
  if (lower === 'pause' || lower === 'resume' || lower === 'clear') {
    return { action: lower };
  }
  if (lower.startsWith('set ')) {
    return { action: 'set', objective: normalizedArgs.slice(4).trim() };
  }
  if (lower === 'set') return { action: 'set', objective: '' };

  // TUI 同样把 get/stop 等非保留字当作目标正文。
  return { action: 'set', objective: normalizedArgs };
}

/** Apply the same Goal/Plan interlock used by the composer toolbar. */
export function prepareGoalSetFromSlash(
  sessionId: string,
  overwriteConfirmed = false,
  planStore: GoalPlanSlashStore = usePlanStore.getState(),
  goalStore: GoalSlashStore = useGoalStore.getState(),
): GoalSetPreparationResult {
  const currentGoal = goalStore.getRuntime(sessionId)?.goal;
  if (currentGoal && currentGoal.status !== 'completed' && !overwriteConfirmed) {
    return 'confirm_overwrite';
  }

  if (planStore.isActive(sessionId)) {
    if (!planStore.hasPendingExplicitEntry(sessionId)) return 'blocked_by_plan';
    // 刚打开但尚未发送消息的 Plan 可以被 Goal 顶掉，与工具栏一致。
    planStore.setActive(sessionId, false);
  }
  goalStore.setArmed(sessionId, false);
  return 'ready';
}

/** 解析 "/persist some task" → { name: "persist", args: "some task" } */
export function parseSlashLine(raw: string): { name: string; args: string } {
  const trimmed = raw.trim().replace(/^\/+/, '');
  const spaceIdx = trimmed.search(/\s/);
  if (spaceIdx === -1) return { name: trimmed.toLowerCase(), args: '' };
  return {
    name: trimmed.slice(0, spaceIdx).toLowerCase(),
    args: trimmed.slice(spaceIdx + 1).trim(),
  };
}

export function findSlashCommand(name: string): SlashCommand | undefined {
  const lower = name.toLowerCase();
  return SLASH_COMMANDS.find((c) => c.name === lower);
}

/** 命令结果消息：保留兼容 content，同时附带结构化字段供专用卡片渲染。 */
function commandResultMessage(inputLine: string, output: string): Message {
  const normalizedInput = inputLine.trim();
  const normalizedOutput = output.trim();
  return {
    id: `slash-out-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    role: 'system',
    isCommandOutput: true,
    commandName: parseSlashLine(normalizedInput).name,
    commandInput: normalizedInput,
    commandOutput: normalizedOutput,
    content: normalizedOutput ? `${normalizedInput}\n${normalizedOutput}` : normalizedInput,
    timestamp: new Date().toISOString(),
  };
}

/** /fork —— 复制当前会话，并复用 App 的会话恢复流程切换到副本。 */
const forkCommand: SlashCommand = {
  name: 'fork',
  execute: async (ctx) => {
    try {
      await ctx.forkConversation(ctx.sessionId);
    } catch {
      ctx.addMessage(ctx.sessionId, commandResultMessage(ctx.inputLine, '分叉会话失败，请稍后再试。'));
    }
  },
};

/** /compact —— 压缩对话历史为摘要；token 计数刷新由 context.* 事件监听处理。 */
const compactCommand: SlashCommand = {
  name: 'compact',
  execute: async (ctx) => {
    let output: string;
    try {
      const res = await webRequest<{
        result: string;
        stats?: { total_tokens?: number; raw_total_tokens?: number };
      }>(
        'command.compact',
        { session_id: ctx.sessionId, mode: ctx.mode },
        { timeoutMs: 600000 },
      );
      if (res.result === 'busy') {
        output = '压缩已在进行中，请稍候。';
      } else if (res.result === 'noop') {
        output = '上下文已是最优，无需压缩。';
      } else if (res.result === 'compressed') {
        const before = res.stats?.raw_total_tokens ?? 0;
        const after = res.stats?.total_tokens ?? 0;
        const rate = before > 0 ? Math.round(((before - after) / before) * 100) : 0;
        const fmt = (n: number) => Math.max(1, Math.round(n / 1000));
        output = `✓ 上下文已压缩：${fmt(after)}K / ${fmt(before)}K tokens（节省 ${rate}%）`;
      } else {
        output = '压缩未完成，请稍后再试。';
      }
    } catch {
      output = '压缩失败：网络异常或请求超时。';
    }
    ctx.addMessage(ctx.sessionId, commandResultMessage(ctx.inputLine, output));
  },
};

/**
 * /plan —— 翻转 planStore 的 Plan 开关（纯本地，不调后端）。
 * 面板选中或精确输入 `/plan` 时立即翻转，带参数的文本不进入此路径。
 * 开启时置 explicitEntry，下一条真实消息带 agent.plan + plan_entry_source；
 * 集群（team）不支持，与工具栏开关一致。
 *
 * 「会话进行中 / 等待 ask_user 回答 / 有未完成目标」时不允许切换：`togglePlanFromSlash`
 * 里用 `isSessionBusyForPlanToggle`（planModeGate）+ Goal 互斥判断，与输入框旁的
 * Plan 开关、「计划」chip 关闭按钮、InputArea 的 executeSlashCommand 守卫同一套口径。
 * 命中限制时的用户提示由 executeSlashCommand 守卫负责（轻量提示条），这里再兜一道底。
 */
const planCommand: SlashCommand = {
  name: 'plan',
  requiresSession: false,
  execute: async (ctx) => {
    // 集群不支持：仅回提示；正常开关静默（状态已由工具栏可视化）
    if (ctx.mode === 'team') {
      ctx.addMessage(
        ctx.sessionId,
        commandResultMessage(ctx.inputLine, '计划模式仅对单 agent 开放，集群会话不支持。'),
      );
      return;
    }
    const result = togglePlanFromSlash(ctx.sessionId);
    if (result === 'blocked_by_goal' || result === 'blocked_by_busy') {
      // 选择器/输入框守卫已先拦（会话忙时守卫弹轻量提示条）；手工输入或旧页面
      // 竞态命中时这里静默兜底，不把同一条互斥提示反复写进聊天记录。
      return;
    }
  },
};

function goalStatusOutput(goal: GoalSlashSnapshot): string {
  if (!goal) return '当前会话没有持续目标。';
  const statusLabel =
    {
      active: '进行中',
      paused: '已暂停',
      completed: '已完成',
      blocked: '已阻塞',
    }[goal.status] ?? goal.status;
  return `当前目标（${statusLabel}）：${goal.objective}`;
}

/**
 * /goal —— 复用 Web 已有的 Goal 状态机和 GoalBar。
 * 语法与 TUI 一致：无参查询，pause/resume/clear 控制，set <objective>
 * 或任意其他文本设置目标。已有未完成目标时先请用户确认覆盖。
 *
 * `clear` 走 evaluateGoalArm 同一套忙态保护（bugfix 2026092201 bug001 第9轮）：跟"+"菜单
 * 目标开关的关闭方向、目标 tag 的 × 关闭按钮口径一致，目标 active 时不能被命令行随手清掉。
 * `pause`/`resume`/`get` 不受这层限制——暂停本来就该在 active 时可用，对齐 GoalBar 的
 * 暂停按钮（那个按钮从第7轮起就只受"双击防护"限制，不受 active 状态限制）。
 */
const goalCommand: SlashCommand = {
  name: 'goal',
  // 欢迎页可用 `/goal <objective>` 创建会话并设置目标。
  requiresSession: false,
  execute: async (ctx, args) => {
    const intent = parseGoalSlashArgs(args);
    if (intent.action === 'set' && !intent.objective) {
      ctx.addMessage(
        ctx.sessionId,
        commandResultMessage(ctx.inputLine, '用法：/goal [set <目标>|pause|resume|clear]'),
      );
      return;
    }

    if (
      ctx.sessionId === NEW_CONVERSATION_ID &&
      intent.action !== 'get' &&
      intent.action !== 'set'
    ) {
      ctx.addMessage(
        ctx.sessionId,
        commandResultMessage(ctx.inputLine, '请先开始一个对话再控制持续目标。'),
      );
      return;
    }

    try {
      if (intent.action === 'set') {
        let preparation = prepareGoalSetFromSlash(ctx.sessionId);
        if (preparation === 'confirm_overwrite') {
          const currentObjective =
            useGoalStore.getState().getRuntime(ctx.sessionId)?.goal?.objective ?? '';
          const confirmed = await ctx.confirmGoalOverwrite(
            currentObjective,
            intent.objective,
          );
          if (!confirmed) return;
          preparation = prepareGoalSetFromSlash(ctx.sessionId, true);
        }
        if (preparation === 'blocked_by_plan') {
          ctx.addMessage(
            ctx.sessionId,
            commandResultMessage(ctx.inputLine, '计划模式正在进行，请先退出计划模式再设置目标。'),
          );
          return;
        }
        await ctx.runGoalAction(ctx.sessionId, 'set', intent.objective);
        return;
      }

      // bugfix 2026092201 bug001 第9轮：`/goal clear` 在这次改造之前完全没有忙态保护，
      // 直接绕过"+"菜单开关、目标 tag 关闭按钮共用的 evaluateGoalArm，能在目标 active 时
      // 把它当场清掉——跟那两个入口应该"同样不能被随手关闭"的要求不一致，这里补上同一套
      // 判断。`pause`/`resume`/`get` 不受影响：暂停本来就该在 active 时可用（对齐 GoalBar
      // 的暂停按钮），查询任何时候都不该被拦。
      if (intent.action === 'clear') {
        const decision = evaluateGoalArm(ctx.sessionId, false);
        if (!decision.ok) {
          ctx.addMessage(
            ctx.sessionId,
            commandResultMessage(ctx.inputLine, '目标正在执行中，暂时无法清除，请先暂停或等待执行结束。'),
          );
          return;
        }
      }

      const goal = await ctx.runGoalAction(ctx.sessionId, intent.action);
      if (intent.action === 'get') {
        ctx.addMessage(ctx.sessionId, commandResultMessage(ctx.inputLine, goalStatusOutput(goal)));
      }
    } catch {
      ctx.addMessage(
        ctx.sessionId,
        commandResultMessage(ctx.inputLine, '目标命令执行失败，请稍后再试。'),
      );
    }
  },
};

/** /persist —— 在欢迎页创建 Persist Session，具体创建仍复用 App.tsx 现有入口。 */
const persistCommand: SlashCommand = {
  name: 'persist',
  requiresSession: false,
  execute: async (ctx, args) => {
    if (ctx.sessionId !== NEW_CONVERSATION_ID) {
      ctx.addMessage(
        ctx.sessionId,
        commandResultMessage(
          ctx.inputLine,
          'Persist Session 只能在创建新会话时开启，并且创建后不可更改。请点击“新建任务”后再使用 /persist <任务>。',
        ),
      );
      return;
    }
    if (!args.trim()) {
      ctx.addMessage(
        ctx.sessionId,
        commandResultMessage(ctx.inputLine, '用法：/persist <任务>'),
      );
      return;
    }
    ctx.submitMessage?.(ctx.inputLine);
  },
};

export const SLASH_COMMANDS: SlashCommand[] = [
  forkCommand,
  compactCommand,
  planCommand,
  goalCommand,
  persistCommand,
];
