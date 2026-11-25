import type { McpUiMessageRequest, McpUiUpdateModelContextRequest } from '@modelcontextprotocol/ext-apps/app-bridge';

const MAX_MESSAGE_BLOCKS = 8;
const MAX_MESSAGE_BYTES = 8 * 1024;
const MAX_CONTEXT_BLOCKS = 32;
const MAX_CONTEXT_BYTES = 32 * 1024;
const MAX_SESSION_CONTEXT_BYTES = 64 * 1024;
const MAX_JSON_DEPTH = 16;
const CONTEXT_RATE_PER_SECOND = 20;
const CONTEXT_RATE_BURST = 40;
const MESSAGE_RATE_PER_MINUTE = 5;

export interface McpAppInstanceIdentity {
  sessionId: string;
  appInstanceId: string;
  server: string;
  tool: string;
  resourceUri: string;
}

export interface McpAppContextEnvelope {
  app_instance_id: string;
  server: string;
  tool: string;
  resource_uri: string;
  content?: Array<{ type: 'text'; text: string }>;
  structured_content?: Record<string, unknown>;
}

type ModelContextParams = McpUiUpdateModelContextRequest['params'];
type MessageParams = McpUiMessageRequest['params'];

interface StoredContext {
  identity: McpAppInstanceIdentity;
  context: McpAppContextEnvelope | null;
  serializedBytes: number;
  updatedAt: number;
  tokens: number;
  lastRefillAt: number;
}

interface ValidationResult<T> {
  value?: T;
  error?: string;
}

type MessageHandler = (sessionId: string, content: string) => Promise<boolean>;

const contexts = new Map<string, StoredContext>();
const pendingMessages = new Set<string>();
const messageAttempts = new Map<string, number[]>();
let messageHandler: MessageHandler | null = null;

function instanceKey(identity: Pick<McpAppInstanceIdentity, 'sessionId' | 'appInstanceId'>): string {
  return `${identity.sessionId}\u0000${identity.appInstanceId}`;
}

function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function jsonDepth(value: unknown, depth = 0): number {
  if (depth > MAX_JSON_DEPTH) return depth;
  if (value === null || typeof value !== 'object') return depth;
  const children = Array.isArray(value) ? value : Object.values(value as Record<string, unknown>);
  return children.reduce((max, child) => Math.max(max, jsonDepth(child, depth + 1)), depth);
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function normalizeTextBlocks(
  content: unknown,
  maxBlocks: number,
): ValidationResult<Array<{ type: 'text'; text: string }>> {
  if (!Array.isArray(content)) return { error: 'Only text content blocks are supported.' };
  if (content.length === 0 || content.length > maxBlocks) {
    return { error: `Expected between 1 and ${maxBlocks} text content blocks.` };
  }
  const blocks: Array<{ type: 'text'; text: string }> = [];
  for (const block of content) {
    if (!isPlainRecord(block) || block.type !== 'text' || typeof block.text !== 'string') {
      return { error: 'Only text content blocks are supported.' };
    }
    blocks.push({ type: 'text', text: block.text });
  }
  return { value: blocks };
}

export function registerMcpAppInstance(identity: McpAppInstanceIdentity, now = Date.now()): () => void {
  const key = instanceKey(identity);
  contexts.set(key, {
    identity,
    context: null,
    serializedBytes: 0,
    updatedAt: now,
    tokens: CONTEXT_RATE_BURST,
    lastRefillAt: now,
  });
  return () => {
    contexts.delete(key);
    pendingMessages.delete(key);
    messageAttempts.delete(key);
  };
}

function consumeContextToken(entry: StoredContext, now: number): boolean {
  const elapsedSeconds = Math.max(0, now - entry.lastRefillAt) / 1000;
  entry.tokens = Math.min(CONTEXT_RATE_BURST, entry.tokens + elapsedSeconds * CONTEXT_RATE_PER_SECOND);
  entry.lastRefillAt = now;
  if (entry.tokens < 1) return false;
  entry.tokens -= 1;
  return true;
}

export function updateMcpAppContext(
  identity: McpAppInstanceIdentity,
  params: ModelContextParams,
  now = Date.now(),
): ValidationResult<McpAppContextEnvelope | null> {
  const key = instanceKey(identity);
  const entry = contexts.get(key);
  if (!entry) return { error: 'The MCP App view is no longer active.' };
  if (!consumeContextToken(entry, now)) return { error: 'Model context updates are arriving too quickly.' };

  let content: Array<{ type: 'text'; text: string }> | undefined;
  if (params.content !== undefined) {
    if (Array.isArray(params.content) && params.content.length === 0) {
      content = undefined;
    } else {
      const normalized = normalizeTextBlocks(params.content, MAX_CONTEXT_BLOCKS);
      if (!normalized.value) return { error: normalized.error };
      content = normalized.value;
    }
  }

  let structuredContent: Record<string, unknown> | undefined;
  if (params.structuredContent !== undefined) {
    if (!isPlainRecord(params.structuredContent)) {
      return { error: 'structuredContent must be a JSON object.' };
    }
    if (jsonDepth(params.structuredContent) > MAX_JSON_DEPTH) {
      return { error: `structuredContent exceeds the maximum depth of ${MAX_JSON_DEPTH}.` };
    }
    structuredContent = params.structuredContent;
  }

  const hasStructuredContent = structuredContent && Object.keys(structuredContent).length > 0;
  if (!content && !hasStructuredContent) {
    entry.context = null;
    entry.serializedBytes = 0;
    entry.updatedAt = now;
    return { value: null };
  }

  const next: McpAppContextEnvelope = {
    app_instance_id: identity.appInstanceId,
    server: identity.server,
    tool: identity.tool,
    resource_uri: identity.resourceUri,
    ...(content ? { content } : {}),
    ...(hasStructuredContent ? { structured_content: structuredContent } : {}),
  };
  let serialized: string;
  try {
    serialized = JSON.stringify(next);
  } catch {
    return { error: 'Model context must contain JSON-serializable values.' };
  }
  const serializedBytes = utf8Bytes(serialized);
  if (serializedBytes > MAX_CONTEXT_BYTES) {
    return { error: `Model context exceeds ${MAX_CONTEXT_BYTES} bytes.` };
  }
  const sessionBytes = [...contexts.values()]
    .filter((candidate) => candidate.identity.sessionId === identity.sessionId && candidate !== entry)
    .reduce((total, candidate) => total + candidate.serializedBytes, serializedBytes);
  if (sessionBytes > MAX_SESSION_CONTEXT_BYTES) {
    return { error: `Session model context exceeds ${MAX_SESSION_CONTEXT_BYTES} bytes.` };
  }

  entry.context = next;
  entry.serializedBytes = serializedBytes;
  entry.updatedAt = now;
  return { value: next };
}

export function getMcpAppContexts(sessionId: string): McpAppContextEnvelope[] {
  return [...contexts.values()]
    .filter((entry) => entry.identity.sessionId === sessionId && entry.context)
    .sort(
      (left, right) =>
        left.updatedAt - right.updatedAt || left.identity.appInstanceId.localeCompare(right.identity.appInstanceId),
    )
    .map((entry) => entry.context!);
}

export function normalizeMcpAppMessage(params: MessageParams): ValidationResult<string> {
  if (params.role !== 'user') return { error: 'Only user messages are supported.' };
  const normalized = normalizeTextBlocks(params.content, MAX_MESSAGE_BLOCKS);
  if (!normalized.value) return { error: normalized.error };
  const text = normalized.value
    .map((block) => block.text)
    .join('\n\n')
    .trim();
  if (!text) return { error: 'The message is empty.' };
  if (utf8Bytes(text) > MAX_MESSAGE_BYTES) return { error: `The message exceeds ${MAX_MESSAGE_BYTES} bytes.` };
  return { value: text };
}

export function beginMcpAppMessageRequest(
  identity: McpAppInstanceIdentity,
  now = Date.now(),
): ValidationResult<() => void> {
  const key = instanceKey(identity);
  if (!contexts.has(key)) return { error: 'The MCP App view is no longer active.' };
  if (pendingMessages.has(key)) return { error: 'Another message from this MCP App is awaiting confirmation.' };
  const recent = (messageAttempts.get(key) ?? []).filter((timestamp) => now - timestamp < 60_000);
  if (recent.length >= MESSAGE_RATE_PER_MINUTE) return { error: 'Too many message requests from this MCP App.' };
  recent.push(now);
  messageAttempts.set(key, recent);
  pendingMessages.add(key);
  return {
    value: () => {
      pendingMessages.delete(key);
    },
  };
}

export function setMcpAppMessageHandler(handler: MessageHandler | null): () => void {
  messageHandler = handler;
  return () => {
    if (messageHandler === handler) messageHandler = null;
  };
}

export async function dispatchMcpAppMessage(sessionId: string, content: string): Promise<boolean> {
  if (!messageHandler) return false;
  return messageHandler(sessionId, content);
}

export function resetMcpAppContextForTests(): void {
  contexts.clear();
  pendingMessages.clear();
  messageAttempts.clear();
  messageHandler = null;
}
