/**
 * 前端内联工具协议清洗工具（与后端 stream_content_sanitize.py 同语义）。
 *
 * 剥离模型/网关写入 delta.content 后泄漏到 chat.final/chat.delta 中的协议串：
 *   <tool_calls_begin><tool_call_begin>function<tool_sep>tool_name{...}</tool_calls_end>
 *
 * 仅影响展示与 TTS，不修改 chatStore 持久化内容。
 */

const OPEN_TAGS = ['<tool_calls_begin>', '<tool_call_begin>'] as const;
const CLOSE_TAGS = ['</tool_calls_end>', '</tool_call_end>'] as const;
const FUNC_SEP = 'function<tool_sep>';
const TOOL_NAME_RE = /^[A-Za-z0-9_]+/;

const TOOL_WHITELIST: ReadonlySet<string> = new Set([
  'todo_create',
  'todo_complete',
  'todo_insert',
  'todo_remove',
  'todo_list',
]);

/** 前缀兜底：未来新增 todo_* 内联工具时无需同步白名单（review 意见）。 */
const TOOL_NAME_PREFIXES: ReadonlyArray<string> = ['todo_'];

function findBalancedJsonEnd(text: string, start: number): number {
  let depth = 0;
  let inString = false;
  let escapeNext = false;
  const n = text.length;

  for (let i = start; i < n; i++) {
    const ch = text[i];
    if (escapeNext) {
      escapeNext = false;
      continue;
    }
    if (inString) {
      if (ch === '\\') { escapeNext = true; }
      else if (ch === '"') { inString = false; }
      continue;
    }
    if (ch === '"') {
      inString = true;
    } else if (ch === '{' || ch === '[') {
      depth++;
    } else if (ch === '}' || ch === ']') {
      depth--;
      if (depth === 0) return i + 1;
    }
  }
  return -1;
}

function findEarliestOpenTag(text: string, from = 0): { pos: number; tag: string } {
  let bestPos = -1;
  let bestTag = '';
  for (const tag of OPEN_TAGS) {
    const pos = text.indexOf(tag, from);
    if (pos !== -1 && (bestPos === -1 || pos < bestPos)) {
      bestPos = pos;
      bestTag = tag;
    }
  }
  return { pos: bestPos, tag: bestTag };
}

function findFuncSep(text: string, from = 0): number {
  return text.indexOf(FUNC_SEP, from);
}

function findCloseTags(text: string, from: number): number {
  let pos = from;
  let foundAny = false;
  while (true) {
    let bestIdx = -1;
    let bestTag = '';
    for (const tag of CLOSE_TAGS) {
      const idx = text.indexOf(tag, pos);
      if (idx !== -1 && (bestIdx === -1 || idx < bestIdx)) {
        bestIdx = idx;
        bestTag = tag;
      }
    }
    if (bestIdx === -1) break;
    const gap = text.slice(pos, bestIdx);
    if (foundAny && gap.trim()) break;
    pos = bestIdx + bestTag.length;
    foundAny = true;
  }
  return foundAny ? pos : -1;
}

function toolNameAllowed(name: string): boolean {
  return (
    TOOL_WHITELIST.has(name) ||
    TOOL_NAME_PREFIXES.some((prefix) => name.startsWith(prefix))
  );
}

function toolNameMayBeIncompletePrefix(name: string): boolean {
  return Array.from(TOOL_WHITELIST).some((tool) => tool.startsWith(name) && tool !== name);
}

function hasExplicitInlineToolTags(text: string): boolean {
  return (
    OPEN_TAGS.some((tag) => text.includes(tag)) ||
    CLOSE_TAGS.some((tag) => text.includes(tag)) ||
    text.includes(FUNC_SEP)
  );
}

function stripOnePass(text: string): string {
  let pos = 0;
  const n = text.length;

  while (pos < n) {
    const { pos: tagPos, tag: openTag } = findEarliestOpenTag(text, pos);
    const funcPos = findFuncSep(text, pos);

    if (tagPos === -1 && funcPos === -1) break;

    const useOuter = tagPos !== -1 && (funcPos === -1 || tagPos <= funcPos);

    if (useOuter) {
      const anchor = tagPos;
      const innerFunc = findFuncSep(text, anchor);

      if (innerFunc === -1) {
        const closeEnd = findCloseTags(text, anchor + openTag.length);
        if (closeEnd !== -1) {
          return text.slice(0, anchor) + text.slice(closeEnd);
        }
        return text.slice(0, anchor);
      }

      const nameStart = innerFunc + FUNC_SEP.length;
      const nameMatch = TOOL_NAME_RE.exec(text.slice(nameStart));
      if (!nameMatch) { pos = innerFunc + 1; continue; }
      const toolName = nameMatch[0];
      if (!toolNameAllowed(toolName)) {
        if (toolNameMayBeIncompletePrefix(toolName)) {
          return text.slice(0, anchor);
        }
        pos = innerFunc + 1;
        continue;
      }

      const jsonStart = nameStart + toolName.length;
      if (jsonStart >= n || text[jsonStart] !== '{') { pos = jsonStart; continue; }

      const jsonEnd = findBalancedJsonEnd(text, jsonStart);
      if (jsonEnd === -1) {
        return text.slice(0, anchor);
      }
      const closeEnd = findCloseTags(text, jsonEnd);
      const segEnd = closeEnd !== -1 ? closeEnd : jsonEnd;
      return text.slice(0, anchor) + text.slice(segEnd);
    } else {
      const anchor = funcPos;
      const nameStart = anchor + FUNC_SEP.length;
      const nameMatch = TOOL_NAME_RE.exec(text.slice(nameStart));
      if (!nameMatch) { pos = anchor + 1; continue; }
      const toolName = nameMatch[0];
      if (!toolNameAllowed(toolName)) {
        if (toolNameMayBeIncompletePrefix(toolName)) {
          return text.slice(0, anchor);
        }
        pos = anchor + 1;
        continue;
      }

      const jsonStart = nameStart + toolName.length;
      if (jsonStart >= n || text[jsonStart] !== '{') { pos = jsonStart; continue; }

      const jsonEnd = findBalancedJsonEnd(text, jsonStart);
      if (jsonEnd === -1) {
        return text.slice(0, anchor);
      }
      // 裸 function<tool_sep> 形态（无前置开标签）也要消费其后的孤儿闭合标签，
      // 否则残留 "</tool_call_end></tool_calls_end>" 到正文。
      const closeEnd = findCloseTags(text, jsonEnd);
      return text.slice(0, anchor) + text.slice(closeEnd !== -1 ? closeEnd : jsonEnd);
    }
  }
  return text;
}

export function stripInlineToolProtocol(text: string): string {
  if (!text) return text;
  let result = text;
  let changed = true;
  while (changed) {
    const next = stripOnePass(result);
    changed = next !== result;
    result = next;
  }
  return result;
}

export function stripResidualInlineToolProtocol(text: string): string {
  if (!text || !hasExplicitInlineToolTags(text)) {
    return text;
  }
  return stripInlineToolProtocol(text);
}

const MAX_PROTOCOL_MARKER_LEN = Math.max(
  ...OPEN_TAGS.map((tag) => tag.length),
  ...CLOSE_TAGS.map((tag) => tag.length),
  FUNC_SEP.length,
);

/**
 * 流式 delta 剥离器（有状态）：协议标记被 SSE 拆到两个 chunk 中间时，
 * 无状态的 stripInlineToolProtocol 命不中。这里把"可能是未完整标记前缀"的
 * 尾部扣住，拼到下一个 chunk 再剥；流结束时的残留由 chat.final 的
 * normalizeFinalContent（stripResidualInlineToolProtocol）整体覆盖兜底。
 */
export function createStreamDeltaStripper(): (chunk: string) => string {
  const markers: ReadonlyArray<string> = [...OPEN_TAGS, ...CLOSE_TAGS, FUNC_SEP];
  let held = '';
  return (chunk: string): string => {
    let text = held + chunk;
    held = '';
    if (text) {
      const scanFrom = Math.max(0, text.length - MAX_PROTOCOL_MARKER_LEN);
      for (let i = scanFrom; i < text.length; i++) {
        const tail = text.slice(i);
        if (markers.some((marker) => marker.length > tail.length && marker.startsWith(tail))) {
          held = tail;
          text = text.slice(0, i);
          break;
        }
      }
    }
    if (!text) return '';
    return stripInlineToolProtocol(text);
  };
}
