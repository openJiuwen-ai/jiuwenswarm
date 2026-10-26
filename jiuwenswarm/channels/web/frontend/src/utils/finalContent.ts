function decodeQuotedPythonLikeString(raw: string): string {
  return raw
    .replace(/\\r/g, '\r')
    .replace(/\\n/g, '\n')
    .replace(/\\t/g, '\t')
    .replace(/\\'/g, "'")
    .replace(/\\"/g, '"')
    .replace(/\\\\/g, '\\');
}

import { stripResidualInlineToolProtocol } from './toolProtocol';

/** 字面量 `\\n` 明显多于真换行时还原，避免 GFM 表格解析失败。 */
export function unescapeLiteralNewlines(text: string): string {
  const realNl = (text.match(/\n/g) || []).length;
  const litNl = (text.match(/\\n/g) || []).length;
  if (litNl > 0 && litNl > realNl) {
    return text.replace(/\\n/g, '\n').replace(/\\t/g, '\t').replace(/\\r/g, '\r');
  }
  return text;
}

function normalizeFinalDisplayText(text: string): string {
  return unescapeLiteralNewlines(text).replace(/^(?:\r?\n)+/, '');
}

export function collapseWs(value: string): string {
  return value.replace(/\s+/g, ' ').trim();
}

/** 分段收尾时优先用干净 final；整轮拼接则不覆盖本段。 */
export function resolveStreamFinalContent(
  streamed: string,
  finalContent: string,
  isSplit: boolean
): string | undefined {
  if (!finalContent) {
    return undefined;
  }
  if (!isSplit) {
    return finalContent;
  }
  const streamedN = collapseWs(streamed);
  const finalN = collapseWs(finalContent);
  if (!streamedN || streamedN === finalN || finalN.startsWith(streamedN)) {
    return finalContent;
  }
  if (streamedN.includes(finalN) && streamedN.length <= finalN.length + 40) {
    return finalContent;
  }
  if (finalN.includes(streamedN) && finalN.length > streamedN.length + 40) {
    return undefined;
  }
  return undefined;
}

/**
 * 在本轮助手气泡中定位与 final 对应的段。
 * 优先 exact；其次「一方以另一方为前缀」且长度比 ≥ 0.85（不再用宽松 includes）。
 */
export function findAssistantSegmentIdForFinal(
  messages: { role: string; id?: string; content?: string }[],
  finalContent: string,
  preferredSegmentId?: string | null
): string | null {
  const finalN = collapseWs(finalContent);
  if (!finalN) return null;

  let turnStart = 0;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === 'user') {
      turnStart = i + 1;
      break;
    }
  }

  const turn = messages.slice(turnStart);
  if (preferredSegmentId) {
    const preferred = turn.find(
      (msg) => msg.role === 'assistant' && msg.id === preferredSegmentId
    );
    if (preferred?.id) {
      return preferred.id;
    }
  }

  for (let i = turn.length - 1; i >= 0; i -= 1) {
    const msg = turn[i];
    if (msg.role !== 'assistant' || typeof msg.id !== 'string') continue;
    if (typeof msg.content !== 'string' || !msg.content) continue;
    if (collapseWs(msg.content) === finalN) {
      return msg.id;
    }
  }

  for (let i = turn.length - 1; i >= 0; i -= 1) {
    const msg = turn[i];
    if (msg.role !== 'assistant' || typeof msg.id !== 'string') continue;
    if (typeof msg.content !== 'string' || !msg.content) continue;
    const msgN = collapseWs(msg.content);
    if (!msgN) continue;
    const longer = Math.max(msgN.length, finalN.length);
    const shorter = Math.min(msgN.length, finalN.length);
    if (shorter / longer < 0.85) continue;
    if (msgN.startsWith(finalN) || finalN.startsWith(msgN)) {
      return msg.id;
    }
  }

  return null;
}

export function normalizeFinalContent(payload: Record<string, unknown>): string {
  const rawContent = payload.content;
  if (typeof rawContent !== 'string') {
    return '';
  }

  const cleaned = stripResidualInlineToolProtocol(rawContent);

  const trimmed = cleaned.trim();

  if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
    try {
      const parsed = JSON.parse(trimmed) as Record<string, unknown>;
      if (typeof parsed.output === 'string') {
        return normalizeFinalDisplayText(parsed.output);
      }
    } catch {
      // ignore
    }
  }

  if (!trimmed.includes('result_type') || !trimmed.includes('output')) {
    try {
      const parsed = JSON.parse(trimmed) as Record<string, unknown>;
      if (parsed.delta && typeof parsed.delta === 'object') {
        const delta = parsed.delta as Record<string, unknown>;
        if (typeof delta.content === 'string') {
          return normalizeFinalDisplayText(delta.content);
        }
      }
    } catch {
      // ignore
    }
    return normalizeFinalDisplayText(cleaned);
  }

  const singleQuoted = cleaned.match(/['"]output['"]\s*:\s*'((?:\\'|[^'])*)'/s);
  if (singleQuoted?.[1] != null) {
    return normalizeFinalDisplayText(decodeQuotedPythonLikeString(singleQuoted[1]));
  }

  const doubleQuoted = cleaned.match(/['"]output['"]\s*:\s*"((?:\\"|[^"])*)"/s);
  if (doubleQuoted?.[1] != null) {
    return normalizeFinalDisplayText(decodeQuotedPythonLikeString(doubleQuoted[1]));
  }

  return normalizeFinalDisplayText(cleaned);
}
