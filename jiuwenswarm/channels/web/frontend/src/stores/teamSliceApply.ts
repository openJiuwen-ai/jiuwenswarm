/**
 * 把 `team.snapshot` 的结果灌进 SessionRuntime。
 *
 * 为什么需要这一步：后端一个 session 可以同时有多个 team，而前端 SessionRuntime 只按
 * sessionId 存一份 teamMembers / teamTasks。与其把整棵 store 改成
 * `runtimes[sessionId].teams[teamId]`（会波及 ToolPanel / teamArea / 成员头像等十几个读点），
 * 不如在"切换 team"这个动作发生时就地用所选 team 的快照覆写这份数据——所有既有读点
 * 无需改动就能展示正确的 team。
 *
 * 输入形状与 `_handle_team_snapshot` 的 payload 一致（members/tasks/team_id），
 * 也因此与 features/teamHistoryPanelRestore.ts 的 reconcileTasksFromSnapshot 保持同源。
 */

import { useSessionStore } from './sessionStore';
import type { TeamTaskUpsert } from './sessionStore';
import type { TeamSnapshotPayload } from './teamSelectorStore';

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

export function applyTeamSnapshotToSession(
  sessionId: string,
  teamId: string,
  snapshot: TeamSnapshotPayload | null | undefined
): void {
  if (!sessionId) return;
  const store = useSessionStore.getState();
  if (!store.getRuntime(sessionId)) return;

  const now = Date.now();
  const rawMembers = Array.isArray(snapshot?.members) ? snapshot.members : [];
  const rawTasks = Array.isArray(snapshot?.tasks) ? snapshot.tasks : [];

  const members = rawMembers
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
    }));

  // 快照项不带 team_name 时（老后端）按当前选中的 team 归属，
  // 这样切换后的过滤/展示仍然一致。
  const tasks: TeamTaskUpsert[] = rawTasks
    .filter((task) => str(task?.task_id))
    .map((task) => ({
      task_id: str(task.task_id),
      title: task.title,
      content: task.content,
      status: (task.status ?? '') as TeamTaskUpsert['status'],
      assignee: task.assignee ?? undefined,
      team_id: str(task.team_name) || teamId,
      timestamp: normalizeTimestamp(task.updated_at, now),
    }));

  // 只有拿到实际内容才覆写：空快照（monitor 已停 / 任务表还没建）不应把当前视图清空。
  if (members.length > 0) {
    useSessionStore.getState().setTeamMembers(sessionId, members);
  }
  for (const task of tasks) {
    useSessionStore.getState().upsertTeamTask(sessionId, task);
  }
}
