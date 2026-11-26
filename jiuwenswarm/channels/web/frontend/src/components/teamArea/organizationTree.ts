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
  inlineTasks: OrganizationTaskNode[];
  completed: number;
  total: number;
}

export function resolveOrganizationRoster<T extends {
  team_id?: string | null;
  is_owner?: boolean;
  agent_group_name?: string | null;
  organization_id?: string | null;
}>(teams: T[], ownerTeamId?: string | null) {
  const isOwner = (team: T) => ownerTeamId ? team.team_id === ownerTeamId : team.is_owner;
  const ordered = [...teams].sort((a, b) => Number(Boolean(isOwner(b))) - Number(Boolean(isOwner(a))));
  const teamIds = new Map<string, string>();
  const teamByPackage = new Map<string, string>();
  const visibleTeams = ordered.filter(({ team_id, agent_group_name, organization_id }) => {
    if (!team_id?.trim() || teamIds.has(team_id)) return false;
    const packageId = agent_group_name?.trim();
    const packageKey = packageId ? JSON.stringify([organization_id ?? '', packageId]) : undefined;
    const existing = packageKey ? teamByPackage.get(packageKey) : undefined;
    teamIds.set(team_id, existing ?? team_id);
    if (existing) return false;
    if (packageKey) teamByPackage.set(packageKey, team_id);
    return true;
  });
  return { visibleTeams, teamIds };
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
export function buildOrganizationTaskTree(
  tasks: OrganizationTask[], teamIds?: ReadonlyMap<string, string>,
): OrganizationTaskNode[] {
  // Summary Tasks bind to a root without a persisted parent; nest them for display only.
  const parentId = (task: OrganizationTask) => task.parent_task_id?.trim() ||
    (task.task_type === 'organization.summary' ? task.root_task_id?.trim() : '') || '';
  const nodes = tasks.map((task, index) => ({
    key: task.task_id || `missing-id-${index}`,
    task,
    children: [] as OrganizationTaskNode[],
    inlineTasks: [] as OrganizationTaskNode[],
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
  // Keep the first visible node for each Team. Fold a later task together with
  // its subtree, so its children never appear under a different parent task.
  const groupByTeam = (root: OrganizationTaskNode) => {
    const anchors = new Map<string, OrganizationTaskNode>();
    const visit = (node: OrganizationTaskNode, parent?: OrganizationTaskNode) => {
      const teamId = node.task.assignment?.team_id;
      const key = typeof teamId === 'string' && teamId ? teamIds?.get(teamId) ?? teamId : '';
      const anchor = key ? anchors.get(key) : undefined;
      if (anchor) {
        anchor.inlineTasks.push(node);
        return;
      }
      const children = node.children;
      node.children = [];
      if (key) anchors.set(key, node);
      parent?.children.push(node);
      children.forEach((child) => visit(child, node));
    };
    visit(root);
  };
  const isComplete = (task: OrganizationTask) => task.status?.toUpperCase() === 'COMPLETED';
  const count = (node: OrganizationTaskNode) => {
    node.total = 1;
    node.completed = Number(isComplete(node.task));
    for (const child of [...node.children, ...node.inlineTasks]) {
      count(child);
      node.total += child.total;
      node.completed += child.completed;
    }
  };
  if (teamIds) roots.forEach(groupByTeam);
  roots.forEach(count);
  return roots;
}
