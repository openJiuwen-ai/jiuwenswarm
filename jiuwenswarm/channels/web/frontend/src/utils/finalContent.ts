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
  messages: { role: string; id?: string; content?: string; supplementalInput?: unknown }[],
  finalContent: string,
  preferredSegmentId?: string | null
): string | null {
  const finalN = collapseWs(finalContent);
  if (!finalN) return null;

  let turnStart = 0;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === 'user' && !messages[i].supplementalInput) {
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

/**
 * 字符串类型的 chat.final.content 就是展示正文，仅做展示层归一（还原字面
 * 换行、去掉开头的空行）。
 *
 * 协议包装（agent invoke 风格的 `{"output": ..., "result_type": ...}` 字典）
 * 在结构化数据进入正文之前由后端解包（_parse_stream_chunk：answer chunk 的
 * payload.output 提取后才写入 content），前端不得再从正文中按关键字猜测
 * 解包——回答本身包含 JSON 示例（含 output/delta.content 字段）时，旧逻辑
 * 会把示例里的值当成协议包装提取出来，覆盖整篇回答，表现为正文被截断。
 * 后端若引入新包装，必须携带明确的结构标识并在进入正文前解包。
 */
export function normalizeFinalContent(payload: Record<string, unknown>): string {
  const rawContent = payload.content;
  if (typeof rawContent !== 'string') {
    return '';
  }
  return normalizeFinalDisplayText(rawContent);
}
