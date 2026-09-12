/** Team 会话的 Web 输入框不提供内置斜杠指令；单 Agent 保留原有能力。 */
export function supportsWebSlashCommands(mode: string): boolean {
  return mode !== 'team';
}

/** 统一给快捷面板做模式过滤，避免 Team 会话泄露可点击的命令入口。 */
export function getWebSlashCommandsForMode<T>(commands: T[], mode: string): T[] {
  return supportsWebSlashCommands(mode) ? commands : [];
}

type GoalWithStatus = { status: string };

/** Plan 与 Goal 的共同互斥判定：只有已完成目标不阻止进入 Plan。 */
export function hasUnfinishedGoal(goal: GoalWithStatus | null | undefined): boolean {
  return goal != null && goal.status !== 'completed';
}

export type PlanGoalInterlockDecision = 'allow' | 'clear_goal_armed' | 'block';

/**
 * 进入 Plan 前处理 Goal：真实未完成目标必须阻止；仅选中但未提交的 Goal 开关可被 Plan 顶掉。
 */
export function resolvePlanGoalInterlock(
  goal: GoalWithStatus | null | undefined,
  goalArmed: boolean,
): PlanGoalInterlockDecision {
  if (hasUnfinishedGoal(goal)) return 'block';
  return goalArmed ? 'clear_goal_armed' : 'allow';
}

/** 未完成 Goal 存在时，指令选择器里的 `/plan` 必须呈禁用态。 */
export function isSlashCommandDisabledByGoal(name: string, unfinishedGoal: boolean): boolean {
  return unfinishedGoal && name.toLowerCase() === 'plan';
}

/**
 * `/plan` 是输入面板上的即时开关。只有独立的 `/plan` 才是命令；
 * 带有其他文本时（如 `/plan hi`）应保留原文并按普通消息发送。
 * Team 模式下所有注册命令都不由 Web 前端拦截执行。
 *
 * 调用方已先确认 name 存在于命令注册表中。
 */
export function shouldExecuteRegisteredSlashCommand(name: string, args: string, mode: string): boolean {
  if (!supportsWebSlashCommands(mode)) return false;
  return name.toLowerCase() !== 'plan' || args.trim().length === 0;
}
