import { resolveAgentGroupSelectionId } from '../agentManagement/port';
import type { AgentCatalogItem, AgentGroupCatalogItem } from '../agentManagement/types';
import type { GroupExecution, GroupMember, GroupMessage } from './client';

// Keep pending-reply eligibility across workspace remounts, scoped to this page lifetime.
const sessionSentIdsByGroup = new Map<string, Set<string>>();

export function groupSessionSentIds(groupId: string): Set<string> {
  let ids = sessionSentIdsByGroup.get(groupId);
  if (!ids) {
    ids = new Set<string>();
    sessionSentIdsByGroup.set(groupId, ids);
  }
  return ids;
}

export function isBackgroundRefreshTimeout(error: unknown, hasSnapshot: boolean): boolean {
  return hasSnapshot && (error as { code?: string } | null)?.code === 'REQUEST_TIMEOUT';
}

export function groupMessageContent(message: GroupMessage): string {
  let content = message.content;
  for (const file of message.attachments ?? []) {
    if (typeof file.path === 'string' && file.path && typeof file.name === 'string' && file.name) {
      content = content.split(file.path).join(file.name);
    }
  }
  return message.sender === 'user' ? content : stripLeadingUserMention(content);
}

/** 成员回复常以「@user …」开头称呼用户，展示时剥掉该前缀（群聊里显式回显没有意义）。 */
export function stripLeadingUserMention(content: string): string {
  return content.replace(/^@user[\s:，、]+/u, '');
}

export function mergeMessages(current: GroupMessage[], incoming: GroupMessage[]): GroupMessage[] {
  const messages = new Map(current.map((message) => [message.message_id, message]));
  for (const message of incoming) {
    if (!message.pending && message.client_message_id) {
      for (const [id, previous] of messages) {
        if (previous.pending && previous.client_message_id === message.client_message_id) messages.delete(id);
      }
    }
    messages.set(message.message_id, { ...messages.get(message.message_id), ...message });
  }
  return [...messages.values()].sort((a, b) => a.timestamp - b.timestamp || a.message_id.localeCompare(b.message_id));
}

export function mergeExecutions(current: GroupExecution[], incoming: GroupExecution[]): GroupExecution[] {
  const executions = new Map(current.map((execution) => [execution.execution_id, execution]));
  for (const execution of incoming) {
    const previous = executions.get(execution.execution_id);
    if (!previous || execution.revision > previous.revision) executions.set(execution.execution_id, execution);
  }
  return [...executions.values()].sort((a, b) => a.created_at - b.created_at);
}

export function groupTimeline(messages: GroupMessage[], executions: GroupExecution[]) {
  const prompts = new Map(messages.filter((message) => message.client_message_id)
    .map((message) => [message.client_message_id, message]));
  return [
    ...messages.map((message) => ({ id: message.message_id, at: message.timestamp, order: 0, message, execution: null })),
    ...executions.map((execution) => {
      const prompt = execution.execution_kind === 'organization' && execution.organization_plan_id
        ? prompts.get(execution.organization_plan_id) : undefined;
      return { id: execution.execution_id, at: Math.max(execution.created_at * 1000, prompt?.timestamp ?? -Infinity),
        order: prompt ? 1 : 0, message: null, execution };
    }),
  ].sort((a, b) => a.at - b.at || a.order - b.order || a.id.localeCompare(b.id));
}

export function newMember(kind: GroupMember['kind'], id: string, displayName: string, description?: string): GroupMember {
  return { kind, id, display_name: displayName, member_name: `member-${crypto.randomUUID()}`, ...(description ? { description } : {}) };
}

/** Case-insensitive, trimmed match for one displayed member label; an empty query matches everything. */
export function matchesMemberQuery(label: string, query: string): boolean {
  const needle = query.trim().toLocaleLowerCase();
  return !needle || label.toLocaleLowerCase().includes(needle);
}

/**
 * Match the visible row label, including its fallback when no display name is set.
 */
export function filterGroupMembers(members: GroupMember[], query: string): GroupMember[] {
  if (!query.trim()) return members;
  return members.filter((member) => matchesMemberQuery(member.display_name || member.member_name, query));
}

export function selectedMentions(content: string, selected: string[], members: GroupMember[]): string[] {
  const mentions: string[] = [];
  for (const member of members) {
    const name = member.display_name || member.member_name;
    const sameName = members.filter((item) => (item.display_name || item.member_name) === name);
    if (sameName.length > 1 && !selected.includes(member.member_name)) continue;
    const label = `@${name}`;
    const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const pattern = new RegExp(`${escaped}(?=$|[\\s，。！？、,:：;；!?])`);
    if (pattern.test(content)) mentions.push(member.member_name);
  }
  return mentions;
}

/** 用户气泡内容的一个片段：普通文本或一枚 @成员 提及 chip。 */
export type GroupMentionPart = { kind: 'text'; text: string } | { kind: 'mention'; label: string };

/** 把用户消息按 @成员 切成文本段与提及 chip（显示名最长优先，边界规则与输入框一致）。 */
export function splitMentionParts(content: string, members: GroupMember[]): GroupMentionPart[] {
  if (!content.includes('@')) return [{ kind: 'text', text: content }];
  const labels = [...new Set(members.map((member) => member.display_name || member.member_name)
    .filter(Boolean))].sort((a, b) => b.length - a.length);
  if (!labels.length) return [{ kind: 'text', text: content }];
  const pattern = new RegExp(
    `@(${labels.map((label) => label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})(?=$|[\\s，。！？、,:：;；!?])`,
    'g',
  );
  const parts: GroupMentionPart[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(content)) !== null) {
    if (match.index > lastIndex) {
      parts.push({ kind: 'text', text: content.slice(lastIndex, match.index) });
    }
    parts.push({ kind: 'mention', label: match[1] });
    lastIndex = pattern.lastIndex;
  }
  if (lastIndex < content.length) parts.push({ kind: 'text', text: content.slice(lastIndex) });
  return parts;
}

/** @成员占位气泡的执行阶段（由成员当前执行快照推导，组件层再转 i18n 文案）。 */
export type MentionExecutionPhase =
  | { key: 'dispatching' }
  | { key: 'planning' }
  | { key: 'generating' }
  | { key: 'executing'; completed: number | null; total: number | null }
  | { key: 'reviewing' }
  | { key: 'processing' };

export function mentionExecutionPhase(execution: GroupExecution | undefined): MentionExecutionPhase {
  if (!execution) return { key: 'dispatching' };
  const status = execution.status;
  if (status === 'planning') return { key: 'planning' };
  if (status === 'in_review') return { key: 'reviewing' };
  if (['running', 'in_progress'].includes(status)) {
    const counts = taskCounts(execution.tasks);
    const progress = execution.progress ?? (counts.total ? { completed: counts.completed, total: counts.total } : null);
    // 无子任务（tasks 空且 progress.total=0）说明还在生成任务计划，
    // 与执行卡片的「任务生成中」对齐，避免误显「正在执行任务…」。
    if (!progress || !progress.total) return { key: 'generating' };
    return { key: 'executing', completed: progress.completed, total: progress.total };
  }
  return { key: 'processing' };
}

export const FINISHED_EXECUTIONS = new Set(['completed', 'failed', 'cancelled', 'interrupted', 'timed_out', 'stopped']);

/** 一条 @ 消息下尚未回音的成员，及其当前执行快照（尚未创建执行时为 undefined）。 */
export interface PendingMentionReply {
  memberName: string;
  execution: GroupExecution | undefined;
}

/**
 * 推导每条带 @ 的用户消息下应挂的「待回复」占位，键为 message_id。
 *
 * 本页面发出的消息（sentIds）一发出就占位，覆盖执行尚未创建的窗口；其余消息只在被 @ 成员
 * 有未完成执行时占位，刷新/换设备后据服务端执行恢复。一个执行只归属创建前最近一条 @ 该成员
 * 的消息，旧提问不会因后来的执行重复占位；早已无执行的旧提问不占位。
 * 占位跟随执行：执行未结束时，成员中途发的消息不撤销占位；没有执行时，成员回复即视为已答复。
 */
export function pendingMentionReplies(
  messages: GroupMessage[],
  executions: GroupExecution[],
  members: GroupMember[],
  sentIds: Set<string>,
): Map<string, PendingMentionReply[]> {
  const asks = messages.filter((message) => message.sender === 'user' && !message.pending).map((message) => ({
    message,
    mentioned: message.mentions?.length
      ? message.mentions
      : selectedMentions(groupMessageContent(message), [], members),
  }));
  // 组织执行只记录 Owner；它由创建前最近一条 @ Owner 的消息触发，该消息里 @ 的成员都参与这次组织协作。
  const organizationTrigger = (execution: GroupExecution) => asks.filter((ask) =>
    Boolean(execution.owner_member) && ask.mentioned.includes(execution.owner_member as string)
    && ask.message.timestamp <= execution.created_at * 1000).pop()?.message.message_id;
  const pending = new Map<string, PendingMentionReply[]>();
  for (const { message, mentioned } of asks) {
    const sentHere = sentIds.has(message.client_message_id);
    const waiting = mentioned.flatMap<PendingMentionReply>((memberName) => {
      // executions 已按 created_at 升序（mergeExecutions），pop 取该成员消息之后最新一次执行。
      const execution = executions.filter((item) =>
        (item.member_name === memberName || item.owner_member === memberName
          || (item.execution_kind === 'organization' && organizationTrigger(item) === message.message_id))
        && item.created_at * 1000 >= message.timestamp).pop();
      if (execution && FINISHED_EXECUTIONS.has(execution.status)) return [];
      if (!execution && messages.some((item) => item.timestamp > message.timestamp && item.sender === memberName)) {
        return [];
      }
      if (!execution) return sentHere ? [{ memberName, execution }] : [];
      const owner = asks.filter((ask) => ask.mentioned.includes(memberName)
        && ask.message.timestamp <= execution.created_at * 1000).pop();
      return owner?.message.message_id === message.message_id ? [{ memberName, execution }] : [];
    });
    if (waiting.length) pending.set(message.message_id, waiting);
  }
  return pending;
}

export function safeAttachmentUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value) return undefined;
  if (value.startsWith('/') && !value.startsWith('//') && !value.includes('\\')) return value;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : undefined;
  } catch {
    return undefined;
  }
}

/**
 * 群聊一旦开始执行就锁定模型：成员实例在首次执行时按当时的模型创建并持续复用，
 * 中途切换不会生效，只在空群（无消息、无执行、无更早历史）时允许选择。
 */
export function groupModelLocked(messages: GroupMessage[], executions: GroupExecution[], hasOlderHistory: boolean): boolean {
  return hasOlderHistory || messages.length > 0 || executions.length > 0;
}

export function executionResult(execution: GroupExecution, messages: GroupMessage[]): GroupMessage | undefined {
  return messages.find((message) => {
    const sameMessage = Boolean(execution.message_id) && message.message_id === execution.message_id;
    const sameExecution = Boolean(message.execution_id) && message.execution_id === execution.execution_id;
    if (message.sender === execution.member_name && (sameExecution || sameMessage)) return true;
    return Boolean(execution.owner_member) && message.sender === execution.owner_member && sameMessage;
  });
}

export interface GroupMemberCandidate {
  providerLabel?: string;
  id: string;
  packageId: string;
  kind: GroupMember['kind'];
  name: string;
  description: string;
  avatar: string | null;
}

export function groupMemberCandidates(
  agents: AgentCatalogItem[],
  teams: AgentGroupCatalogItem[],
): GroupMemberCandidate[] {
  return [
    ...agents.filter((item) => item.installed && item.enabled !== false).map((item): GroupMemberCandidate => ({
      id: item.id, packageId: resolveAgentGroupSelectionId(item), kind: 'agent_template', name: item.displayName,
      description: item.description, avatar: item.avatarUrl,
      providerLabel: item.runtime?.provider_name === 'claudecode' ? 'Claude Code'
        : item.runtime?.provider_name === 'codex' ? 'Codex' : item.runtime?.provider_name,
    })),
    ...teams.filter((item) => item.installed && item.capabilities.canUse).map((item): GroupMemberCandidate => ({
      id: item.id, packageId: item.name.trim() || item.id, kind: 'agent_group', name: item.displayName,
      description: item.description, avatar: item.avatarUrl,
    })),
  ];
}

export function shortTaskName(value: string | null | undefined, fallback: string, limit = 36): string {
  const first = (value ?? '').split(/[\n。；;]/, 1)[0].replace(/^\s*[#*\-]+\s*/, '').trim();
  const chars = Array.from(first || fallback);
  return chars.length > limit ? `${chars.slice(0, limit).join('')}…` : chars.join('');
}

export function taskCounts(tasks: Record<string, string> = {}) {
  const counts = { total: 0, running: 0, completed: 0, stopped: 0, failed: 0 };
  for (const status of Object.values(tasks)) {
    counts.total += 1;
    if (status === 'completed') counts.completed += 1;
    else if (['cancelled', 'stopped', 'interrupted'].includes(status)) counts.stopped += 1;
    else if (['failed', 'timed_out'].includes(status)) counts.failed += 1;
    else if (['running', 'in_progress', 'planning', 'in_review'].includes(status)) counts.running += 1;
  }
  return counts;
}

/** Compact size label for file rows, e.g. 512B, 226.12K, 3.4M. */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  const units = ['K', 'M', 'G'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${Number(value.toFixed(2))}${units[unit]}`;
}

/** Apply a confirmed file rename or delete to attachments already loaded on the client. */
export function renameAttachment(file: Record<string, unknown>, name: string, path: string): Record<string, unknown> {
  const renamed: Record<string, unknown> = { ...file, name, path };
  if (path !== file.path) {
    for (const key of ['url', 'download_url', 'download_token']) delete renamed[key];
    if ('file_path' in renamed) renamed.file_path = path;
  }
  return renamed;
}

export function updateAttachments<T extends { attachments?: Record<string, unknown>[] }>(
  records: T[], path: string, name: string | null, newPath: string = path,
): T[] {
  return records.map((record) => {
    if (!record.attachments?.some((file) => file.path === path)) return record;
    const attachments = name === null
      ? record.attachments.filter((file) => file.path !== path)
      : record.attachments.map((file) => file.path === path ? renameAttachment(file, name, newPath) : file);
    return { ...record, attachments };
  });
}
