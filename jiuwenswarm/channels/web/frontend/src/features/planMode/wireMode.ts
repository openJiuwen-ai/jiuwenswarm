/**
 * Plan 模式的 wire mode 解析。
 *
 * Web UI 只保留 `agent` / `team` 两个基础模式（`AgentMode`），Plan 是一个独立的
 * 开关。发送请求时才把两者组合成后端认识的 mode 字符串：
 *
 * ```text
 * agent + plan off -> "agent"
 * agent + plan on  -> "agent.plan"
 * team             -> "team"（集群不支持 Plan）
 * ```
 *
 * `work` / `code` 由请求里的 `work_mode` 单独表达，后端据此决定使用 Deep 还是
 * Code profile。前端不需要拼 `code.normal` / `code.plan` / `code.team`。
 */

/** UI 层的基础模式。只有单 agent 支持 Plan。 */
export type PlanBaseMode = 'agent' | 'team' | 'auto_harness';

/**
 * Plan 只对单 agent 开放。
 *
 * 集群（`team`）不支持 Plan：集群的计划由 Leader 在团队运行时里自行编排，
 * 没有独立的计划审批流程，所以工具栏不提供 Plan 入口。
 */
export function supportsPlanMode(mode: PlanBaseMode | string | undefined): boolean {
  return mode === 'agent';
}

/**
 * 组合出发送给后端的 mode。
 *
 * @param baseMode UI 当前的基础模式。
 * @param planActive 该会话的 Plan 开关是否打开。
 * @returns 后端认识的 wire mode。
 */
export function resolvePlanWireMode(
  baseMode: PlanBaseMode | string | undefined,
  planActive: boolean
): string {
  const base = typeof baseMode === 'string' && baseMode ? baseMode : 'agent';
  if (!planActive || !supportsPlanMode(base)) return base;
  return `${base}.plan`;
}

/** wire mode 是否处于 Plan。 */
export function isPlanWireMode(wireMode: string | undefined): boolean {
  return wireMode === 'agent.plan';
}

/** 去掉 Plan 后缀，得到基础模式。 */
export function stripPlanSuffix(wireMode: string | undefined): string {
  if (wireMode === 'agent.plan') return 'agent';
  return typeof wireMode === 'string' && wireMode ? wireMode : 'agent';
}
