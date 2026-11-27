import type { HistoryItem, DiffMeta } from "../types.js";

function now(): string {
  return new Date().toISOString();
}

export function makeItem(
  sessionId: string,
  kind: HistoryItem["kind"],
  content: string,
  icon?: string,
  meta?: Extract<HistoryItem, { kind: "info" }>["meta"],
): HistoryItem {
  const id = `${kind}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
  if (kind === "info") return { kind, id, sessionId, content, icon, meta, at: now() };
  if (kind === "error") return { kind, id, sessionId, content, at: now() };
  return { kind: "system", id, sessionId, content, at: now() };
}

export function addInfo(
  sessionId: string,
  content: string,
  icon?: string,
  meta?: Extract<HistoryItem, { kind: "info" }>["meta"],
): HistoryItem {
  return makeItem(sessionId, "info", content, icon, meta);
}

export function addError(sessionId: string, content: string): HistoryItem {
  return makeItem(sessionId, "error", content);
}

export function addDiff(
  sessionId: string,
  content: string,
  meta: DiffMeta,
): HistoryItem {
  const id = `diff-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
  return { kind: "diff", id, sessionId, content, meta, at: now() };
}

export function parseArgs(raw: string): string[] {
  return raw.trim().split(/\s+/).filter(Boolean);
}

export function formatValue(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function flattenArrayPayload(payload: unknown): unknown[] {
  if (Array.isArray(payload)) return payload;
  if (payload && typeof payload === "object") {
    const obj = payload as Record<string, unknown>;
    for (const key of ["items", "sessions", "skills", "data"]) {
      if (Array.isArray(obj[key])) return obj[key] as unknown[];
    }
  }
  return [];
}

export function extractObject(payload: unknown): Record<string, unknown> | null {
  return payload && typeof payload === "object" && !Array.isArray(payload)
    ? (payload as Record<string, unknown>)
    : null;
}
