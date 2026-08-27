import { summarize } from "../../rendering/text.js";
import { isCodeGraphTool, summarizePath } from "./tool-kind-utils.js";
import { isPlainObject, tryParseStructuredText } from "./tool-structured-data.js";

export const MAX_VISIBLE_TOOLS = 3;

export * from "./tool-kind-utils.js";
export * from "./tool-line-renderers.js";
export * from "./tool-structured-data.js";

function summarizeStructuredPayload(value: string): string | undefined {
  const parsed = tryParseStructuredText(value);
  if (Array.isArray(parsed)) return `${parsed.length} item${parsed.length === 1 ? "" : "s"}`;
  if (isPlainObject(parsed)) {
    const keys = Object.keys(parsed);
    return `${keys.length} field${keys.length === 1 ? "" : "s"}`;
  }
  return undefined;
}

function listCount(parsed: Record<string, unknown>, key: string): number | undefined {
  const value = parsed[key];
  return Array.isArray(value) ? value.length : undefined;
}

function summarizeCodeGraphResult(result: string): string | undefined {
  const parsed = tryParseStructuredText(result);
  if (!isPlainObject(parsed)) return undefined;
  const status = typeof parsed.status === "string" ? parsed.status : undefined;
  const indexState = typeof parsed.index_state === "string" ? parsed.index_state : undefined;
  const file =
    typeof parsed.file === "string"
      ? parsed.file
      : typeof parsed.path === "string"
        ? parsed.path
        : undefined;
  const counts: string[] = [];
  for (const key of ["matches", "symbols", "related", "paths", "definitions", "chunks", "candidates"]) {
    const n = listCount(parsed, key);
    if (n !== undefined) counts.push(`${n} ${key}`);
  }
  const parts: string[] = [];
  if (status) parts.push(status);
  if (indexState && indexState !== "READY" && indexState !== status) parts.push(indexState);
  parts.push(...counts);
  if (file) parts.push(summarizePath(file) ?? file);
  const message = typeof parsed.message === "string" ? parsed.message : undefined;
  if (message && (status === "UNAVAILABLE" || status === "ERROR" || status === "NO_MATCH")) {
    parts.push(summarize(message, 48));
  }
  return parts.length > 0 ? parts.join(" · ") : undefined;
}

export function summarizeToolResultByKind(name: string, result: string): string | undefined {
  const normalized = name.toLowerCase();
  if (isCodeGraphTool(name)) {
    return summarizeCodeGraphResult(result) ?? summarizeStructuredPayload(result);
  }
  const lines = result.split("\n").filter(Boolean).length;
  if (normalized.includes("read") || normalized.includes("view")) return `${lines} lines loaded`;
  if (normalized.includes("search") || normalized.includes("grep")) return `${lines} matches`;
  if (normalized.includes("fetch") || normalized.includes("webpage"))
    return `${lines} lines fetched`;
  if (normalized.includes("edit") || normalized.includes("write") || normalized.includes("patch")) {
    return "edit applied";
  }
  if (
    normalized.includes("exec") ||
    normalized.includes("bash") ||
    normalized.includes("shell") ||
    normalized.includes("command")
  ) {
    return summarize(result.split("\n")[0] ?? "", 88);
  }
  return summarizeStructuredPayload(result);
}
