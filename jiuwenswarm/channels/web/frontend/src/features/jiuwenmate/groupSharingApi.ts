import type { JiuwenMateRequest } from './jiuwenmateApi';

export const SHARING_CONVERSATION_ID = 'sharing-approvals';
export type SharingDecision = 'once' | 'permanent' | 'reject';
export type SharingTimestamp = string | number;

/** JSON form of the host's GroupMessageQuery, including filters-only searches. */
export interface SharingQuery {
  query: string | null;
  keywords?: string[];
  since: string | null;
  until: string | null;
  sender_id: string | null;
  cursor: string | null;
  limit: number;
  group_ids: string[];
  group_names: string[];
}

export interface SharingTarget {
  channel: string;
  app_id: string | null;
  kind: string;
  id: string;
  name: string;
}

export interface SharingApproval {
  approval_id: string;
  request_id: string;
  agent_id: string;
  config_id: string;
  status: 'pending' | 'approved_once' | 'approved_permanent' | 'rejected' | 'expired';
  can_decide: boolean;
  requester_name: string;
  source_groups: { group_id: string; name: string }[];
  target: SharingTarget;
  query: SharingQuery;
  summary: string;
  messages: { group_id: string; group_name: string; message_id: string; sender_name: string; text: string | null }[];
  created_at: SharingTimestamp;
  expires_at: SharingTimestamp;
  decided_at: SharingTimestamp | null;
}

export interface SharingGrant {
  grant_id: string;
  source_group_id: string;
  source_group_name: string;
  target: SharingTarget;
  created_at: SharingTimestamp;
  status: 'active' | 'revoked';
}

export interface GroupSharingSnapshot {
  approvals: SharingApproval[];
  grants: SharingGrant[];
  conversation_id: typeof SHARING_CONVERSATION_ID;
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function strings(value: unknown, keys: readonly string[]): boolean {
  return record(value) && keys.every((key) => typeof value[key] === 'string');
}

function target(value: unknown): boolean {
  return record(value) && strings(value, ['channel', 'kind', 'id', 'name']) &&
    (value.app_id === null || typeof value.app_id === 'string');
}

function query(value: unknown): boolean {
  return record(value) &&
    ['query', 'since', 'until', 'sender_id', 'cursor'].every((key) =>
      value[key] === null || typeof value[key] === 'string') &&
    (value.keywords === undefined || (Array.isArray(value.keywords) &&
      value.keywords.every((item: unknown) => typeof item === 'string'))) &&
    typeof value.limit === 'number' && Number.isInteger(value.limit) && value.limit > 0 && value.limit <= 500 &&
    ['group_ids', 'group_names'].every((key) =>
      Array.isArray(value[key]) && value[key].every((item: unknown) => typeof item === 'string'));
}

/** Accept ISO dates and epoch seconds/milliseconds; invalid deadlines cannot be approved. */
export function sharingTime(value: SharingTimestamp): number {
  return typeof value === 'number' ? (value < 1e12 ? value * 1000 : value) : Date.parse(value);
}

export function sharingStatus(approval: SharingApproval, now: number): SharingApproval['status'] {
  return approval.status === 'pending' && sharingTime(approval.expires_at) <= now ? 'expired' : approval.status;
}

export function canDecideSharing(approval: SharingApproval, now: number): boolean {
  return approval.status === 'pending' && approval.can_decide && sharingTime(approval.expires_at) > now;
}

/** Keep the wire fields intact and fail closed on a wrong colleague or conversation. */
function snapshot(value: unknown, configId: string): GroupSharingSnapshot {
  if (
    !record(value) || value.conversation_id !== SHARING_CONVERSATION_ID ||
    !Array.isArray(value.approvals) || !Array.isArray(value.grants) ||
    !value.approvals.every((item: unknown) => record(item) &&
      strings(item, ['approval_id', 'request_id', 'agent_id', 'config_id', 'status', 'requester_name', 'summary']) &&
      query(item.query) &&
      item.config_id === configId && typeof item.can_decide === 'boolean' &&
      ['pending', 'approved_once', 'approved_permanent', 'rejected', 'expired'].includes(String(item.status)) &&
      target(item.target) && Array.isArray(item.source_groups) && item.source_groups.length === 1 &&
      item.source_groups.every((group: unknown) => strings(group, ['group_id', 'name'])) &&
      Array.isArray(item.messages) && item.messages.every((message: unknown) => record(message) &&
        strings(message, ['group_id', 'group_name', 'message_id', 'sender_name']) &&
        (message.text === null || typeof message.text === 'string'))) ||
    !value.grants.every((item: unknown) => record(item) &&
      strings(item, ['grant_id', 'source_group_id', 'source_group_name']) &&
      ['active', 'revoked'].includes(String(item.status)) && target(item.target))
  ) {
    throw new Error('Invalid private sharing snapshot');
  }
  return value as unknown as GroupSharingSnapshot;
}

export function createGroupSharingApi(request: JiuwenMateRequest) {
  return {
    async list(configId: string): Promise<GroupSharingSnapshot> {
      return snapshot(await request('jiuwenmate.sharing.list', { agent_id: configId }), configId);
    },
    async decide(configId: string, approvalId: string, decision: SharingDecision): Promise<GroupSharingSnapshot> {
      return snapshot(await request('jiuwenmate.sharing.decide', {
        agent_id: configId, approval_id: approvalId, decision,
      }), configId);
    },
    async revoke(configId: string, grantId: string): Promise<GroupSharingSnapshot> {
      return snapshot(await request('jiuwenmate.sharing.revoke', { agent_id: configId, grant_id: grantId }), configId);
    },
  };
}

export type GroupSharingApi = ReturnType<typeof createGroupSharingApi>;
