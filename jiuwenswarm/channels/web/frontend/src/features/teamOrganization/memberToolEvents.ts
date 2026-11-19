// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import i18n from '../../i18n';
import { useSessionStore, type TeamMemberExecutionEvent } from '../../stores/sessionStore';
import { parseTimestampToMs } from '../../utils/timestamp';
import { normalizeFinalContent } from '../../utils/finalContent';
import type { NormalizedToolCall, NormalizedToolResult } from '../tool-events/toolEventNormalizer';

type Payload = Record<string, unknown>;

function executionContent(value: unknown): string {
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value) ?? '';
  } catch {
    return String(value ?? '');
  }
}

function memberName(payload: Payload): string {
  for (const value of [payload.member_name, payload.member_id, payload.source_member]) {
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

/** Project child member output and tools into the member process panel's store. */
export class MemberToolEventProjector {
  private readonly members = new Map<string, string>();

  private callKey(key: string, payload: Payload, callId: string): string {
    return JSON.stringify([key, payload.request_id ?? payload.rid ?? '', callId]);
  }

  toolCall(key: string, payload: Payload, call: NormalizedToolCall): void {
    if (String(payload.role).trim().toLowerCase() !== 'teammate') return;
    const member = memberName(payload) || (call.memberName !== 'teammate' ? call.memberName : '');
    if (!member) return;
    this.members.set(this.callKey(key, payload, call.id), member);
    if (this.members.size > 300) {
      const oldest = this.members.keys().next().value;
      if (oldest) this.members.delete(oldest);
    }
    this.append(key, payload, member, 'tool_call', call.id, call.name,
      call.description || call.formatted_args || executionContent(call.arguments));
  }

  toolResult(key: string, payload: Payload, result: NormalizedToolResult): void {
    const role = String(payload.role ?? '').trim().toLowerCase();
    if (role && role !== 'teammate') return;
    const correlated = result.toolCallId ? this.members.get(this.callKey(key, payload, result.toolCallId)) : '';
    const member = memberName(payload) || correlated;
    if (!member || (!role && !correlated)) return;
    this.append(key, payload, member, 'tool_result', result.toolCallId, result.toolName,
      result.summary || executionContent(result.result));
  }

  final(key: string, payload: Payload): void {
    if (String(payload.role ?? '').trim().toLowerCase() !== 'teammate') return;
    const member = memberName(payload);
    const content = normalizeFinalContent(payload);
    if (!member || !content.trim()) return;
    const timestamp = parseTimestampToMs(payload.timestamp);
    const store = useSessionStore.getState();
    store.ensureRuntime(key);
    store.addTeamMemberExecutionEvent(key, {
      id: JSON.stringify(['org-member-final', key, member, payload.request_id ?? payload.rid ?? '',
        payload.timestamp ?? '', content]),
      member_id: member,
      kind: 'final',
      timestamp: Number.isFinite(timestamp) ? timestamp : Date.now(),
      title: i18n.t('team.process.execution.final'),
      content,
    });
  }

  private append(
    key: string, payload: Payload, member: string, kind: 'tool_call' | 'tool_result',
    callId: string | undefined, tool: string, content: string,
  ): void {
    const timestamp = parseTimestampToMs(payload.timestamp);
    const event: TeamMemberExecutionEvent = {
      id: JSON.stringify(['org-member-tool', key, member, kind, payload.request_id ?? payload.rid ?? '',
        callId || payload.timestamp || content]),
      member_id: member,
      kind,
      timestamp: Number.isFinite(timestamp) ? timestamp : Date.now(),
      title: i18n.t(`team.process.execution.${kind === 'tool_call' ? 'toolCallTitle' : 'toolResultTitle'}`, { tool }),
      content,
      tool_name: tool,
      tool_call_id: callId,
    };
    const store = useSessionStore.getState();
    store.ensureRuntime(key);
    store.addTeamMemberExecutionEvent(key, event);
  }
}
