/**
 * Plan 模式的 wire mode 解析。
 *
 * Web UI 只保留 `agent` / `team` 两个基础模式（`AgentMode`），Plan 是一个独立的
 * 开关；profile（`work` / `code`）来自 workspaceStore 的 `workMode`，对应
 * Web 项目分桶键。发送请求时把三者组合成后端认识的三段命名 mode 字符串：
 *
 * ```text
 * agent + plan off              -> "agent"
 * agent + plan on + work        -> "agent.work.plan"（work profile + plan 段）
 * agent + plan on + code        -> "agent.code.plan"（code profile + plan 段）
 * team  + plan off              -> "team"
 * team  + plan on + work        -> "team.work.plan"（work profile + plan 段）
 * team  + plan on + code        -> "team.code.plan"（code profile + plan 段）
 * ```
 *
 * 新三段命名 `{base}.{profile}.plan` 直接携带 profile + plan 段，不再依赖请求里的
 * `work_mode` 字段做 mode 解析（但 `work_mode` 仍由 `getSessionWorkContext` 写入
 * session/project 元数据作项目分桶键，后端 `resolve_request_mode` 优先识别新串）。
 *
 * D1 修复前 `resolvePlanWireMode` 硬编码 `'agent.work.plan'`，导致 Web UI 永远
 * 产不出 `agent.code.plan`——code profile 下用户打开 Plan，后端仍按 work
 * profile 解析。现在 profile 由调用方传入，agent / team 两条路径都能产出。
 */

/** UI 层的基础模式。agent / team 支持 Plan；auto / auto_harness 仅透传。 */
export type PlanBaseMode = 'agent' | 'team' | 'auto' | 'auto_harness';

/** Web profile 取值：`work` 走普通 Deep 通道，`code` 走 CodeAdapter。 */
export type PlanWorkProfile = 'work' | 'code';

/**
 * Plan 对单 agent 与集群均开放；Auto / auto_harness 不提供 Plan 入口。
 *
 * MACRO 的调度 lane 仅为 agent / team；Auto 是每次请求重新分类的用户选择，
 * 不参与 Plan mode 的 wire 组合。集群 Plan 由 Leader 先产出计划、经用户审批再执行；
 * profile（Deep / Code）映射到
 * `team.work.plan` / `team.code.plan`。
 */
export function supportsPlanMode(mode: PlanBaseMode | string | undefined): boolean {
  return mode === 'agent' || mode === 'team';
}

/**
 * 判断 wire mode 是否属于集群（`team.*` 或归一前的旧 `team` / `team.code` /
 * `code.team`）。前后端归一约定：所有 `team.*` 串都会被 `normalizeAgentMode`
 * 折叠成 UI 层的 `'team'`，本 helper 用于在归一前对原始 mode 做团队判定
 * （如 `App.tsx` 的 session 切换、`useWebSocket.ts` 的 final 折叠）。
 */
export function isTeamAgentMode(mode: string | undefined | null): boolean {
  if (typeof mode !== 'string' || !mode) return false;
  const normalized = mode.trim().toLowerCase();
  if (normalized === 'team' || normalized === 'team.code' || normalized === 'code.team') {
    return true;
  }
  return normalized.startsWith('team.');
}

/**
 * 组合出发送给后端的 mode。
 *
 * @param baseMode UI 当前的基础模式。
 * @param planActive 该会话的 Plan 开关是否打开。
 * @param profile 当前 workspace 的 work/code profile（默认 `work`）。仅当
 *                `planActive` 为真时使用，决定三段命名的中间段：
 *                `work` -> `{base}.work.plan`，`code` -> `{base}.code.plan`。
 * @returns 后端认识的 wire mode。
 */
export function resolvePlanWireMode(
  baseMode: PlanBaseMode | string | undefined,
  planActive: boolean,
  profile: PlanWorkProfile | string | null | undefined = 'work',
): string {
  const base = typeof baseMode === 'string' && baseMode ? baseMode : 'agent';
  if (!planActive || !supportsPlanMode(base)) return base;
  const env = profile === 'code' ? 'code' : 'work';
  return `${base}.${env}.plan`;
}

/** wire mode 是否处于 Plan（识别 `agent.work.plan` / `agent.code.plan` 与
 * 集群 `team.work.plan` / `team.code.plan`）。 */
export function isPlanWireMode(wireMode: string | undefined): boolean {
  return (
    wireMode === 'agent.work.plan' ||
    wireMode === 'agent.code.plan' ||
    wireMode === 'team.work.plan' ||
    wireMode === 'team.code.plan'
  );
}

/** 去掉 Plan 后缀，得到基础模式。 */
export function stripPlanSuffix(wireMode: string | undefined): string {
  if (wireMode === 'agent.work.plan' || wireMode === 'agent.code.plan') return 'agent';
  if (wireMode === 'team.work.plan' || wireMode === 'team.code.plan') return 'team';
  return typeof wireMode === 'string' && wireMode ? wireMode : 'agent';
}
