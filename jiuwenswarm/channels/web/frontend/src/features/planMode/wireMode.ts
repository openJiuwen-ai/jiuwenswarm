/**
 * Plan 模式的 wire mode 解析。
 *
 * Web UI 只保留 `agent` / `team` 两个基础模式（`AgentMode`），Plan 是一个独立的
 * 开关。发送请求时才把两者组合成后端认识的 mode 字符串：
 *
 * ```text
 * agent + plan off -> "agent"
 * agent + plan on  -> "agent.plan"
 * team  + plan off -> "team"
 * team  + plan on  + work_mode=work  -> "team.plan.normal"（Deep profile）
 * team  + plan on  + work_mode=code  -> "team.plan.code"  （Code profile）
 * ```
 *
 * `work` / `code` 由请求里的 `work_mode` 单独表达，后端据此决定使用 Deep 还是
 * Code profile。前端不需要拼 `code.normal` / `code.plan` / `code.team`。
 */

/** UI 层的基础模式。agent 与 team 都支持 Plan。 */
export type PlanBaseMode = 'agent' | 'team' | 'auto_harness';

/**
 * Plan 对单 agent 与集群均开放。
 *
 * 集群 Plan 由 Leader 先产出计划、经用户审批再执行，和单 agent 的 Plan 流程
 * 看齐；profile（Deep / Code）由 work_mode 决定，映射到
 * `team.plan.normal` / `team.plan.code`。
 */
export function supportsPlanMode(mode: PlanBaseMode | string | undefined): boolean {
  return mode === 'agent' || mode === 'team';
}

/**
 * 组合出发送给后端的 mode。
 *
 * @param baseMode UI 当前的基础模式。
 * @param planActive 该会话的 Plan 开关是否打开。
 * @param workMode 该会话 / 项目的 work_mode（`work` / `code`）。仅 team + plan
 *   时用来区分 `team.plan.normal` / `team.plan.code`；单 agent 的 profile 由
 *   后端按请求里的 `work_mode` 决定，这里不拼 `code.plan`。
 * @returns 后端认识的 wire mode。
 */
export function resolvePlanWireMode(
  baseMode: PlanBaseMode | string | undefined,
  planActive: boolean,
  workMode?: string | null
): string {
  const base = typeof baseMode === 'string' && baseMode ? baseMode : 'agent';
  if (!planActive || !supportsPlanMode(base)) return base;
  if (base === 'team') {
    return workMode === 'code' ? 'team.plan.code' : 'team.plan.normal';
  }
  return 'agent.plan';
}

/** wire mode 是否处于 Plan。 */
export function isPlanWireMode(wireMode: string | undefined): boolean {
  return (
    wireMode === 'agent.plan' ||
    wireMode === 'team.plan.normal' ||
    wireMode === 'team.plan.code' ||
    // 后端 legacy 归一落盘的 canonical 形式，与 stripPlanSuffix 保持一致。
    wireMode === 'team.plan'
  );
}

/** 去掉 Plan 后缀，得到基础模式。 */
export function stripPlanSuffix(wireMode: string | undefined): string {
  if (wireMode === 'agent.plan') return 'agent';
  if (wireMode === 'team.plan.normal' || wireMode === 'team.plan.code') return 'team';
  // 后端 legacy 归一会把 team.plan.normal / team.plan.code 折叠为 canonical
  // `team.plan` 落盘（server/handlers/_shared.py），刷新 / 重进会话恢复的就是该值。
  if (wireMode === 'team.plan') return 'team';
  return typeof wireMode === 'string' && wireMode ? wireMode : 'agent';
}
