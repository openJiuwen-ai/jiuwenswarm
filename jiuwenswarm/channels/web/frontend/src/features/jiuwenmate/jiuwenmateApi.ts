import type {
  JiuwenMateCategory,
  JiuwenMateColleague,
  JiuwenMateColleagueDraft,
  JiuwenMateModelOption,
} from './types';

/** Server RPCs handled by the AgentServer JiuwenMate adapter; the server fixes the namespace. */
export const JIUWENMATE_METHODS = {
  ensureDefault: 'jiuwenmate.colleague.ensure_default',
  list: 'jiuwenmate.colleague.list',
  get: 'jiuwenmate.colleague.get',
  create: 'jiuwenmate.colleague.create',
  settingsGet: 'jiuwenmate.colleague.settings.get',
  settingsUpdate: 'jiuwenmate.colleague.settings.update',
  publish: 'jiuwenmate.colleague.publish',
  delete: 'jiuwenmate.colleague.delete',
  modelsList: 'jiuwenmate.models.list',
  chatSend: 'jiuwenmate.chat.send',
  chatHistory: 'jiuwenmate.chat.history',
  requestGet: 'jiuwenmate.request.get',
  requestAbort: 'jiuwenmate.request.abort',
  conversationList: 'jiuwenmate.conversation.list',
  conversationRename: 'jiuwenmate.conversation.rename',
  conversationDelete: 'jiuwenmate.conversation.delete',
} as const;

/** Server push carrying reply fragments of a running private message. */
export const JIUWENMATE_STREAM_EVENT = 'jiuwenmate.chat.stream';

const CATEGORIES: readonly JiuwenMateCategory[] = [
  'education',
  'development',
  'data',
  'document',
  'finance',
  'legal',
  'commerce',
  'content',
  'custom',
];

/** The server stores at most this many characters of an avatar URL; uploaded images are not stored. */
const MAX_AVATAR_URL = 2048;

export type JiuwenMateRequest = (method: string, params?: Record<string, unknown>) => Promise<unknown>;
export type JiuwenMateSubscribe = (event: string, handler: (payload: unknown) => void) => () => void;

export interface JiuwenMateWireCapabilities {
  model: string | null;
  skills: string[];
  connectors: string[];
  plugins: string[];
  knowledge: string[];
  starter_prompts: string[];
}

export interface JiuwenMateWireSettings {
  name: string;
  description: string;
  persona?: string;
  tags: string[];
  category: string;
  avatar_url: string;
  capabilities: JiuwenMateWireCapabilities;
}

export interface JiuwenMateModelCatalog {
  models: JiuwenMateModelOption[];
  defaultModelId: string;
  defaultModelLabel: string;
}

export interface JiuwenMatePublishedVersion {
  agentId: string;
  configVersion: string;
  publishedAt: string;
}

/** A Web private conversation with a colleague; it is listed once its first message is accepted. */
export interface JiuwenMateConversation {
  id: string;
  title: string;
  updatedAt: string;
}

/** A private message and what became of it; replies are only the outputs the colleague's final answer adopted. */
export interface JiuwenMateChatRequest {
  requestId: string;
  conversationId: string;
  messageId: string;
  text: string;
  receivedAt: string;
  status: 'queued' | 'dispatched' | 'ended';
  outcome: string | null;
  running: boolean;
  replies: string[];
  reason: string | null;
  draft: JiuwenMateChatDraft | null;
  control: JiuwenMateRequestControl | null;
  recovery: JiuwenMateRequestRecovery | null;
}

/** How a message left unfinished by a server restart was closed; ``unverifiedToolCalls`` may have run. */
export interface JiuwenMateRequestRecovery {
  decision: 'closed_from_log' | 'closed_by_control' | 'awaiting_continuation';
  unverifiedToolCalls: string[];
}

export type JiuwenMateControlReason = 'stop' | 'cancel';

/** The sender's stop or cancel of a message; ``failed`` means interrupting it raised and ``detail`` says why. */
export interface JiuwenMateRequestControl {
  reason: JiuwenMateControlReason;
  status: 'accepted' | 'failed';
  detail: string | null;
}

/**
 * Text the model is writing for a running message, up to event ``seq`` of its execution. Each model
 * call starts new text; ``closed`` means the call that wrote ``text`` has completed.
 */
export interface JiuwenMateChatDraft {
  executionId: string;
  seq: number;
  text: string;
  closed: boolean;
}

/** One pushed step: model text, the end of a model call, or the end of the execution. */
export interface JiuwenMateStreamEvent {
  kind: 'delta' | 'segment' | 'ended';
  agentId: string;
  requestId: string;
  executionId: string;
  seq: number;
  text: string;
}

/** How a message is shown: waiting, running, interrupted by a server restart, or its recorded outcome. */
export type JiuwenMateChatPhase =
  | 'queued'
  | 'running'
  | 'interrupted'
  | 'completed'
  | 'ignored'
  | 'paused'
  | 'stopped'
  | 'cancelled'
  | 'failed';

const OUTCOMES: readonly JiuwenMateChatPhase[] = ['completed', 'ignored', 'paused', 'stopped', 'cancelled', 'failed'];

type Wire = Record<string, unknown>;

function record(value: unknown): Wire {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Wire) : {};
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function texts(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function filled(values: readonly string[]): string[] {
  return values.map((item) => item.trim()).filter(Boolean);
}

export function toColleague(value: unknown): JiuwenMateColleague {
  const wire = record(value);
  const capabilities = record(wire.capabilities);
  const category = text(wire.category) as JiuwenMateCategory;
  const avatarUrl = text(wire.avatar_url);
  return {
    id: text(wire.agent_id),
    name: text(wire.name),
    description: text(wire.description),
    tags: texts(wire.tags),
    category: CATEGORIES.includes(category) ? category : 'custom',
    ...(avatarUrl ? { avatarUrl } : {}),
    capabilities: {
      model: text(capabilities.model),
      skills: texts(capabilities.skills),
      connectors: texts(capabilities.connectors),
      plugins: texts(capabilities.plugins),
      knowledge: texts(capabilities.knowledge),
      starterPrompts: texts(capabilities.starter_prompts),
    },
    imConfigured: false,
    disabled: false,
    joinedAt: text(wire.created_at),
    isDefault: wire.is_default === true,
    currentVersion: typeof wire.current_version === 'string' ? wire.current_version : null,
    hasUnpublishedChanges: wire.has_unpublished_changes === true,
  };
}

/** Settings to create a colleague from a draft; a model missing from the list falls back to the server default. */
export function toWireSettings(
  draft: JiuwenMateColleagueDraft,
  models: readonly JiuwenMateModelOption[],
): JiuwenMateWireSettings {
  const { capabilities } = draft;
  const avatarUrl = draft.avatarUrl ?? '';
  return {
    name: draft.name.trim(),
    description: draft.description.trim(),
    tags: filled(draft.tags),
    category: draft.category,
    avatar_url: avatarUrl.startsWith('data:') || avatarUrl.length > MAX_AVATAR_URL ? '' : avatarUrl,
    capabilities: {
      model: models.some((item) => item.id === capabilities.model) ? capabilities.model : null,
      skills: filled(capabilities.skills),
      connectors: filled(capabilities.connectors),
      plugins: filled(capabilities.plugins),
      knowledge: filled(capabilities.knowledge),
      starter_prompts: filled(capabilities.starterPrompts),
    },
  };
}

export function toModelCatalog(value: unknown): JiuwenMateModelCatalog {
  const wire = record(value);
  const models = (Array.isArray(wire.models) ? wire.models : []).flatMap((item) => {
    const model = record(item);
    const id = text(model.selection_key);
    if (!id) return [];
    return [{ id, label: text(model.display_name) || text(model.model_name) || id, provider: text(model.provider) }];
  });
  return {
    models,
    defaultModelId: text(wire.current_selection),
    defaultModelLabel: text(wire.current_display_name),
  };
}

function toDraft(value: unknown): JiuwenMateChatDraft | null {
  const wire = record(value);
  const executionId = text(wire.execution_id);
  if (!executionId || typeof wire.seq !== 'number') return null;
  return { executionId, seq: wire.seq, text: text(wire.text), closed: wire.closed === true };
}

export function toStreamEvent(value: unknown): JiuwenMateStreamEvent | null {
  const wire = record(value);
  const kind = text(wire.kind);
  if (kind !== 'delta' && kind !== 'segment' && kind !== 'ended') return null;
  const event = {
    kind,
    agentId: text(wire.agent_id),
    requestId: text(wire.request_id),
    executionId: text(wire.execution_id),
    seq: typeof wire.seq === 'number' ? wire.seq : 0,
    text: text(wire.text),
  } as const;
  return event.agentId && event.requestId && event.executionId && event.seq > 0 ? event : null;
}

export function toConversation(value: unknown): JiuwenMateConversation {
  const wire = record(value);
  return { id: text(wire.conversation_id), title: text(wire.title), updatedAt: text(wire.updated_at) };
}

/** Puts a conversation that just received a message at the top of the list, replacing its old entry. */
export function upsertConversation(
  conversations: readonly JiuwenMateConversation[],
  conversation: JiuwenMateConversation,
): JiuwenMateConversation[] {
  return [conversation, ...conversations.filter((item) => item.id !== conversation.id)];
}

export function toChatRequest(value: unknown): JiuwenMateChatRequest {
  const wire = record(value);
  const status = text(wire.status);
  return {
    requestId: text(wire.request_id),
    conversationId: text(wire.conversation_id),
    messageId: text(wire.message_id),
    text: text(wire.text),
    receivedAt: text(wire.received_at),
    status: status === 'queued' || status === 'ended' ? status : 'dispatched',
    outcome: typeof wire.outcome === 'string' ? wire.outcome : null,
    running: wire.running === true,
    replies: texts(wire.replies),
    reason: typeof wire.reason === 'string' && wire.reason ? wire.reason : null,
    draft: status === 'ended' ? null : toDraft(wire.draft),
    control: toControl(wire.control),
    recovery: toRecovery(wire.recovery),
  };
}

const DECISIONS: readonly JiuwenMateRequestRecovery['decision'][] = [
  'closed_from_log',
  'closed_by_control',
  'awaiting_continuation',
];

function toRecovery(value: unknown): JiuwenMateRequestRecovery | null {
  const wire = record(value);
  const decision = DECISIONS.find((item) => item === wire.decision);
  return decision ? { decision, unverifiedToolCalls: texts(wire.unverified_tool_calls) } : null;
}

function toControl(value: unknown): JiuwenMateRequestControl | null {
  const wire = record(value);
  if (wire.reason !== 'stop' && wire.reason !== 'cancel') return null;
  return {
    reason: wire.reason,
    status: wire.status === 'failed' ? 'failed' : 'accepted',
    detail: typeof wire.detail === 'string' && wire.detail ? wire.detail : null,
  };
}

export function chatPhase(request: JiuwenMateChatRequest): JiuwenMateChatPhase {
  if (request.status === 'queued') return 'queued';
  if (request.status === 'dispatched') return request.running ? 'running' : 'interrupted';
  const outcome = request.outcome as JiuwenMateChatPhase;
  return OUTCOMES.includes(outcome) ? outcome : 'failed';
}

/** Whether the page should ask again: the message is still waiting or running on the server. */
export function isPending(request: JiuwenMateChatRequest): boolean {
  const phase = chatPhase(request);
  return phase === 'queued' || phase === 'running';
}

function isNewer(draft: JiuwenMateChatDraft | null, than: JiuwenMateChatDraft | null): boolean {
  if (!draft) return false;
  return !than || (draft.executionId === than.executionId && draft.seq > than.seq);
}

/**
 * Replaces a request with its newer state, or appends it if the list does not have it yet.
 * Pushed text can be ahead of a query, because the server writes fragments in the background,
 * so a running request keeps the draft it already has when the queried one is older.
 */
export function mergeChatRequest(
  requests: readonly JiuwenMateChatRequest[],
  request: JiuwenMateChatRequest,
): JiuwenMateChatRequest[] {
  const index = requests.findIndex((item) => item.requestId === request.requestId);
  if (index < 0) return [...requests, request];
  const current = requests[index];
  const merged =
    request.status !== 'ended' && isNewer(current.draft, request.draft) ? { ...request, draft: current.draft } : request;
  return requests.map((item, position) => (position === index ? merged : item));
}

/** Applies a pushed step to its request; returns null when the list does not have the request yet. */
export function applyStreamEvent(
  requests: JiuwenMateChatRequest[],
  event: JiuwenMateStreamEvent,
): JiuwenMateChatRequest[] | null {
  const index = requests.findIndex((item) => item.requestId === event.requestId);
  if (index < 0) return null;
  const current = requests[index];
  const draft = current.draft;
  if (current.status === 'ended' || event.kind === 'ended') return requests;
  if (draft && draft.executionId === event.executionId && draft.seq >= event.seq) return requests;
  const continues = draft !== null && draft.executionId === event.executionId && !draft.closed;
  const next: JiuwenMateChatDraft =
    event.kind === 'delta'
      ? { executionId: event.executionId, seq: event.seq, text: (continues ? draft.text : '') + event.text, closed: false }
      : { executionId: event.executionId, seq: event.seq, text: draft?.executionId === event.executionId ? draft.text : '', closed: true };
  return requests.map((item, position) => (position === index ? { ...item, draft: next } : item));
}

export function findModel(catalog: JiuwenMateModelCatalog, id: string): JiuwenMateModelOption | null {
  return catalog.models.find((item) => item.id === (id || catalog.defaultModelId)) ?? null;
}

export function createJiuwenMateApi(request: JiuwenMateRequest, subscribe?: JiuwenMateSubscribe) {
  const colleague = async (method: string, params?: Record<string, unknown>) =>
    toColleague(record(await request(method, params)).colleague);

  return {
    /** Ensures 小九 exists, then lists every colleague. */
    async loadColleagues(): Promise<JiuwenMateColleague[]> {
      await request(JIUWENMATE_METHODS.ensureDefault);
      const listed = record(await request(JIUWENMATE_METHODS.list));
      return (Array.isArray(listed.colleagues) ? listed.colleagues : []).map(toColleague);
    },
    getColleague: (agentId: string) => colleague(JIUWENMATE_METHODS.get, { agent_id: agentId }),
    createColleague: (draft: JiuwenMateColleagueDraft, models: readonly JiuwenMateModelOption[]) =>
      colleague(JIUWENMATE_METHODS.create, { settings: toWireSettings(draft, models) }),
    async readSettings(agentId: string): Promise<JiuwenMateWireSettings> {
      return record(await request(JIUWENMATE_METHODS.settingsGet, { agent_id: agentId })).settings as JiuwenMateWireSettings;
    },
    updateSettings: (agentId: string, settings: JiuwenMateWireSettings) =>
      colleague(JIUWENMATE_METHODS.settingsUpdate, { agent_id: agentId, settings }),
    async publish(agentId: string): Promise<JiuwenMatePublishedVersion> {
      const version = record(record(await request(JIUWENMATE_METHODS.publish, { agent_id: agentId })).version);
      return {
        agentId: text(version.agent_id),
        configVersion: text(version.config_version),
        publishedAt: text(version.published_at),
      };
    },
    async deleteColleague(agentId: string): Promise<void> {
      await request(JIUWENMATE_METHODS.delete, { agent_id: agentId });
    },
    async listModels(): Promise<JiuwenMateModelCatalog> {
      return toModelCatalog(await request(JIUWENMATE_METHODS.modelsList));
    },
    /** Sends a private message; resending the same message id returns the request it already created. */
    async sendMessage(
      agentId: string,
      conversationId: string,
      message: string,
      messageId: string,
    ): Promise<{ request: JiuwenMateChatRequest; conversation: JiuwenMateConversation }> {
      const sent = record(
        await request(JIUWENMATE_METHODS.chatSend, {
          agent_id: agentId,
          conversation_id: conversationId,
          text: message,
          message_id: messageId,
        }),
      );
      return { request: toChatRequest(sent.request), conversation: toConversation(sent.conversation) };
    },
    async history(agentId: string, conversationId: string): Promise<JiuwenMateChatRequest[]> {
      const listed = record(
        await request(JIUWENMATE_METHODS.chatHistory, { agent_id: agentId, conversation_id: conversationId }),
      );
      return (Array.isArray(listed.requests) ? listed.requests : []).map(toChatRequest);
    },
    /** The colleague's conversations, latest message first. */
    async listConversations(agentId: string): Promise<JiuwenMateConversation[]> {
      const listed = record(await request(JIUWENMATE_METHODS.conversationList, { agent_id: agentId }));
      return (Array.isArray(listed.conversations) ? listed.conversations : []).map(toConversation);
    },
    async renameConversation(agentId: string, conversationId: string, title: string): Promise<JiuwenMateConversation> {
      const renamed = record(
        await request(JIUWENMATE_METHODS.conversationRename, { agent_id: agentId, conversation_id: conversationId, title }),
      );
      return toConversation(renamed.conversation);
    },
    async deleteConversation(agentId: string, conversationId: string): Promise<void> {
      await request(JIUWENMATE_METHODS.conversationDelete, { agent_id: agentId, conversation_id: conversationId });
    },
    /** Stops a running message or cancels a queued one; a repeated control returns the first one. */
    async abortRequest(requestId: string, reason: JiuwenMateControlReason): Promise<JiuwenMateChatRequest> {
      return toChatRequest(record(await request(JIUWENMATE_METHODS.requestAbort, { request_id: requestId, reason })).request);
    },
    async getRequest(requestId: string): Promise<JiuwenMateChatRequest> {
      return toChatRequest(record(await request(JIUWENMATE_METHODS.requestGet, { request_id: requestId })).request);
    },
    /** Receives pushed reply steps; without a push connection nothing arrives and querying still works. */
    onStream(handler: (event: JiuwenMateStreamEvent) => void): () => void {
      if (!subscribe) return () => undefined;
      return subscribe(JIUWENMATE_STREAM_EVENT, (payload) => {
        const event = toStreamEvent(payload);
        if (event) handler(event);
      });
    },
  };
}

export type JiuwenMateApi = ReturnType<typeof createJiuwenMateApi>;

export function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return typeof error === 'string' ? error : '';
}
