export type InvokableTeamMember = {
  member_id: string;
};

export type TeamInvocationTask = {
  assignee?: string;
};

export type TeamInvocationEvent = {
  member_id?: string;
  assignee?: string;
};

function normalizedMemberId(value: string | undefined): string {
  return typeof value === 'string' ? value.trim() : '';
}

function isLeaderMemberId(memberId: string, leaderMemberIds: readonly string[]): boolean {
  const normalized = memberId.toLowerCase();
  return leaderMemberIds.includes(memberId) || normalized === 'team_leader' || normalized === 'leader';
}

/**
 * Once runtime dispatch evidence exists, a manager-routed team should expose
 * only the members selected for this query. Before the first event arrives we
 * retain the roster so legacy team sessions do not flash an empty panel.
 */
export function filterInvokedTeamMembers<T extends InvokableTeamMember>(
  members: readonly T[],
  tasks: readonly TeamInvocationTask[],
  taskEvents: readonly TeamInvocationEvent[],
  executionEvents: readonly TeamInvocationEvent[],
  leaderMemberIds: readonly string[] = [],
): T[] {
  const invokedIds = new Set<string>();
  tasks.forEach(task => {
    const assignee = normalizedMemberId(task.assignee);
    if (assignee) invokedIds.add(assignee);
  });
  [...taskEvents, ...executionEvents].forEach(event => {
    const memberId = normalizedMemberId(event.assignee) || normalizedMemberId(event.member_id);
    if (memberId) invokedIds.add(memberId);
  });

  if (invokedIds.size === 0) return [...members];
  return members.filter(member => invokedIds.has(member.member_id) || isLeaderMemberId(member.member_id, leaderMemberIds));
}
