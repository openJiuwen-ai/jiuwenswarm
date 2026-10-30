import type { ModelSelection, AskUserQuestionPayload, UserAnswer } from '../../types';
import { webClient, webRequest } from '../../services/webClient';

export interface GroupMember {
  kind: 'agent_template' | 'agent_group';
  cli_agent?: 'claude' | 'codex';
  id: string;
  member_name: string;
  display_name?: string;
}

export interface GroupQuestion extends AskUserQuestionPayload {
  member_name?: string;
  execution_id?: string | null;
}

export interface GroupInfo {
  pending_questions?: GroupQuestion[];
  group_id: string;
  session_id: string;
  team_name: string;
  name: string;
  description: string;
  avatar: string;
  members: GroupMember[];
  configs: Record<string, unknown>;
  created_at: number;
  updated_at: number;
  status: 'ready' | 'deleting';
}

export interface GroupCreateInput {
  name: string;
  description?: string;
  avatar?: string;
  members?: GroupMember[];
  configs?: Record<string, unknown>;
  create_token: string;
}

export interface GroupMessage {
  pending?: boolean;
  message_id: string;
  execution_id?: string | null;
  reply_id?: string;
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
  update: (groupId: string, patch: { name?: string; description?: string; avatar?: string }) =>
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
  executions: (groupId: string, page: PageInput = {}) =>
    webRequest<{ executions: GroupExecution[]; total: number; limit: number; offset: number }>(
      'group.execution.list', { group_id: groupId, ...page },
    ),
  send: (groupId: string, input: {
    query: string; mentions: string[]; client_message_id: string; intent: 'chat' | 'task';
    model_name?: string; model_selection?: ModelSelection;
  }) => new Promise<void>((resolve, reject) => {
    let requestId: string | undefined;
    let settled = false;
    const controller = new AbortController();
    const stops: Array<() => void> = [];
    const cleanup = () => {
      window.clearTimeout(timer);
      stops.forEach((stop) => stop());
      controller.abort();
    };
    const fail = (error: unknown) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(error);
    };
    const timer = window.setTimeout(() => fail(new Error('GROUP_MESSAGE_TIMEOUT')), 60_000);
    stops.push(webClient.on('team.group_message.accepted', ({ payload }) => {
      const message = payload.message as GroupMessage | undefined;
      const clientMessageId = message?.client_message_id ?? payload.client_message_id;
      if (payload.session_id !== groupId || clientMessageId !== input.client_message_id) return;
      settled = true;
      cleanup();
      resolve();
    }));
    const onFailure = ({ payload }: { payload: Record<string, unknown> }) => {
      if (!requestId || (payload.request_id ?? payload.rid) !== requestId) return;
      fail(new Error(String(payload.error || payload.message || 'GROUP_MESSAGE_FAILED')));
    };
    stops.push(webClient.on('chat.error', onFailure));
    stops.push(webClient.on('team.interact.failed', onFailure));
    void webRequest('chat.send', { group_id: groupId, ...input, mode: 'team', type: 'group_chat' }, {
      isStream: true, timeoutMs: 60_000, signal: controller.signal, onRequestId: (id) => { requestId = id; },
    }).catch(fail);
  }),
  answer: (groupId: string, requestId: string, answers: UserAnswer[]) =>
    webRequest<{ request_id: string; accepted: boolean }>('chat.send', {
      group_id: groupId, query: '', source: 'ask_user_interrupt', request_id: requestId, answers,
    }),
  renameFile: (groupId: string, path: string, name: string) =>
    webRequest<{ group_id: string; path: string; name: string }>('group.file.rename', { group_id: groupId, path, name }),
  deleteFile: (groupId: string, path: string) =>
    webRequest<{ group_id: string; path: string; deleted: true; file_removed: boolean }>(
      'group.file.delete', { group_id: groupId, path },
    ),
  clearHistory: (groupId: string) =>
    webRequest<{ group_id: string; cleared: true; history_cleared_at: number }>('group.history.clear', { group_id: groupId }),
  delete: (groupId: string) =>
    webRequest<{ group_id: string; deleted: boolean }>('group.delete', { group_id: groupId }),
};

export interface GroupExecution {
  group_id?: string;
  execution_id: string;
  member_name: string | null;
  owner_member?: string | null;
  display_name: string;
  execution_kind: 'agent' | 'team' | 'organization';
  title: string;
  task: string;
  status: string;
  phase: string;
  revision: number;
  created_at: number;
  updated_at: number;
  progress: { completed: number; total: number } | null;
  tasks?: Record<string, string>;
  task_details?: Record<string, { title?: string; content?: string; member_id?: string; status?: string }>;
  result?: string | null;
  message_id?: string | null;
  attachments?: Record<string, unknown>[];
}
