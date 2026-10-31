/**
 * Enterprise Team control-plane API (phase 1).
 *
 * Thin typed wrappers over the Gateway Web RPC methods
 * `team.create / team.get / team.list / team.stop / team.member.join /
 * team.member.leave / team.members.list`. Works over both WebSocket and the
 * enterprise HTTP transport (see `webHttpClient.ROUTES`).
 */
import { webRequest } from './webClient';

export type TeamStatus = 'creating' | 'running' | 'paused' | 'stopping' | 'stopped' | 'deleted';
export type MembershipStatus = 'pending' | 'active' | 'left' | 'revoked' | 'expired';

export interface TeamEntity {
  team_id: string;
  team_name: string;
  owner_user_id: string;
  owner_group_id: string;
  owner_bot_id: string;
  status: TeamStatus;
  runtime_mode: string;
  template_id: string;
  team_session_id: string;
  runtime_team_name: string;
  agent_service_id: string;
  gateway_id: string;
  created_at: number;
  updated_at: number;
  extra?: Record<string, unknown>;
  runtime_active?: boolean;
  runtime_pending?: boolean;
  memberships?: TeamMembership[];
}

export interface TeamMembership {
  membership_id: string;
  team_id: string;
  user_id: string;
  group_id: string;
  bot_id: string;
  member_name: string;
  member_type: string;
  display_name: string;
  permissions: string[];
  status: MembershipStatus;
  expires_at: number | null;
  joined_at: number;
  updated_at: number;
}

export interface TeamLookup {
  team_id?: string;
  session_id?: string;
  team_name?: string;
}

export async function listTeams(includeDeleted = false): Promise<TeamEntity[]> {
  const payload = await webRequest<{ teams: TeamEntity[]; count: number }>(
    'team.list',
    includeDeleted ? { include_deleted: true } : {},
  );
  return Array.isArray(payload?.teams) ? payload.teams : [];
}

export async function stopTeam(lookup: TeamLookup): Promise<TeamEntity> {
  const payload = await webRequest<{ team: TeamEntity; runtime_stopped: boolean }>(
    'team.stop',
    lookup as Record<string, unknown>,
  );
  return payload.team;
}

export interface TeamQuotaSlice {
  used: number;
  limit: number;
  default: number;
  occupying_ids?: string[];
}

export interface TeamQuotaOrgSlice {
  group_id: string;
  used: number;
  limit: number;
}

export interface TeamQuotaSnapshot {
  teams: TeamQuotaSlice;
  teammates_default_limit: number;
  org?: TeamQuotaOrgSlice;
}

export async function getTeamQuota(): Promise<TeamQuotaSnapshot> {
  return webRequest<TeamQuotaSnapshot>('team.quota.get', {});
}
