import { resolveAgentGroupSelectionId } from '../agentManagement/port';
import type { AgentCatalogItem, AgentGroupCatalogItem } from '../agentManagement/types';
import type { GroupExecution, GroupMember, GroupMessage } from './client';

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
  return content;
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

export function newMember(kind: GroupMember['kind'], id: string, displayName: string): GroupMember {
  return { kind, id, display_name: displayName, member_name: `member-${crypto.randomUUID()}` };
}

export function selectedMentions(content: string, selected: string[], members: GroupMember[]): string[] {
  const mentions: string[] = [];
  for (const member of members) {
    if (!selected.includes(member.member_name)) continue;
    const label = `@${member.display_name || member.member_name}`;
    const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const pattern = new RegExp(`${escaped}(?=$|[\\s，。！？、,:：;；!?])`);
    if (pattern.test(content)) mentions.push(member.member_name);
  }
  return mentions;
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
