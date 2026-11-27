import type { ToolCallDisplay } from "../../../core/types.js";
import { summarize } from "../../rendering/text.js";
import { getStringArg, isEditTool, isWriteTool } from "./tool-kind-utils.js";

function normalizePythonLiteralToJson(value: string): string {
  return value
    .replace(/\bNone\b/g, "null")
    .replace(/\bTrue\b/g, "true")
    .replace(/\bFalse\b/g, "false")
    .replace(/'([^'\\]*(?:\\.[^'\\]*)*)'/g, (_match, content: string) => {
      const normalized = content.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
      return `"${normalized}"`;
    });
}

function parseLiteralFragment(value: string): unknown {
  const trimmed = value.trim();
  if (!trimmed || trimmed === "None") return null;
  try {
    return JSON.parse(trimmed);
  } catch {}
  try {
    return JSON.parse(normalizePythonLiteralToJson(trimmed));
  } catch {
    return trimmed;
  }
}

export function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function parseProtocolWrapper(value: string): unknown | undefined {
  if (!value.includes("data=") && !value.includes("success=") && !value.includes("error=")) {
    return undefined;
  }

  let working = value.trim();
  let errorValue: unknown = undefined;
  const errorIndex = working.lastIndexOf(" error=");
  if (errorIndex >= 0) {
    errorValue = parseLiteralFragment(working.slice(errorIndex + " error=".length));
    working = working.slice(0, errorIndex);
  }

  let dataValue: unknown = undefined;
  const dataIndex = working.indexOf(" data=");
  if (dataIndex >= 0) {
    dataValue = parseLiteralFragment(working.slice(dataIndex + " data=".length));
    working = working.slice(0, dataIndex);
  }

  const successMatch = /\bsuccess=(True|False|true|false)\b/.exec(working);
  const successValue =
    successMatch?.[1] !== undefined ? successMatch[1].toLowerCase() === "true" : undefined;

  if (isPlainObject(dataValue)) {
    return {
      ...dataValue,
      ...(successValue !== undefined ? { success: successValue } : {}),
      ...(errorValue !== undefined ? { error: errorValue } : {}),
    };
  }
  if (Array.isArray(dataValue)) {
    return {
      items: dataValue,
      count: dataValue.length,
      ...(successValue !== undefined ? { success: successValue } : {}),
      ...(errorValue !== undefined ? { error: errorValue } : {}),
    };
  }
  if (dataValue !== undefined || successValue !== undefined || errorValue !== undefined) {
    return {
      ...(successValue !== undefined ? { success: successValue } : {}),
      ...(errorValue !== undefined ? { error: errorValue } : {}),
      ...(dataValue !== undefined ? { result: dataValue } : {}),
    };
  }
  return undefined;
}

export function tryParseStructuredText(value: string): unknown {
  const trimmed = value.trim();
  if (!trimmed) return value;
  const wrapped = parseProtocolWrapper(trimmed);
  if (wrapped !== undefined) return wrapped;
  if (!["{", "["].includes(trimmed[0] ?? "")) return value;
  try {
    return JSON.parse(trimmed);
  } catch {
    try {
      return JSON.parse(normalizePythonLiteralToJson(trimmed));
    } catch {
      return value;
    }
  }
}

function isPrimitive(value: unknown): value is string | number | boolean | null {
  return (
    value === null ||
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  );
}

function formatLeafValue(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string") {
    const compact = value.replace(/\s+/g, " ").trim();
    return compact ? summarize(compact, 96) : '""';
  }
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return summarize(JSON.stringify(value), 96);
}

export function formatStructuredValue(
  value: unknown,
  maxLines: number,
  maxDepth: number,
): { lines: string[]; truncated: number } {
  const output: string[] = [];
  const append = (line: string): boolean => {
    if (output.length >= maxLines) return false;
    output.push(line);
    return true;
  };

  const visit = (current: unknown, indent: string, label?: string, depth = 0): void => {
    if (output.length >= maxLines) return;
    const parsed = typeof current === "string" ? tryParseStructuredText(current) : current;

    if (isPrimitive(parsed)) {
      if (typeof parsed === "string" && parsed.includes("\n")) {
        const compactLines = parsed
          .split("\n")
          .map((line) => line.trimEnd())
          .filter((line) => line.length > 0);
        if (label && !append(`${indent}${label}:`)) return;
        const visible = compactLines.slice(0, Math.max(1, maxLines - output.length));
        for (const line of visible) {
          if (!append(`${indent}  ${summarize(line, 96)}`)) return;
        }
        return;
      }
      append(`${indent}${label ? `${label}: ` : ""}${formatLeafValue(parsed)}`);
      return;
    }

    if (depth >= maxDepth) {
      append(`${indent}${label ? `${label}: ` : ""}${Array.isArray(parsed) ? "[…]" : "{…}"}`);
      return;
    }

    if (Array.isArray(parsed)) {
      if (label && !append(`${indent}${label}:`)) return;
      if (parsed.length === 0) {
        append(`${indent}${label ? "  " : ""}[]`);
        return;
      }
      const childIndent = indent + (label ? "  " : "");
      for (const item of parsed) {
        if (output.length >= maxLines) return;
        if (isPrimitive(item)) {
          append(`${childIndent}- ${formatLeafValue(item)}`);
          continue;
        }
        if (!append(`${childIndent}-`)) return;
        visit(item, `${childIndent}  `, undefined, depth + 1);
      }
      return;
    }

    if (isPlainObject(parsed)) {
      const entries = Object.entries(parsed).filter(([, item]) => item !== undefined);
      if (label && !append(`${indent}${label}:`)) return;
      if (entries.length === 0) {
        append(`${indent}${label ? "  " : ""}{}`);
        return;
      }
      const childIndent = indent + (label ? "  " : "");
      for (const [key, item] of entries) {
        visit(item, childIndent, key, depth + 1);
        if (output.length >= maxLines) return;
      }
      return;
    }

    append(`${indent}${label ? `${label}: ` : ""}${summarize(String(parsed), 96)}`);
  };

  visit(value, "");
  const truncated = Math.max(0, output.length - maxLines);
  return { lines: output.slice(0, maxLines), truncated };
}

export function extractTrailingBracketNotices(text: string): {
  mainLines: string[];
  notices: string[];
} {
  const lines = text.split("\n");
  const notices: string[] = [];
  let end = lines.length;
  while (end > 0) {
    const current = lines[end - 1]?.trim() ?? "";
    if (current.startsWith("[") && current.endsWith("]")) {
      notices.unshift(current);
      end -= 1;
      continue;
    }
    break;
  }
  return { mainLines: lines.slice(0, end).filter((line) => line.length > 0), notices };
}

export function parseToolResultPayload(tool: ToolCallDisplay): Record<string, unknown> | undefined {
  if (!tool.result) return undefined;
  const parsed = tryParseStructuredText(tool.result);
  return isPlainObject(parsed) ? parsed : undefined;
}

export function parseToolResultValue(tool: ToolCallDisplay): unknown {
  if (!tool.result) return undefined;
  return tryParseStructuredText(tool.result);
}

export function parseFetchResult(tool: ToolCallDisplay): {
  url?: string;
  status?: string;
  title?: string;
  contentLines: string[];
} {
  if (!tool.result) return { contentLines: [] };
  const parsed = parseToolResultPayload(tool);
  if (parsed) {
    return {
      url: getStringArg(parsed, "url"),
      status:
        typeof parsed.status_code === "number"
          ? String(parsed.status_code)
          : getStringArg(parsed, "status", "status_code"),
      title: getStringArg(parsed, "title"),
      contentLines: nonEmptyLines(parsed.content),
    };
  }
  const lines = tool.result.split("\n").map((line) => line.trimEnd());
  const contentStartIndex = lines.findIndex((line) => line.trim().toLowerCase() === "content:");
  return {
    url: lines
      .find((line) => line.startsWith("URL:"))
      ?.slice(4)
      .trim(),
    status: lines
      .find((line) => line.startsWith("Status:"))
      ?.slice(7)
      .trim(),
    title: lines
      .find((line) => line.startsWith("Title:"))
      ?.slice(6)
      .trim(),
    contentLines:
      contentStartIndex >= 0
        ? lines.slice(contentStartIndex + 1).filter((line) => line.trim().length > 0)
        : nonEmptyLines(tool.result),
  };
}

export function getStringList(payload: Record<string, unknown>, ...keys: string[]): string[] {
  for (const key of keys) {
    const value = payload[key];
    if (Array.isArray(value)) {
      const list = value.filter(
        (item): item is string => typeof item === "string" && item.length > 0,
      );
      if (list.length > 0) return list;
    }
  }
  return [];
}

export function getStringListFromValue(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.length > 0);
}

export function nonEmptyLines(value: unknown): string[] {
  if (typeof value !== "string") return [];
  return value
    .split("\n")
    .map((line) => line.trimEnd())
    .filter((line) => line.trim().length > 0);
}

export function shouldRenderStructuredPayloadByDefault(name: string): boolean {
  return isWriteTool(name) || isEditTool(name);
}
