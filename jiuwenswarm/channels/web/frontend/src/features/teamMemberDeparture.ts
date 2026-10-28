/**
 * 成员是否已永久离队（终态 shut_down）。
 *
 * 离队成员只该从名册里消失：快照对账、@ 候选都不能把它带回来，否则用户能 @ 到
 * 一个后端无法唤醒的成员。休眠（dormant）不算离队——它仍在名册上，被点名即可唤醒。
 */
export function isDepartedTeamMemberStatus(status: unknown): boolean {
  return typeof status === 'string' && status.trim().toLowerCase() === 'shut_down';
}
