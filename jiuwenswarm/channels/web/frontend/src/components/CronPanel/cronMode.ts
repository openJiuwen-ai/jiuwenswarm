/**
 * 后端 CronJob.mode 的"团队判定"（对齐 jiuwenswarm/gateway/cron/models.py）。
 *
 * 后端团队串集合 = CRON_JOB_MODES 里 team 系 + mode_matrix.TEAM_CANONICAL_MODES：
 *   team / code.team / team.plan / team.plan.normal / team.plan.code /
 *   team.work.normal / team.work.plan / team.code.normal / team.code.plan
 * （team.plan 由后端 _CRON_JOB_MODE_ALIASES 映到 team.plan.normal，team.code 由
 *   MODE_ALIASES 映到 code.team，二者仍属团队）。
 *
 * P3 引入的新三段命名 canonical 全是 `team.` 前缀串，这里用 startsWith('team.')
 * 一次覆盖，避免逐个枚举漏掉 team.work.* / team.code.* 造成团队任务被误判成单
 * agent（M2：新建团队定时任务在 Cron 面板被误判，徽标/编辑表单预填/统计口径全错）。
 * inline 在 CronPanel 目录模块而不是 types/cron.ts，因为它只服务于本文件的归一逻辑，
 * 跟 CronTaskUI.mode 这个已经归一过的 UI 字段语义不同。
 */
export function isTeamCronModeValue(raw: string | undefined | null): boolean {
  const value = String(raw ?? '').trim().toLowerCase();
  return value === 'team' || value === 'code.team' || value.startsWith('team.');
}
