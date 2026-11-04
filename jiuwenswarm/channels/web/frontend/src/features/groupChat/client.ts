import { webRequest } from '../../services/webClient';

export interface GroupMember {
  kind: 'expert' | 'expert_team';
  definition_id: string;
  member_name: string;
  display_name?: string;
}

export interface GroupInfo {
  group_id: string;
  session_id: string;
  team_name: string;
  name: string;
  description: string;
  members: GroupMember[];
  created_at: number;
  updated_at: number;
  status: 'ready' | 'deleting';
}

export interface GroupCreateInput {
  name: string;
  description?: string;
  members?: GroupMember[];
  create_token: string;
  project_id?: string;
  project_dir?: string;
  model_name?: string;
}

export interface GroupMessage {
  message_id: string;
  client_message_id: string;
  sender: string;
  sender_name: string;
  content: string;
  timestamp: number;
  mentions: string[];
  attachments: Record<string, unknown>[];
}

interface PageInput {
  limit?: number;
  offset?: number;
}

export const groupChatClient = {
  create: (input: GroupCreateInput) => webRequest<GroupInfo>('group.create', { ...input }),
  get: (groupId: string) => webRequest<GroupInfo>('group.get', { group_id: groupId }),
  list: (page: PageInput = {}) =>
    webRequest<{ groups: GroupInfo[]; total: number; limit: number; offset: number }>('group.list', { ...page }),
  update: (groupId: string, patch: { name?: string; description?: string }) =>
    webRequest<GroupInfo>('group.update', { group_id: groupId, ...patch }),
  addMember: (groupId: string, member: GroupMember, clientRequestId: string) =>
    webRequest<{ group_id: string; member: GroupMember; status: 'registered'; event_type: 'group.member.added' }>(
      'group.member.add',
      { group_id: groupId, member, client_request_id: clientRequestId },
    ),
  history: (groupId: string, page: PageInput = {}) =>
    webRequest<{ messages: GroupMessage[]; total: number; next_offset: number | null; limit: number; offset: number }>(
      'group.history.get',
      { group_id: groupId, ...page },
    ),
  delete: (groupId: string) =>
    webRequest<{ group_id: string; deleted: boolean }>('group.delete', { group_id: groupId }),
};
