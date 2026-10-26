/**
 * chat.final 落地协议。
 *
 * 后端可显式下发 final_mode：
 * - replace_turn：用 final 替换本轮全部助手气泡
 * - patch_segment：覆写当前/匹配的分段气泡（工具打断后的常见路径）
 * - append：追加新气泡
 *
 * 无 final_mode 时（旧历史）走收紧后的启发式，不再用宽松 includes 折叠整轮。
 */
import { collapseWs } from './finalContent';

export type ChatFinalMode = 'replace_turn' | 'patch_segment' | 'append';

export type ChatFinalAction =
  | { type: 'replace_turn' }
  | { type: 'patch_segment'; segmentId?: string }
  | { type: 'append' }
  | { type: 'heuristic' };

const A2UI_OPEN_TAG = '<a2ui-json>';

export function contentHasA2UIBlock(text: string): boolean {
  return text.includes(A2UI_OPEN_TAG);
}

export function parseChatFinalMode(payload: Record<string, unknown>): ChatFinalMode | null {
  const raw = payload.final_mode ?? payload.finalMode;
  if (typeof raw !== 'string') return null;
  const mode = raw.trim().toLowerCase();
  if (mode === 'replace_turn' || mode === 'replace-turn' || mode === 'covers_turn') {
    return 'replace_turn';
  }
  if (mode === 'patch_segment' || mode === 'patch-segment' || mode === 'segment') {
    return 'patch_segment';
  }
  if (mode === 'append') {
    return 'append';
  }
  return null;
}

export function parseChatFinalSegmentId(payload: Record<string, unknown>): string | undefined {
  for (const key of ['segment_id', 'segmentId', 'stream_message_id', 'streamMessageId'] as const) {
    const value = payload[key];
    if (typeof value === 'string' && value.trim()) {
      return value.trim();
    }
  }
  return undefined;
}

export function interpretChatFinalAction(payload: Record<string, unknown>): ChatFinalAction {
  const mode = parseChatFinalMode(payload);
  if (mode === 'replace_turn') {
    return { type: 'replace_turn' };
  }
  if (mode === 'patch_segment') {
    return { type: 'patch_segment', segmentId: parseChatFinalSegmentId(payload) };
  }
  if (mode === 'append') {
    return { type: 'append' };
  }
  return { type: 'heuristic' };
}

function collectTurnAssistantParts(
  messages: { role: string; id?: string; content?: string }[],
  kind: 'agent' | 'team'
): string[] {
  let turnStart = 0;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === 'user') {
      turnStart = i + 1;
      break;
    }
  }
  const parts: string[] = [];
  for (const msg of messages.slice(turnStart)) {
    if (kind === 'agent') {
      if (msg.role === 'assistant' && typeof msg.content === 'string' && msg.content) {
        parts.push(msg.content);
      }
      continue;
    }
    if (
      msg.role === 'system' &&
      typeof msg.id === 'string' &&
      msg.id.startsWith('team-leader-') &&
      typeof msg.content === 'string' &&
      msg.content
    ) {
      const raw = msg.content;
      if (raw.startsWith('team.leader:')) {
        try {
          const parsed = JSON.parse(raw.slice('team.leader:'.length)) as { content?: unknown };
          if (typeof parsed?.content === 'string' && parsed.content) {
            parts.push(parsed.content);
            continue;
          }
        } catch {
          // fall through
        }
      }
      parts.push(raw);
    }
  }
  return parts;
}

/**
 * 是否允许折叠整轮。显式 replace_turn 优先；启发式仅允许空白折叠后的精确相等
 *（不再用 final.includes(shown) 的宽松匹配）。
 */
export function shouldCollapseTurnFinal(
  messages: { role: string; id?: string; content?: string }[],
  finalContent: string,
  kind: 'agent' | 'team',
  action: ChatFinalAction = { type: 'heuristic' }
): boolean {
  if (action.type === 'patch_segment' || action.type === 'append') {
    return false;
  }
  if (contentHasA2UIBlock(finalContent)) {
    return false;
  }
  const parts = collectTurnAssistantParts(messages, kind);
  if (parts.some(contentHasA2UIBlock)) {
    return false;
  }
  if (action.type === 'replace_turn') {
    return true;
  }
  const shownN = collapseWs(parts.join(''));
  const finalN = collapseWs(finalContent);
  if (!shownN) return true;
  return finalN === shownN;
}
