export interface TeamConnectionPresentation {
  memberIds: string[];
}

const STORAGE_PREFIX = 'jiuwenclaw_team_connection_presentation:';

export function loadTeamConnectionPresentation(sessionId: string): TeamConnectionPresentation | null {
  if (typeof sessionStorage === 'undefined') return null;
  try {
    const memberIds: unknown = JSON.parse(sessionStorage.getItem(`${STORAGE_PREFIX}${sessionId}`) || 'null');
    return Array.isArray(memberIds) && memberIds.every((id) => typeof id === 'string') && memberIds.length > 0
      ? { memberIds }
      : null;
  } catch {
    return null;
  }
}

export function saveTeamConnectionPresentation(
  sessionId: string,
  presentation: TeamConnectionPresentation | null,
): void {
  if (typeof sessionStorage === 'undefined') return;
  try {
    const key = `${STORAGE_PREFIX}${sessionId}`;
    if (presentation?.memberIds.length) {
      sessionStorage.setItem(key, JSON.stringify(presentation.memberIds));
    } else {
      sessionStorage.removeItem(key);
    }
  } catch {
    // Storage may be unavailable; the in-memory presentation still works.
  }
}

interface TeamMemberStatus {
  member_id: string;
  status: string;
}

const RUNNING_TASK_STATUSES = new Set(['planning', 'in_progress', 'in_review']);

export function isRunningTeamMemberStatus(status: string): boolean {
  const normalized = status.toLowerCase();
  return (
    normalized.includes('execut') ||
    normalized.includes('running') ||
    normalized.includes('busy') ||
    normalized.includes('working')
  );
}

export function isRunningTeamTaskStatus(status: string): boolean {
  return RUNNING_TASK_STATUSES.has(status);
}

export function captureTeamConnectionPresentation(
  mode: string | undefined,
  members: readonly TeamMemberStatus[],
): TeamConnectionPresentation | null {
  if (mode !== 'team') return null;

  const memberIds = members
    .filter((member) => isRunningTeamMemberStatus(member.status))
    .map((member) => member.member_id);
  return memberIds.length > 0 ? { memberIds } : null;
}

export function retainRunningTeamConnectionPresentation(
  captured: TeamConnectionPresentation | null,
  members: readonly TeamMemberStatus[],
): TeamConnectionPresentation | null {
  if (!captured) return null;

  const current = captureTeamConnectionPresentation('team', members);
  if (!current) return null;
  const runningMemberIds = new Set(current.memberIds);
  const memberIds = captured.memberIds.filter((id) => runningMemberIds.has(id));

  return memberIds.length > 0 ? { memberIds } : null;
}

export function shouldPresentTeamMemberIdle(
  memberId: string,
  status: string,
  presentation: TeamConnectionPresentation | null | undefined,
): boolean {
  return Boolean(presentation?.memberIds.includes(memberId) && isRunningTeamMemberStatus(status));
}
