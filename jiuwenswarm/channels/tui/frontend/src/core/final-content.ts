function decodeQuotedPythonLikeString(raw: string): string {
  return raw
    .replace(/\\r/g, "\r")
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, "\t")
    .replace(/\\'/g, "'")
    .replace(/\\"/g, '"')
    .replace(/\\\\/g, "\\");
}

function normalizeFinalDisplayText(text: string): string {
  return text.replace(/^(?:\r?\n)+/, "");
}

export function normalizeFinalContent(payload: Record<string, unknown>): string {
  const rawContent = payload.content;
  if (typeof rawContent !== "string") {
    return "";
  }

  const trimmed = rawContent.trim();

  if (trimmed.startsWith("{") && trimmed.endsWith("}")) {
    try {
      const parsed = JSON.parse(trimmed) as Record<string, unknown>;
      if (typeof parsed.output === "string") {
        return normalizeFinalDisplayText(parsed.output);
      }
    } catch {
      // Ignore and fall through to Python-dict compatibility.
    }
  }

  if (!trimmed.includes("result_type") || !trimmed.includes("output")) {
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
