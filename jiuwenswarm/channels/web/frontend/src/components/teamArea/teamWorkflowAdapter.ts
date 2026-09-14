import { isCurrentTurnEvidence, timestampToEpochMs } from './invokedTeamMembers';
import type { WorkflowAgent, WorkflowRun, WorkflowStatus } from './workflowTypes';

export type RuntimeWorkflowMember = {
  member_id: string;
  name?: string;
};

export type RuntimeWorkflowTask = {
  task_id: string;
  title?: string;
  content?: string;
  status?: string;
  assignee?: string;
  member_id?: string;
  timestamp?: number | string;
};

export type RuntimeWorkflowExecution = {
  member_id?: string;
  kind?: string;
  title?: string;
  content?: string;
  timestamp?: number | string;
};

export interface TeamWorkflowAdapterInput {
  members: readonly RuntimeWorkflowMember[];
  tasks: readonly RuntimeWorkflowTask[];
  taskEvents: readonly RuntimeWorkflowTask[];
  executionEvents: readonly RuntimeWorkflowExecution[];
  leaderMemberIds?: readonly string[];
  currentTurnStartedAtMs: number | null;
  query?: string;
  isProcessing?: boolean;
}

function normalized(value: string | undefined): string {
  return typeof value === 'string' ? value.trim() : '';
}

function isLeader(memberId: string, leaderMemberIds: readonly string[]): boolean {
  const lower = memberId.toLowerCase();
  return leaderMemberIds.includes(memberId) || lower === 'leader' || lower === 'team_leader';
}

function memberName(member: RuntimeWorkflowMember, leaderMemberIds: readonly string[]): string {
  if (isLeader(member.member_id, leaderMemberIds)) return '团队主理人';
  return normalized(member.name) || member.member_id;
}

function taskStatus(status: string | undefined): WorkflowStatus {
  switch (normalized(status).toLowerCase()) {
    case 'completed':
    case 'done':
    case 'success':
      return 'completed';
    case 'cancelled':
    case 'failed':
    case 'error':
      return 'failed';
    case 'in_progress':
    case 'in_review':
    case 'planning':
    case 'running':
      return 'running';
    default:
      return 'pending';
  }
}

function aggregateStatus(statuses: readonly WorkflowStatus[], fallback: WorkflowStatus): WorkflowStatus {
  if (statuses.includes('failed')) return 'failed';
  if (statuses.includes('running')) return 'running';
  if (statuses.length > 0 && statuses.every(status => status === 'completed')) return 'completed';
  if (statuses.includes('completed')) return 'running';
  return fallback;
}

function memberAgent(
  member: RuntimeWorkflowMember,
  memberTasks: readonly RuntimeWorkflowTask[],
  memberExecutions: readonly RuntimeWorkflowExecution[],
  leaderMemberIds: readonly string[],
  query: string,
  isProcessing: boolean,
): WorkflowAgent {
  const leader = isLeader(member.member_id, leaderMemberIds);
  const taskStatuses = memberTasks.map(task => taskStatus(task.status));
  const executionStatuses = memberExecutions.map(event => (event.kind === 'final' ? 'completed' : 'running') as WorkflowStatus);
  const status = leader
    ? aggregateStatus([...taskStatuses, ...executionStatuses], isProcessing ? 'running' : 'planned')
    : aggregateStatus([...taskStatuses, ...executionStatuses], isProcessing ? 'running' : 'pending');
  const prompt = leader
    ? query
    : memberTasks
        .map(task => normalized(task.title) || normalized(task.content))
        .filter(Boolean)
        .join('\n');
  const outcome = [...memberExecutions].reverse().find(event => event.kind === 'final' && normalized(event.content))?.content;

  const startedAtMs = timestampToEpochMs(memberTasks[0]?.timestamp);
  return {
    id: member.member_id,
    name: memberName(member, leaderMemberIds),
    status,
    kind: 'agent',
    node_type: 'agent',
    prompt: prompt || undefined,
    outcome,
    started_at: startedAtMs === null ? undefined : new Date(startedAtMs).toISOString(),
    detail_pending: false,
  };
}

/**
 * beta3 exposes team task/member events but not develop's native WorkflowRun
 * snapshots. This adapter preserves the develop UI contract without inventing
 * a fixed execution chain: only members selected in the current user turn are
 * projected under the manager's dynamic-routing phase.
 */
export function buildCurrentTurnWorkflowRuns(input: TeamWorkflowAdapterInput): WorkflowRun[] {
  const { members, tasks, taskEvents, executionEvents, leaderMemberIds = [], currentTurnStartedAtMs, query = '', isProcessing = false } = input;
  if (currentTurnStartedAtMs === null || members.length === 0) return [];

  const currentTasks = [...tasks, ...taskEvents].filter(task => isCurrentTurnEvidence(task, currentTurnStartedAtMs));
  const currentExecutions = executionEvents.filter(event => isCurrentTurnEvidence(event, currentTurnStartedAtMs));
  const agents = members.map(member => {
    const memberTasks = currentTasks.filter(task => normalized(task.assignee) === member.member_id || normalized(task.member_id) === member.member_id);
    const memberExecutions = currentExecutions.filter(event => normalized(event.member_id) === member.member_id);
    return memberAgent(member, memberTasks, memberExecutions, leaderMemberIds, query, isProcessing);
  });
  const runStatus = aggregateStatus(
    agents.filter(agent => !isLeader(agent.id, leaderMemberIds)).map(agent => agent.status),
    isProcessing ? 'running' : 'planned',
  );
  const completedAgents = agents.filter(agent => agent.status === 'completed').length;

  return [
    {
      id: `team-turn-${currentTurnStartedAtMs}`,
      name: '主理人动态协作',
      summary: query || '主理人正在按当前需求选择合适的专家成员',
      status: runStatus,
      agent_count: agents.length,
      completed_agent_count: completedAgents,
      started_at: new Date(currentTurnStartedAtMs).toISOString(),
      recovered: true,
      phases: [
        {
          id: `routing-${currentTurnStartedAtMs}`,
          name: '按需选择与执行',
          description: '主理人根据当前 Query 动态选择一个或多个专家，不要求全员执行。',
          status: runStatus,
          agent_count: agents.length,
          completed_agent_count: completedAgents,
          agents,
          detail_pending: false,
        },
      ],
    },
  ];
}
