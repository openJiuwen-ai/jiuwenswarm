/**
 * 成员离队口径（前端唯一出口）。
 *
 * - 离队（shut_down）：永久离开名册。快照对账、@ 候选都不能把它带回来，否则用户能
 *   @ 到一个后端无法唤醒的成员；但它做过的任务还在，名字 / 头像要从离队目录里查。
 * - 退役中（shutdown_requested）：还在名册上（最后可能落到 dormant），但已不接新消息，
 *   不能出现在 @ 候选里。
 * - 休眠（dormant）不算离队——它仍在名册上，被点名即可唤醒。
 */

interface RosterMember {
  member_id: string;
  status?: string;
}

function normalizedStatus(status: unknown): string {
  return typeof status === 'string' ? status.trim().toLowerCase() : '';
}

export function isDepartedTeamMemberStatus(status: unknown): boolean {
  return normalizedStatus(status) === 'shut_down';
}

export function isRetiringTeamMemberStatus(status: unknown): boolean {
  return normalizedStatus(status) === 'shutdown_requested';
}

/** 还能接到消息（可作为 @ 候选、算作活跃成员）：离队和退役中的成员都不行。 */
export function isReachableTeamMemberStatus(status: unknown): boolean {
  return !isDepartedTeamMemberStatus(status) && !isRetiringTeamMemberStatus(status);
}

/**
 * 用 team.snapshot 对账名册。
 *
 * - snapshotMembers 是快照里的在册成员；旧后端还会把离队成员混在里面，这里一并剔除。
 * - departedIds 是快照显式列出的离队成员：断线期间漏掉的 shutdown 事件靠它收敛。
 * - changedIds 是对账请求期间收到过实时事件的成员，以实时状态为准，快照不覆盖。
 *
 * 返回 null 表示快照里没有任何可对账的成员，调用方应保持现状。
 */
export function reconcileSnapshotRoster<T extends RosterMember>(
  snapshotMembers: T[],
  departedIds: ReadonlySet<string>,
  currentMembers: T[],
  changedIds: ReadonlySet<string>,
): { members: T[]; reconciledMemberIds: string[] } | null {
  const reconciledMemberIds = [
    ...snapshotMembers.map((member) => member.member_id),
    ...departedIds,
  ].filter((memberId) => !changedIds.has(memberId));
  if (reconciledMemberIds.length === 0) return null;

  const snapshotMemberIds = new Set(snapshotMembers.map((member) => member.member_id));
  const members = snapshotMembers.flatMap((member) => {
    if (!changedIds.has(member.member_id)) {
      return isDepartedTeamMemberStatus(member.status) ? [] : [member];
    }
    const currentMember = currentMembers.find((candidate) => candidate.member_id === member.member_id);
    return currentMember ? [currentMember] : [];
  });
  for (const member of currentMembers) {
    if (changedIds.has(member.member_id) && !snapshotMemberIds.has(member.member_id)) {
      members.push(member);
    }
  }
  return { members, reconciledMemberIds: [...new Set(reconciledMemberIds)] };
}
