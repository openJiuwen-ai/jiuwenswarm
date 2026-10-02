/** Team 会话不提供 Web 内置斜杠指令；单 Agent 支持完整的内置指令集。 */
export function supportsWebSlashCommands(mode: string): boolean {
  return mode !== 'team';
}

/** 统一给快捷面板做模式过滤。 */
export function getWebSlashCommandsForMode<T extends { name: string }>(commands: T[], mode: string): T[] {
  return supportsWebSlashCommands(mode) ? commands : [];
}

/** 命令说明由后端维护；旧服务端或缺少当前语言时回退到原 description。 */
export function resolveSlashCommandDescription(
  command: { description: string; description_i18n?: Record<string, string> },
  language: string,
): string {
  const locale = language.trim().toLowerCase().replace(/_/g, '-');
  const descriptions = command.description_i18n;
  return descriptions?.[locale] || descriptions?.[locale.split('-')[0]] || command.description;
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
 * `/fork` 和 `/plan` 是输入面板上的即时操作。只有独立命令才执行；
 * 带有其他文本时（如 `/fork title`）应保留原文并按普通消息发送。
 *
 * 调用方已先确认 name 存在于命令注册表中。
 */
export function shouldExecuteRegisteredSlashCommand(name: string, args: string, mode: string): boolean {
  const normalizedName = name.toLowerCase();
  if (!supportsWebSlashCommands(mode)) return false;
  return !['fork', 'plan'].includes(normalizedName) || args.trim().length === 0;
}
