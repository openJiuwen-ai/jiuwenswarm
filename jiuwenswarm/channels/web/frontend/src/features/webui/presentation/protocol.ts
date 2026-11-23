import { isRecord, utf8ByteLength } from '../protocol/utils.ts';

export const PRESENTATION_PROTOCOL = 'workswarm.presentation';
export const PRESENTATION_VERSION = 1;
export const PRESENTATION_SLOT = 'application.presentation';

export interface WindowTransitionOptions { durationMs: number }
export interface PresentationWorkArea { x: number; y: number; width: number; height: number }
export interface WindowTransition {
  id: string;
  direction: 'compact' | 'workspace';
  durationMs: number;
  image: string;
  viewport: { width: number; height: number };
  workspace: { x: number; y: number; width: number; height: number };
  surface: { x: number; y: number; width: number; height: number };
  workArea: PresentationWorkArea;
}

export function isWindowTransitionOptions(value: unknown): value is WindowTransitionOptions {
  return isRecord(value) && Object.keys(value).join(',') === 'durationMs'
    && typeof value.durationMs === 'number' && Number.isSafeInteger(value.durationMs)
    && value.durationMs >= 200 && value.durationMs <= 2000;
}

export type PresentationAction = 'select' | 'draft' | 'send' | 'stop' | 'reply' | 'model' | 'workspace' | 'resize' | 'move' | 'hitTest';
export interface PresentationRequest {
  protocol: typeof PRESENTATION_PROTOCOL;
  version: 1;
  type: 'request';
  id: number;
  action: PresentationAction;
  params: Record<string, unknown>;
}
export interface PresentationResult {
  protocol: typeof PRESENTATION_PROTOCOL;
  version: 1;
  type: 'result';
  id: number;
  ok: boolean;
  value?: unknown;
  error?: { code: string; message: string };
}
export type PresentationActivity = 'idle' | 'thinking' | 'toolRunning' | 'replying' | 'needsInput' | 'failed' | 'paused' | 'done';
export interface PresentationConversation {
  conversationKey: string;
  sessionId: string;
  teamId: string | null;
  title: string;
  preview: string;
  draft: string;
  activity: PresentationActivity;
  toolNames: string[];
  canSend: boolean;
  mode: string;
  modelId: string | null;
  latestUserMessage: string;
  lastUserMessageAt: number | null;
  userMessageId: number | null;
  replyId: number | null;
  inputMessageId: string | null;
  replyToMessageId: string | null;
  inputOutcome: 'failed' | 'stopped' | null;
}
export interface PresentationState {
  locale: string;
  connected: boolean;
  selectedConversationKey: string | null;
  conversations: PresentationConversation[];
  theme: Record<string, string>;
  reducedMotion: boolean;
  models: { id: string; label: string }[];
}
export interface ApplicationSurfaceConfig {
  key: string;
  src: string;
  resourceUrl: string;
  title: string;
  width: number;
  height: number;
  config: Record<string, unknown>;
  state: PresentationState;
}
export interface PresentationEnvelope {
  generation: string;
  key: string;
  request: unknown;
}
export interface PresentationNativeApi {
  open: (config: ApplicationSurfaceConfig) => Promise<{ generation: string }>;
  update: (key: string, payload: { config?: Record<string, unknown>; state?: PresentationState }) => Promise<boolean>;
  close: (key: string) => Promise<boolean>;
  transitionWorkspace: (key: string, options: WindowTransitionOptions) => Promise<boolean>;
  respond: (generation: string, result: PresentationResult) => Promise<boolean>;
  resize: (key: string, width: number, height: number) => Promise<boolean>;
  move: (key: string, deltaX: number, deltaY: number) => Promise<boolean>;
  hitTest: (key: string, interactive: boolean) => Promise<boolean>;
  onRequest: (callback: (envelope: PresentationEnvelope) => void) => () => void;
  onClosed: (callback: (event: { generation: string; key: string; reason: string }) => void) => () => void;
}
export interface PresentationWindowApi {
  bootstrap: () => Promise<ApplicationSurfaceConfig & { generation: string; initialWorkArea: PresentationWorkArea }>;
  ready: (options?: WindowTransitionOptions) => Promise<true | WindowTransition>;
  startTransition: (id: string) => Promise<boolean>;
  finishTransition: (id: string) => Promise<boolean>;
  onTransition: (callback: (transition: WindowTransition) => void) => () => void;
  failed: () => Promise<boolean>;
  request: (request: PresentationRequest) => Promise<boolean>;
  onUpdate: (callback: (payload: { state?: PresentationState; config?: Record<string, unknown> }) => void) => () => void;
  onResult: (callback: (result: PresentationResult) => void) => () => void;
}

const integer = (value: unknown, min: number, max: number): boolean =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= min && value <= max;

export function parsePresentationRequest(value: unknown): PresentationRequest | null {
  if (!isRecord(value) || Object.keys(value).sort().join(',') !== 'action,id,params,protocol,type,version'
    || value.protocol !== PRESENTATION_PROTOCOL || value.version !== 1 || value.type !== 'request'
    || !integer(value.id, 1, Number.MAX_SAFE_INTEGER) || !isRecord(value.params)) return null;
  const p = value.params;
  const keys = Object.keys(p).sort().join(',');
  const keyValid = typeof p.conversationKey === 'string' && !!p.conversationKey
    && utf8ByteLength(p.conversationKey) <= 800;
  switch (value.action) {
    case 'select': case 'stop':
      if (keys !== 'conversationKey' || !keyValid) return null;
      break;
    case 'reply':
      if ((keys !== 'conversationKey,offset,replyId' && keys !== 'conversationKey,offset,replyId,role')
        || (p.role !== undefined && p.role !== 'user' && p.role !== 'assistant') || !keyValid || !integer(p.replyId, 1, Number.MAX_SAFE_INTEGER)
        || !integer(p.offset, 0, Number.MAX_SAFE_INTEGER)) return null;
      break;
    case 'model':
      if (keys !== 'conversationKey,modelId' || !keyValid || typeof p.modelId !== 'string'
        || !p.modelId || utf8ByteLength(p.modelId) > 800) return null;
      break;
    case 'workspace':
      if (!['', 'conversationKey', 'transition', 'conversationKey,transition'].includes(keys)
        || ('conversationKey' in p && !keyValid)
        || ('transition' in p && !isWindowTransitionOptions(p.transition))) return null;
      break;
    case 'draft': case 'send':
      if (keys !== 'conversationKey,text' || !keyValid || typeof p.text !== 'string'
        || utf8ByteLength(p.text) > 32 * 1024 || (value.action === 'send' && !p.text.trim())) return null;
      break;
    case 'resize':
      if (keys !== 'height,width' || !integer(p.width, 160, 960) || !integer(p.height, 120, 960)) return null;
      break;
    case 'move':
      if (keys !== 'deltaX,deltaY' || !integer(p.deltaX, -4096, 4096) || !integer(p.deltaY, -4096, 4096)) return null;
      break;
    case 'hitTest':
      if (keys !== 'interactive' || typeof p.interactive !== 'boolean') return null;
      break;
    default: return null;
  }
  return value as unknown as PresentationRequest;
}

/** Keep the deduplication ledger bounded without ever re-executing an expired request. */
export function createPresentationRequests(
  execute: (request: PresentationRequest) => Promise<unknown>,
  describeError: (error: unknown) => { code: string; message: string },
) {
  let highest = 0;
  let pending = 0;
  const results = new Map<number, { fingerprint: string; result: Promise<PresentationResult>; done: boolean }>();
  const fail = (id: number, code: string) => ({
    protocol: PRESENTATION_PROTOCOL, version: 1, type: 'result', id, ok: false,
    error: describeError(new Error(code)),
  }) as PresentationResult;
  return async (request: PresentationRequest): Promise<PresentationResult> => {
    const fingerprint = JSON.stringify([request.action, Object.entries(request.params).sort(([a], [b]) => a.localeCompare(b))]);
    const previous = results.get(request.id);
    if (previous) return previous.fingerprint === fingerprint ? previous.result : fail(request.id, 'REQUEST_CONFLICT');
    if (request.id <= highest) return fail(request.id, 'STALE_REQUEST');
    highest = request.id;
    if (pending >= 32) return fail(request.id, 'REQUEST_BUSY');
    pending += 1;
    const entry = { fingerprint, result: Promise.resolve({} as PresentationResult), done: false };
    // Defer execution until the ledger owns the id, including synchronously throwing actions.
    entry.result = Promise.resolve().then(() => execute(request)).then(
      (value): PresentationResult => ({ protocol: PRESENTATION_PROTOCOL, version: 1, type: 'result', id: request.id, ok: true, value }),
      (error): PresentationResult => ({ protocol: PRESENTATION_PROTOCOL, version: 1, type: 'result', id: request.id, ok: false,
        error: describeError(error) }),
    ).finally(() => {
      pending -= 1;
      entry.done = true;
      for (const [id, candidate] of results) {
        if (results.size <= 512) break;
        if (candidate.done) results.delete(id);
      }
    });
    results.set(request.id, entry);
    return entry.result;
  };
}
