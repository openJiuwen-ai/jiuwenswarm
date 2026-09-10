/**
 * `null` 表示技能源探测失败或结果未知，不能等同于管理员确认配置了零个源。
 * 保留旧值可避免一次瞬态失败把 SwarmSkills 入口永久隐藏。
 */
export function resolveEnterpriseSourceCount(current: number | null, sources: readonly unknown[] | null): number | null {
  return sources === null ? current : sources.length;
}
