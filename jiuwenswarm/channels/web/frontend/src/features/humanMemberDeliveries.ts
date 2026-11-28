import type { Message, FileDownloadItem } from '../types';
import { isEnterprise } from '../edition';

interface Execution {
  member_id: string;
  kind: string;
  timestamp: number;
  tool_name?: string;
  tool_call_id?: string;
  tool_arguments?: unknown;
  tool_success?: boolean;
  tool_output?: unknown;
}
interface Member { member_id: string; mode?: string; role?: string }

function asRecord(value: unknown): Record<string, unknown> | undefined {
  try {
    const parsed = typeof value === 'string' ? JSON.parse(value) : value;
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed;
  } catch {
    // Legacy text-only outputs cannot confirm individual multicast recipients.
  }
  return undefined;
}

function humanRecipients(
  args: Record<string, unknown>,
  result: Execution,
  humans: Set<string>,
): string[] {
  const to = typeof args.to === 'string' ? args.to.trim() : '';
  const targets = Array.isArray(args.targets)
    ? args.targets.filter((target): target is string => typeof target === 'string').map((target) => target.trim())
    : undefined;
  const requested = targets ?? (to === '*' ? [...humans].filter((member) => member !== result.member_id) : [to]);
  let recipients = requested;
  if (result.tool_success !== true) {
    const output = asRecord(result.tool_output);
    const data = asRecord(output?.data) ?? output;
    if (!targets || result.tool_success !== false || data?.type !== 'multicast' || !Array.isArray(data.delivered)) {
      return [];
    }
    // A failed multicast can still have confirmed deliveries. Never infer them from the requested targets.
    const delivered = new Set(data.delivered.filter((target): target is string => typeof target === 'string'));
    recipients = requested.filter((target) => delivered.has(target));
  }
  return [...new Set(recipients)].filter((recipient) => humans.has(recipient));
}

/** Project confirmed deliveries only; internal AI collaboration stays in the process panel. */
export function humanMemberDeliveries(events: Execution[], members: Member[]): Message[] {
  const humans = new Set(members.filter(m => m.mode === 'human' || m.role === 'human_agent' || m.role === 'passive_human').map(m => m.member_id));
  const calls = new Map(events.filter(e => e.kind === 'tool_call' && e.tool_name === 'send_message' && e.tool_call_id).map(e => [`${e.member_id}:${e.tool_call_id}`, e]));
  const seen = new Set<string>();
  return events.flatMap(result => {
    const key = `${result.member_id}:${result.tool_call_id}`;
    if (result.kind !== 'tool_result' || seen.has(key)) return [];
    const call = calls.get(key);
    if (!call) return [];
    const args = asRecord(call.tool_arguments);
    if (!args || typeof args.content !== 'string' || !args.content.trim()) return [];
    const { content } = args;
    const recipients = humanRecipients(args, result, humans);
    if (recipients.length === 0) return [];
    seen.add(key);
    return recipients.map((recipient) => {
      const id = `human-delivery:${key}:${encodeURIComponent(recipient)}`;
      return {
        id,
        role: 'system' as const,
        timestamp: new Date(result.timestamp).toISOString(),
        content: `team.event:${JSON.stringify({ event: { type: 'team.message.p2p', message_id: id, from_member: result.member_id, to_member: recipient, recipient_is_human: true, projection_source: 'send_message', content } })}`,
        keepExpanded: true,
      };
    });
  });
}

/** Enterprise-only path cards; personal edition keeps text and existing file messages unchanged. */
export function withArtifactReferences(message: Message): Message {
  if (!isEnterprise() || message.role === 'user') return message;
  let content = message.content;
  let event: { event?: { content?: string } } | undefined;
  if (content.startsWith('team.event:')) {
    try { event = JSON.parse(content.slice(11)); content = event?.event?.content || ''; } catch { return message; }
  }
  const files: FileDownloadItem[] = [...(message.fileItems || [])];
  const next = content.replace(/(?:\*\*|`)?(\/(?:[^\s`*<>"]+\/)?team-workspace\/artifacts\/[^\s`*<>"]+\.[a-zA-Z0-9]+)(?:\*\*|`)?/g, (_match, path: string) => {
    const name = path.split('/').pop()!;
    if (!files.some(f => f.path === path)) files.push({ name, path, size: 0, mime_type: '', download_url: '', download_token: '' });
    return name;
  });
  if (next === content) return message;
  if (event?.event) { event.event.content = next; return { ...message, content: `team.event:${JSON.stringify(event)}`, fileItems: files }; }
  return { ...message, content: next, fileItems: files };
}
