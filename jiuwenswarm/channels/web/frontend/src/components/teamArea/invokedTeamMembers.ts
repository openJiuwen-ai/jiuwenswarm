export type InvokableTeamMember = {
  member_id: string;
};

export type TeamInvocationTask = {
  assignee?: string;
  timestamp?: number | string;
};

export type TeamInvocationEvent = {
  member_id?: string;
  assignee?: string;
  timestamp?: number | string;
};

export type TurnBoundaryMessage = {
  role: string;
  timestamp?: number | string;
};

function normalizedMemberId(value: string | undefined): string {
  return typeof value === 'string' ? value.trim() : '';
}

function isLeaderMemberId(memberId: string, leaderMemberIds: readonly string[]): boolean {
  const normalized = memberId.toLowerCase();
  return leaderMemberIds.includes(memberId) || normalized === 'team_leader' || normalized === 'leader';
}

export function timestampToEpochMs(value: number | string | undefined): number | null {
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null;
    return value > 0 && value < 1_000_000_000_000 ? value * 1_000 : value;
  }
  if (typeof value !== 'string' || !value.trim()) return null;
  const numeric = Number(value);
  if (Number.isFinite(numeric)) {
    return numeric > 0 && numeric < 1_000_000_000_000 ? numeric * 1_000 : numeric;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function latestUserTurnStartedAtMs(messages: readonly TurnBoundaryMessage[]): number | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role.trim().toLowerCase() !== 'user') continue;
    const timestamp = timestampToEpochMs(message.timestamp);
    if (timestamp !== null) return timestamp;
  }
  return null;
}

export function isCurrentTurnEvidence(value: { timestamp?: number | string }, currentTurnStartedAtMs: number | null): boolean {
  if (currentTurnStartedAtMs === null) return false;
  const timestamp = timestampToEpochMs(value.timestamp);
  return timestamp !== null && timestamp >= currentTurnStartedAtMs;
}

/**
 * A manager-routed team exposes only the leader plus members backed by dispatch
 * evidence from the latest user turn. Session-scoped stores intentionally keep
 * history, so evidence older than the current turn must never leak into the
 * next query. Before dispatch starts we show only the leader rather than imply
 * that the complete roster has already been called.
 */
export function filterInvokedTeamMembers<T extends InvokableTeamMember>(
  members: readonly T[],
  tasks: readonly TeamInvocationTask[],
  taskEvents: readonly TeamInvocationEvent[],
  executionEvents: readonly TeamInvocationEvent[],
  leaderMemberIds: readonly string[] = [],
  currentTurnStartedAtMs: number | null = null,
): T[] {
  const invokedIds = new Set<string>();
  tasks
    .filter(task => isCurrentTurnEvidence(task, currentTurnStartedAtMs))
    .forEach(task => {
      const assignee = normalizedMemberId(task.assignee);
      if (assignee) invokedIds.add(assignee);
    });
  [...taskEvents, ...executionEvents]
    .filter(event => isCurrentTurnEvidence(event, currentTurnStartedAtMs))
    .forEach(event => {
      const memberId = normalizedMemberId(event.assignee) || normalizedMemberId(event.member_id);
      if (memberId) invokedIds.add(memberId);
    });

  return members.filter(member => invokedIds.has(member.member_id) || isLeaderMemberId(member.member_id, leaderMemberIds));
}
