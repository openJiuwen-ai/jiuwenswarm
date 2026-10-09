import { DESIGNER_PREVIEW_GRAPH_ID } from './designerBootstrapGraph';
import { DESIGNER_NODE_ROLE_BRIEF, type DesignerExecutionGraph } from './executionGraphTypes';
import type { DesignerChatMedia } from './designerChatStore';
import {
  extractDesignerGraphReferences,
  type DesignerStoredReference,
} from './designerReferences';

export { extractDesignerGraphReferences };

export type DesignerStoredChatMessage = {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  kind: 'user' | 'thinking' | 'bootstrap_done' | 'bootstrap_error' | 'chat_ack' | 'chat_error' | 'not_implemented';
  createdAt: number;
  references?: DesignerStoredReference[];
  /** Generated outputs shown inline; stored as node id + uri (never blobs) so
   * the capped localStorage transcript stays small. */
  media?: DesignerChatMedia[];
};

export const DESIGNER_CHAT_STORAGE_KEY = 'jiuwenswarm_designer_chat_by_graph';
export const DESIGNER_CHAT_STORAGE_VERSION = 1;
export const DESIGNER_CHAT_MAX_GRAPHS = 40;
export const DESIGNER_CHAT_MAX_MESSAGES = 50;

export type DesignerChatStorage = {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem?(key: string): void;
};

type PersistedDesignerChat = {
  v: number;
  order: string[];
  byId: Record<string, DesignerStoredChatMessage[]>;
};

const MESSAGE_KINDS = new Set([
  'user',
  'thinking',
  'bootstrap_done',
  'bootstrap_error',
  'chat_ack',
  'chat_error',
  'not_implemented',
]);

const MESSAGE_ROLES = new Set(['user', 'assistant', 'system']);

export function isDesignerChatPreviewGraphId(graphId: string | null | undefined): boolean {
  const id = String(graphId ?? '').trim();
  return id === DESIGNER_PREVIEW_GRAPH_ID || id.startsWith('preview_');
}

export function extractDesignerGraphPrompt(
  graph: Pick<DesignerExecutionGraph, 'description' | 'nodes'> | null | undefined,
): string {
  const brief = (graph?.nodes || []).find((node) => node?.config?.role === DESIGNER_NODE_ROLE_BRIEF);
  const fromBrief = String(brief?.config?.prompt ?? '').trim();
  if (fromBrief) return fromBrief;
  return String(graph?.description ?? '').trim();
}

export function hasDesignerUserPrompt(
  messages: Array<Pick<DesignerStoredChatMessage, 'kind' | 'content' | 'references'>> | null | undefined,
): boolean {
  return (messages || []).some(
    (item) =>
      item?.kind === 'user' &&
      (String(item.content || '').trim() || (item.references || []).length > 0),
  );
}

export function resolveBoundDesignerMessages(input: {
  stored?: DesignerStoredChatMessage[] | null;
  pending?: DesignerStoredChatMessage[] | null;
}): DesignerStoredChatMessage[] {
  const stored = sanitizeDesignerChatMessages(input.stored);
  if (stored.length > 0) return stored;
  return sanitizeDesignerChatMessages(input.pending, { keepThinking: true });
}

export function sanitizeDesignerChatMessages(
  messages: DesignerStoredChatMessage[] | null | undefined,
  options?: { keepThinking?: boolean },
): DesignerStoredChatMessage[] {
  const keepThinking = Boolean(options?.keepThinking);
  const out: DesignerStoredChatMessage[] = [];
  for (const raw of messages || []) {
    if (!raw || typeof raw !== 'object') continue;
    const kind = String(raw.kind || '');
    const role = String(raw.role || '');
    const content = String(raw.content ?? '');
    if (!MESSAGE_KINDS.has(kind) || !MESSAGE_ROLES.has(role)) continue;
    if (!keepThinking && kind === 'thinking') continue;
    const references = sanitizeStoredReferences(raw.references);
    const media = sanitizeStoredMedia(raw.media);
    if (!content.trim() && references.length === 0 && media.length === 0) continue;
    out.push({
      id: String(raw.id || '').trim() || `seed-${out.length}`,
      role: role as DesignerStoredChatMessage['role'],
      content,
      kind: kind as DesignerStoredChatMessage['kind'],
      createdAt: Number.isFinite(Number(raw.createdAt)) ? Number(raw.createdAt) : 0,
      ...(references.length > 0 ? { references } : {}),
      ...(media.length > 0 ? { media } : {}),
    });
    if (out.length >= DESIGNER_CHAT_MAX_MESSAGES) break;
  }
  return out;
}

function defaultStorage(): DesignerChatStorage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function parsePersisted(raw: string | null): PersistedDesignerChat {
  if (!raw) return { v: DESIGNER_CHAT_STORAGE_VERSION, order: [], byId: {} };
  try {
    const parsed = JSON.parse(raw) as Partial<PersistedDesignerChat> | Record<string, DesignerStoredChatMessage[]>;
    if (parsed && typeof parsed === 'object' && 'byId' in parsed) {
      const byId: Record<string, DesignerStoredChatMessage[]> = {};
      const order: string[] = [];
      const source = (parsed as PersistedDesignerChat).byId || {};
      const listed = Array.isArray((parsed as PersistedDesignerChat).order)
        ? (parsed as PersistedDesignerChat).order
        : Object.keys(source);
      for (const graphId of listed) {
        const id = String(graphId || '').trim();
        if (!id || isDesignerChatPreviewGraphId(id) || byId[id]) continue;
        const messages = sanitizeDesignerChatMessages(source[id]);
        if (messages.length === 0) continue;
        byId[id] = messages;
        order.push(id);
      }
      return { v: DESIGNER_CHAT_STORAGE_VERSION, order, byId };
    }
  } catch {
    // Corrupt storage should not blank the canvas chat.
  }
  return { v: DESIGNER_CHAT_STORAGE_VERSION, order: [], byId: {} };
}

export function readPersistedDesignerChat(
  storage: DesignerChatStorage | null = defaultStorage(),
): Record<string, DesignerStoredChatMessage[]> {
  if (!storage) return {};
  try {
    return parsePersisted(storage.getItem(DESIGNER_CHAT_STORAGE_KEY)).byId;
  } catch {
    return {};
  }
}

export function persistDesignerChat(
  messagesByGraphId: Record<string, DesignerStoredChatMessage[]>,
  storage: DesignerChatStorage | null = defaultStorage(),
): Record<string, DesignerStoredChatMessage[]> {
  const previous = storage ? parsePersisted(storage.getItem(DESIGNER_CHAT_STORAGE_KEY)) : {
    v: DESIGNER_CHAT_STORAGE_VERSION,
    order: [],
    byId: {},
  };
  let order = previous.order.filter((id) => !isDesignerChatPreviewGraphId(id));
  const byId: Record<string, DesignerStoredChatMessage[]> = { ...previous.byId };

  for (const [rawId, messages] of Object.entries(messagesByGraphId || {})) {
    const id = String(rawId || '').trim();
    if (!id || isDesignerChatPreviewGraphId(id)) continue;
    const clean = sanitizeDesignerChatMessages(messages);
    if (clean.length === 0) continue;
    byId[id] = clean;
    order = order.filter((item) => item !== id);
    order.push(id);
  }

  if (order.length > DESIGNER_CHAT_MAX_GRAPHS) {
    order = order.slice(-DESIGNER_CHAT_MAX_GRAPHS);
  }
  const pruned: Record<string, DesignerStoredChatMessage[]> = {};
  for (const id of order) {
    if (byId[id]) pruned[id] = byId[id];
  }

  if (!storage) return pruned;
  try {
    storage.setItem(
      DESIGNER_CHAT_STORAGE_KEY,
      JSON.stringify({ v: DESIGNER_CHAT_STORAGE_VERSION, order, byId: pruned }),
    );
  } catch {
    // Quota or private-mode failures should not break chat.
  }
  return pruned;
}

function sanitizeStoredReferences(raw: DesignerStoredReference[] | unknown): DesignerStoredReference[] {
  if (!Array.isArray(raw)) return [];
  return extractDesignerGraphReferences({ metadata: { user_references: raw } });
}

const MAX_STORED_MEDIA = 8;

/** Rebuild a message's inline media from persisted JSON, dropping anything
 * malformed. Only ids + URIs are kept, never image data. */
function sanitizeStoredMedia(raw: unknown): DesignerChatMedia[] {
  if (!Array.isArray(raw)) return [];
  const out: DesignerChatMedia[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const record = item as Record<string, unknown>;
    const nodeId = String(record.nodeId ?? '').trim();
    const uri = String(record.uri ?? '').trim();
    if (!nodeId || !uri) continue;
    const kind = String(record.kind ?? 'image').trim() || 'image';
    const label = String(record.label ?? '').trim();
    out.push({ nodeId, uri, kind, ...(label ? { label } : {}) });
    if (out.length >= MAX_STORED_MEDIA) break;
  }
  return out;
}
