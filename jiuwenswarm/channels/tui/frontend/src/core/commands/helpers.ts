import type { HistoryItem, DiffMeta } from "../types.js";
import type { CommandContext } from "./types.js";

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
  if (kind === "command_echo") return { kind, id, sessionId, content, at: now() };
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

export function addCommandEcho(sessionId: string, content: string): HistoryItem {
  return makeItem(sessionId, "command_echo", content);
}

export interface ReusableEmptySession {
  session_id: string;
  message_count: number;
  created_at?: number;
  mode?: string;
  accent_color?: string;
  active_in_window?: boolean;
}

export function findReusableEmptySession(
  sessions: unknown,
  excludedSessionId: string,
): ReusableEmptySession | undefined {
  if (!Array.isArray(sessions)) return undefined;
  const candidates = sessions.filter(
    (session): session is ReusableEmptySession =>
      session !== null &&
      typeof session === "object" &&
      !Array.isArray(session) &&
      typeof session.session_id === "string" &&
      session.session_id.trim() !== excludedSessionId &&
      session.message_count === 0 &&
      session.active_in_window !== true,
  );
  return candidates.reduce<ReusableEmptySession | undefined>(
    (latest, session) =>
      !latest || (session.created_at ?? 0) > (latest.created_at ?? 0) ? session : latest,
    undefined,
  );
}

/** Read every server page; the TUI gateway filters each page after fetching it. */
export async function listAllActiveSessions(request: CommandContext["request"]): Promise<unknown[]> {
  const sessions: unknown[] = [];
  for (let offset = 0; ; offset += 200) {
    const page = await request<{ sessions?: unknown; total?: number; page_count?: number }>("session.list", {
      include_current: true,
      limit: 200,
      offset,
    });
    if (Array.isArray(page.sessions)) sessions.push(...page.sessions);
    const pageCount = page.page_count ?? (Array.isArray(page.sessions) ? page.sessions.length : 0);
    if (typeof page.total === "number" ? offset + 200 >= page.total : pageCount < 200) break;
  }
  return sessions;
}

export async function listAllArchivedSessions(request: CommandContext["request"]): Promise<unknown[]> {
  const sessions: unknown[] = [];
  for (let offset = 0; ; offset += 200) {
    const page = await request<{ sessions?: unknown; has_more?: boolean }>("session.archived.list", {
      limit: 200,
      offset,
    });
    if (Array.isArray(page.sessions)) sessions.push(...page.sessions);
    if (!page.has_more) break;
  }
  return sessions;
}

export function addDiff(sessionId: string, content: string, meta: DiffMeta): HistoryItem {
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
