/**
 * 定时任务列表「项目」列展示口径（Issue #2653 / bug009）。
 *
 * 落库可能是空串（手动未选）或 default/default_code（对话在默认会话下注入）。
 * 列表统一把默认类项目当作「未选真实项目」，显示为 "-"（由调用方对 null 做 i18n）。
 */

export type CronProjectLike = {
  project_id: string;
  name: string;
  is_default?: boolean;
};

/** is_default 或 id 为 default/default_code 都算默认项目。 */
export function isDefaultLikeProject(p: CronProjectLike): boolean {
  return Boolean(p.is_default) || p.project_id === 'default' || p.project_id === 'default_code';
}

/**
 * 由 job.project_id + 项目列表解析列表展示名。
 * 返回 null 表示未绑定真实项目（UI 应渲染 "-"）。
 */
export function resolveCronJobProjectName(
  projectId: string | null | undefined,
  projects: CronProjectLike[],
): string | null {
  if (!projectId) return null;
  const project = projects.find((p) => p.project_id === projectId) ?? null;
  if (!project || isDefaultLikeProject(project)) return null;
  return project.name;
}
