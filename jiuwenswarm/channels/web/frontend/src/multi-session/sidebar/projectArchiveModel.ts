import type { ProjectInfo, Session } from '../../types';

export function resolveProjectArchiveSessionCount(
  project: ProjectInfo,
  projectSessionTotals: Record<string, number>,
  pinnedSessions: Session[],
): number | null {
  const nonPinnedCount = projectSessionTotals[project.project_id] ?? project.session_count;
  if (typeof nonPinnedCount !== 'number') return null;

  // 项目统计不包含置顶会话，但项目级归档会归档它们。
  const pinnedOrdinaryCount = pinnedSessions.filter((session) => {
    const belongsToProject = session.project_id === project.project_id;
    const isCronSession = Boolean(session.cron_id)
      || session.session_id.startsWith('cron_')
      || session.session_id.startsWith('heartbeat_');
    return belongsToProject && !isCronSession;
  }).length;

  return nonPinnedCount + pinnedOrdinaryCount;
}
