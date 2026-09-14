export type TaskAssigneePresentation =
  | { kind: 'unassigned' }
  | {
      kind: 'member' | 'roster-pending';
      assignee: string;
      placeholder: string;
    };

type TeamMemberIdentity = {
  member_id: string;
};

function getAssigneePlaceholder(assignee: string): string {
  return Array.from(assignee).slice(0, 2).join('').toUpperCase();
}

/** Keep an explicit assignment visible while its roster entry is still syncing. */
export function resolveTaskAssigneePresentation(assignee: string | undefined, members: readonly TeamMemberIdentity[]): TaskAssigneePresentation {
  const normalizedAssignee = assignee?.trim();
  if (!normalizedAssignee) {
    return { kind: 'unassigned' };
  }

  return {
    kind: members.some(member => member.member_id === normalizedAssignee) ? 'member' : 'roster-pending',
    assignee: normalizedAssignee,
    placeholder: getAssigneePlaceholder(normalizedAssignee),
  };
}
