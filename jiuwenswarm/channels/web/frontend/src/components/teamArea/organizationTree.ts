export interface OrganizationTask {
  task_id?: string;
  parent_task_id?: string | null;
  root_task_id?: string | null;
  title?: string | null;
  description?: string | null;
  status?: string | null;
  task_type?: string | null;
  assignment?: Record<string, unknown> | null;
  required_capabilities?: string[];
  updated_at?: number | string | null;
  aggregation_mode?: string | null;
  failure_code?: string | null;
  recreated_from_task_id?: string | null;
}

export interface OrganizationTaskNode {
  key: string;
  task: OrganizationTask;
  children: OrganizationTaskNode[];
  completed: number;
  total: number;
}

/** Organization members are children of the Owner, including the system Summary Team. */
export function buildOrganizationTeamTree<T extends { team_id?: string | null; is_owner?: boolean }>(
  teams: T[], ownerTeamId?: string | null,
): { key: string; team: T; children: { key: string; team: T }[] }[] {
  const nodes = teams.map((team, index) => ({
    key: team.team_id || `missing-team-${index}`,
    team,
    children: [] as { key: string; team: T }[],
  }));
  const owner = nodes.find((node) => ownerTeamId ? node.team.team_id === ownerTeamId : node.team.is_owner);
  if (!owner) return nodes;
  owner.children = nodes.filter((node) => node !== owner);
  return [owner];
}

/** Preserve server order and every task, including missing parents and malformed cycles. */
export function buildOrganizationTaskTree(tasks: OrganizationTask[]): OrganizationTaskNode[] {
  // Summary Tasks bind to a root without a persisted parent; nest them for display only.
  const parentId = (task: OrganizationTask) => task.parent_task_id?.trim() ||
    (task.task_type === 'organization.summary' ? task.root_task_id?.trim() : '') || '';
  const nodes = tasks.map((task, index) => ({
    key: task.task_id || `missing-id-${index}`,
    task,
    children: [] as OrganizationTaskNode[],
    completed: 0,
    total: 0,
  }));
  const byId = new Map(nodes.filter((node) => node.task.task_id).map((node) => [node.task.task_id!, node]));
  const roots: OrganizationTaskNode[] = [];
  for (const node of nodes) {
    let parent = byId.get(parentId(node.task));
    const visited = new Set([node]);
    let cursor = parent;
    while (cursor) {
      if (visited.has(cursor)) {
        parent = undefined;
        break;
      }
      visited.add(cursor);
      cursor = byId.get(parentId(cursor.task));
    }
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  const count = (node: OrganizationTaskNode) => {
    node.total = 1;
    node.completed = node.task.status?.toUpperCase() === 'COMPLETED' ? 1 : 0;
    for (const child of node.children) {
      count(child);
      node.total += child.total;
      node.completed += child.completed;
    }
  };
  roots.forEach(count);
  return roots;
}
