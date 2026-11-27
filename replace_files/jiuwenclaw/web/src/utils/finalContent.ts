/**
 * 将 chat.final 的 payload 规范为可展示的纯文本（与实时 WS 处理一致）。
 */

function decodeQuotedPythonLikeString(raw: string): string {
  return raw
    .replace(/\\r/g, '\r')
    .replace(/\\n/g, '\n')
    .replace(/\\t/g, '\t')
    .replace(/\\'/g, "'")
    .replace(/\\"/g, '"')
    .replace(/\\\\/g, '\\');
}

function normalizeFinalDisplayText(text: string): string {
  return text.replace(/^(?:\r?\n)+/, '');
}

export function normalizeFinalContent(payload: Record<string, unknown>): string {
  const rawContent = payload.content;
  if (typeof rawContent !== 'string') {
    return '';
  }

  const trimmed = rawContent.trim();

  if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
    try {
      const parsed = JSON.parse(trimmed) as Record<string, unknown>;
      if (typeof parsed.output === 'string') {
        return normalizeFinalDisplayText(parsed.output);
      }
    } catch {
      // ignore: 继续尝试 Python dict 风格兼容解析
    }
  }

  if (!trimmed.includes('result_type') || !trimmed.includes('output')) {
    // 处理嵌套在 delta 中的 chat.final 格式
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
    return normalizeFinalDisplayText(rawContent);
  }

  const singleQuoted = rawContent.match(/['"]output['"]\s*:\s*'((?:\\'|[^'])*)'/s);
  if (singleQuoted?.[1] != null) {
    return normalizeFinalDisplayText(decodeQuotedPythonLikeString(singleQuoted[1]));
  }

  const doubleQuoted = rawContent.match(/['"]output['"]\s*:\s*"((?:\\"|[^"])*)"/s);
  if (doubleQuoted?.[1] != null) {
    return normalizeFinalDisplayText(decodeQuotedPythonLikeString(doubleQuoted[1]));
  }

  return normalizeFinalDisplayText(rawContent);
}
