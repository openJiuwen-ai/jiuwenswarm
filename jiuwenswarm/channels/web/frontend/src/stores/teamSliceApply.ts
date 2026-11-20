/**
 * 将 team.snapshot 写入对应团队的会话投影。
 * 主团队保留 sessionId 命名空间；专家使用独立的 conversationKey，避免覆盖主团队状态。
 * 输入形状与服务端 members/tasks/team_id 保持一致。
 */

import { isEnterprise } from '../edition';
import { useSessionStore } from './sessionStore';
import { conversationKey } from './chatStore';
import { useTeamSelectorStore } from './teamSelectorStore';
import type { TeamTask, TeamTaskStatus } from './sessionStore';
import type { TeamSnapshotPayload } from './teamSelectorStore';
import { resolveOrganizationTeamIdentity } from '../features/teamOrganization/teamIdentity';
import { isDepartedTeamMemberStatus } from '../features/teamMemberDeparture';

const TEAM_TASK_STATUSES = new Set<TeamTaskStatus>([
  'pending',
  'blocked',
  'planning',
  'in_progress',
  'in_review',
  'completed',
  'cancelled',
]);

/**
 * 快照时间戳可能是秒 / 毫秒 / ISO 字符串，统一成毫秒。
 * 后端部分表存的是秒级时间戳，直接当毫秒用会让所有卡片显示在 1970 年。
 */
function normalizeTimestamp(value: unknown, fallback: number): number {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value < 10_000_000_000 ? Math.round(value * 1000) : Math.round(value);
  }
  if (typeof value === 'string' && value.trim()) {
    const parsed = Date.parse(value);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return fallback;
}

function str(value: unknown): string {
  if (typeof value === 'string') return value.trim();
  return value == null ? '' : String(value).trim();
}

function normalizeTaskStatus(value: unknown): TeamTaskStatus {
  const status = str(value).toLowerCase() as TeamTaskStatus;
  return TEAM_TASK_STATUSES.has(status) ? status : 'pending';
}

export function applyTeamSnapshotToSession(
  sessionId: string,
  teamId: string,
  snapshot: TeamSnapshotPayload | null | undefined
): void {
  if (!sessionId) return;
  const team = useTeamSelectorStore.getState().runtimes[sessionId]?.teams.find((item) => item.team_id === teamId);
  sessionId = conversationKey(sessionId, team?.is_owner ? null : teamId);
  const store = useSessionStore.getState();
  store.ensureRuntime(sessionId);
  if (!team?.is_owner) {
    store.setMode(sessionId, 'team');
    const identity = resolveOrganizationTeamIdentity(team, []);
    if (identity) store.setTeamGroupIdentity(sessionId, identity);
  }

  const now = Date.now();
  const rawMembers = Array.isArray(snapshot?.members) ? snapshot.members : [];
  const rawDepartedMembers = Array.isArray(snapshot?.departed_members) ? snapshot.departed_members : [];
  const rawTasks = Array.isArray(snapshot?.tasks) ? snapshot.tasks : [];

  // 旧后端把离队成员混在 members 里，按状态再拆一次。
  const allMembers = [...rawMembers, ...rawDepartedMembers]
    .filter((member) => str(member?.member_id))
    .map((member, index) => ({
      id: `snapshot-${teamId}-${member.member_id}-${index}`,
      member_id: str(member.member_id),
      status: str(member.status),
      timestamp: now,
      name: member.name ?? undefined,
      execution_status: member.execution_status ?? null,
      mode: member.mode,
      role: member.role,
      cli_agent: member.cli_agent ?? null,
      workflow_run_id: member.workflow_run_id,
      agent_template: member.agent_template ?? null,
      display_name: member.display_name ?? null,
    }));
  const members = allMembers.filter((member) => !isDepartedTeamMemberStatus(member.status));
  const departedMembers = allMembers.filter((member) => isDepartedTeamMemberStatus(member.status));

  // 快照项不带 team_name 时（老后端）按当前选中的 team 归属，
  // 这样切换后的过滤/展示仍然一致。
  const tasks: TeamTask[] = rawTasks
    .filter((task) => str(task?.task_id))
    .map((task) => ({
      task_id: str(task.task_id),
      title: task.title,
      content: task.content,
      status: normalizeTaskStatus(task.status),
      assignee: task.assignee ?? undefined,
      team_id: str(task.team_name) || teamId,
      timestamp: normalizeTimestamp(task.updated_at, now),
    }));

  // teamMembers / teamTasks 是当前所选 Team 的 session 级投影，因此快照必须完整
  // 替换（包括空快照），不能与上一个 Team 的内容合并。
  // 例外是镜像出来的 swarmflow worker（带 workflow_run_id）：它们只存在于前端、
  // 不进 team.db 名册，快照缺席不代表已被移除，原样保留（规则同
  // reconcileSnapshotRoster）；同一 member_id 两边都有时以快照为准，离队清理不丢。
  // 保留无需按 teamId 过滤：函数入口已将 sessionId 重定向为 conversationKey
  // （owner → 裸 sessionId，专家 team → sessionId##teamId），且镜像 worker 事件的
  // team_id 是 workflow 名、不会被 expertTeamId 认领——镜像 worker 只存在于 owner
  // 主投影，各专家投影互不相通，切换 Team 不会读到上一个 Team 的镜像成员。
  const currentStore = useSessionStore.getState();
  // In-service plus departed is the full set the snapshot already knows. A
  // mirrored worker can sit in one projection locally while the snapshot
  // reports it on the other side; both mirrors must exclude by the union or
  // the same member_id survives in both projections.
  const snapshotAllIds = new Set(
    [...members, ...departedMembers].map((member) => member.member_id)
  );
  const mirroredMembers = (currentStore.getRuntime(sessionId)?.teamMembers ?? []).filter(
    (member) => member.workflow_run_id && !snapshotAllIds.has(member.member_id)
  );
  const projectedMembers = [...mirroredMembers, ...members];
  currentStore.setTeamMembers(sessionId, projectedMembers);
  // teamDepartedMembers 同样不能全量替换：镜像 worker 离队后只活在这条投影里
  // （shutdown 事件写入，见 rememberDepartedTeamMember），快照不带它的身份记录，
  // 清掉后任务卡与 @ 提及会退化为裸 member_id。
  const mirroredDeparted = (currentStore.getRuntime(sessionId)?.teamDepartedMembers ?? []).filter(
    (member) => member.workflow_run_id && !snapshotAllIds.has(member.member_id)
  );
  currentStore.setTeamDepartedMembers(sessionId, [...mirroredDeparted, ...departedMembers]);
  if (!isEnterprise()) {
    currentStore.setTeamTasks(sessionId, tasks);
    return;
  }
  if (projectedMembers.length === 0) {
    currentStore.setTeamTasks(sessionId, []);
    return;
  }
  if (tasks.length === 0) {
    return;
  }
  const currentTasks = currentStore.getRuntime(sessionId)?.teamTasks ?? [];
  currentStore.setTeamTasks(sessionId, mergeEnterpriseTeamTasks(currentTasks, tasks));
}

export function mergeEnterpriseTeamTasks(current: TeamTask[], incoming: TeamTask[]): TeamTask[] {
  const byId = new Map(current.map((task) => [task.task_id, { ...task }]));
  for (const task of incoming) {
    const prev = byId.get(task.task_id);
    if (!prev) {
      byId.set(task.task_id, { ...task });
      continue;
    }
    const next = { ...prev };
    (Object.keys(task) as (keyof TeamTask)[]).forEach((key) => {
      const value = task[key];
      if (value !== undefined && value !== null && value !== '') {
        (next as Record<string, unknown>)[key] = value;
      }
    });
    byId.set(task.task_id, next);
  }
  return Array.from(byId.values());
}
