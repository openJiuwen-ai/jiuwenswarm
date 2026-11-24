// Test stub for src/services/teamApi: type aliases mirror the real module's exported types
// (erased at build time) and the functions are backed by the mutable teamApiStubState.
export type TeamStatus = 'creating' | 'running' | 'paused' | 'stopping' | 'stopped' | 'deleted';
export type TeamQuotaSnapshot = Record<string, unknown>;
export type TeamEntity = {
  team_id?: string;
  team_name?: string;
  status?: TeamStatus;
  team_session_id?: string;
  memberships?: Array<unknown>;
};

export const teamApiStubState = {
  entities: [] as TeamEntity[],
};

export async function listTeams(): Promise<TeamEntity[]> {
  return teamApiStubState.entities;
}

export async function getTeamQuota(): Promise<TeamQuotaSnapshot> {
  return {};
}

export async function stopTeam(_params?: unknown): Promise<unknown> {
  return {};
}
