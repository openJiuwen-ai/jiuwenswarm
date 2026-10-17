import type { Schedule, ScheduleDraft, ScheduleRun } from './schedules/types';
import type {
  JiuwenMateActivity,
  JiuwenMateActivityChannel,
  JiuwenMateActivityDay,
  JiuwenMateActivityGroup,
  JiuwenMateActivityHour,
  JiuwenMateActivityMember,
  JiuwenMateCategory,
  JiuwenMateChannel,
  JiuwenMateChannelMessage,
  JiuwenMateColleague,
  JiuwenMateColleagueDraft,
  JiuwenMateModelOption,
  JiuwenMateTraceRow,
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
  activityGet: 'jiuwenmate.activity.get',
  scheduleList: 'jiuwenmate.schedule.list',
  scheduleCreate: 'jiuwenmate.schedule.create',
  scheduleSetEnabled: 'jiuwenmate.schedule.set_enabled',
  scheduleDelete: 'jiuwenmate.schedule.delete',
  scheduleUpdate: 'jiuwenmate.schedule.update',
  scheduleRun: 'jiuwenmate.schedule.run',
  scheduleHistory: 'jiuwenmate.schedule.history',
  channelList: 'jiuwenmate.channel.list',
  channelMessages: 'jiuwenmate.channel.messages',
  channelTrace: 'jiuwenmate.channel.trace',
} as const;

export type JiuwenMateSchedule = Schedule;

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
  files: JiuwenMateSharedFile[];
  reason: string | null;
  draft: JiuwenMateChatDraft | null;
  control: JiuwenMateRequestControl | null;
  recovery: JiuwenMateRequestRecovery | null;
}

/** A file the colleague handed off with its answer; ``downloadUrl`` is null while its copy is unavailable. */
export interface JiuwenMateSharedFile {
  outputId: string;
  name: string;
  size: number;
  caption: string | null;
  downloadUrl: string | null;
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

function count(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function texts(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function rows(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function filled(values: readonly string[]): string[] {
  return values.map((item) => item.trim()).filter(Boolean);
}

/** A skill directory or MCP record the colleague publish step can resolve. */
export interface ColleagueResourceOption {
  name: string;
  label: string;
}

const SELECTABLE_SKILL_TYPES = new Set(['skill', 'swarm_skill', 'multimodal_skill']);

function resourceOption(name: string, label: string): ColleagueResourceOption | null {
  const trimmed = name.trim();
  if (!trimmed) return null;
  return { name: trimmed, label: label.trim() || trimmed };
}

function uniqueOptions(options: readonly ColleagueResourceOption[]): ColleagueResourceOption[] {
  const seen = new Set<string>();
  return options.filter((option) => {
    if (seen.has(option.name)) return false;
    seen.add(option.name);
    return true;
  });
}

/**
 * Names publish can copy from the local skill library and the connected MCP state.
 * Marketplace-only skills, disabled skills, skill packs and disconnected connectors are omitted.
 */
export function selectableColleagueResources(
  skillsPayload: unknown,
  connectorsPayload: unknown,
): { skills: ColleagueResourceOption[]; connectors: ColleagueResourceOption[]; plugins: ColleagueResourceOption[] } {
  const skills = uniqueOptions(
    rows(record(skillsPayload).skills).flatMap((item: unknown) => {
      const row = record(item);
      const skillType = text(row.skill_type);
      if (row.source === 'mcp' || row.installed === false || row.enabled === false) return [];
      if (skillType && !SELECTABLE_SKILL_TYPES.has(skillType)) return [];
      const option = resourceOption(text(row.name), text(row.display_name));
      return option ? [option] : [];
    }),
  );
  const connectors = uniqueOptions(
    rows(record(connectorsPayload).items).flatMap((item: unknown) => {
      const row = record(item);
      if (text(row.connection_state) !== 'connected') return [];
      const option = resourceOption(text(row.name), text(row.display_name));
      return option ? [option] : [];
    }),
  );
  const plugins = uniqueOptions(
    rows(record(skillsPayload).plugins).flatMap((item: unknown) => {
      const row = record(item);
      const option = resourceOption(text(row.plugin_name) || text(row.name), text(row.display_name));
      return option ? [option] : [];
    }),
  );
  return { skills, connectors, plugins };
}

/** Published personal-context pages an employee can be assigned. */
export function selectableKnowledgePages(payload: unknown): ColleagueResourceOption[] {
  return uniqueOptions(
    rows(record(payload).pages).flatMap((item: unknown) => {
      const row = record(item);
      const option = resourceOption(text(row.name), text(row.label));
      return option ? [option] : [];
    }),
  );
}

/** Settings update that changes only the selected skills and connectors. */
export function withResourceSelection(
  colleague: JiuwenMateColleague,
  models: readonly JiuwenMateModelOption[],
  skills: readonly string[],
  connectors: readonly string[],
): JiuwenMateWireSettings {
  return withColleagueEdit(colleague, models, { skills, connectors });
}

/** One settings update. Omitted fields keep the colleague's current values. */
export function withColleagueEdit(
  colleague: JiuwenMateColleague,
  models: readonly JiuwenMateModelOption[],
  edit: {
    name?: string;
    description?: string;
    tags?: readonly string[];
    model?: string;
    skills?: readonly string[];
    connectors?: readonly string[];
    plugins?: readonly string[];
    knowledge?: readonly string[];
  },
): JiuwenMateWireSettings {
  return toWireSettings(
    {
      ...colleague,
      name: edit.name ?? colleague.name,
      description: edit.description ?? colleague.description,
      tags: [...(edit.tags ?? colleague.tags)],
      capabilities: {
        ...colleague.capabilities,
        model: edit.model ?? colleague.capabilities.model,
        skills: [...(edit.skills ?? colleague.capabilities.skills)],
        connectors: [...(edit.connectors ?? colleague.capabilities.connectors)],
        plugins: [...(edit.plugins ?? colleague.capabilities.plugins)],
        knowledge: [...(edit.knowledge ?? colleague.capabilities.knowledge)],
      },
    },
    models,
  );
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
    workspaceDir: text(wire.workspace_dir) || null,
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
    files: toSharedFiles(wire.files),
    reason: typeof wire.reason === 'string' && wire.reason ? wire.reason : null,
    draft: status === 'ended' ? null : toDraft(wire.draft),
    control: toControl(wire.control),
    recovery: toRecovery(wire.recovery),
  };
}

export function formatSharedFileSize(size: number): string {
  if (!Number.isFinite(size) || size < 0) return '0 B';
  if (size < 1024) return `${size} B`;
  const scaled = size < 1024 * 1024 ? size / 1024 : size / (1024 * 1024);
  const unit = size < 1024 * 1024 ? 'KB' : 'MB';
  const digits = scaled < 10 ? 1 : 0;
  return `${Number(scaled.toFixed(digits))} ${unit}`;
}

function toSharedFiles(value: unknown): JiuwenMateSharedFile[] {
  return (Array.isArray(value) ? value : []).flatMap((item) => {
    const wire = record(item);
    const outputId = text(wire.output_id);
    const name = text(wire.name);
    if (!outputId || !name) return [];
    const downloadUrl = text(wire.download_url);
    return [
      {
        outputId,
        name,
        size: typeof wire.size === 'number' ? wire.size : 0,
        caption: text(wire.caption) || null,
        downloadUrl: downloadUrl.startsWith('/file-api/download?') ? downloadUrl : null,
      },
    ];
  });
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

function toActivityDay(value: unknown): JiuwenMateActivityDay | null {
  const row = record(value);
  const date = text(row.date);
  if (!date) return null;
  return { date, tasks: count(row.tasks), minutes: count(row.minutes), calls: count(row.calls), tokens: count(row.tokens) };
}

function toActivityHour(value: unknown): JiuwenMateActivityHour | null {
  const row = record(value);
  const date = text(row.date);
  if (!date) return null;
  return { date, hour: count(row.hour), minutes: count(row.minutes) };
}

function toActivityChannel(value: unknown): JiuwenMateActivityChannel | null {
  const row = record(value);
  const channel = text(row.channel);
  if (!channel) return null;
  return {
    channel,
    calls: count(row.calls),
    tokens: count(row.tokens),
    tasks: count(row.tasks),
    replies: count(row.replies),
    failures: count(row.failures),
  };
}

function toActivityGroup(value: unknown): JiuwenMateActivityGroup | null {
  const row = record(value);
  const id = text(row.group_chat_id);
  if (!id) return null;
  return {
    id,
    name: text(row.name) || id,
    platform: text(row.platform),
    artifacts: count(row.artifacts),
    tokens: count(row.tokens),
    tasks: count(row.tasks),
    lastResponseAt: text(row.last_response_at) || null,
  };
}

function toActivityMember(value: unknown): JiuwenMateActivityMember | null {
  const row = record(value);
  const agentId = text(row.agent_id);
  if (!agentId) return null;
  return {
    agentId,
    calls: count(row.calls),
    tokens: count(row.tokens),
    tasks: count(row.tasks),
    replies: count(row.replies),
    failures: count(row.failures),
    companionDays: count(row.companion_days),
    lastResponseAt: text(row.last_response_at) || null,
    days: (Array.isArray(row.days) ? row.days : []).flatMap((item) => {
      const day = toActivityDay(item);
      return day ? [day] : [];
    }),
    hours: (Array.isArray(row.hours) ? row.hours : []).flatMap((item) => {
      const hour = toActivityHour(item);
      return hour ? [hour] : [];
    }),
    channels: (Array.isArray(row.channels) ? row.channels : []).flatMap((item) => {
      const channel = toActivityChannel(item);
      return channel ? [channel] : [];
    }),
    groups: (Array.isArray(row.groups) ? row.groups : []).flatMap((item) => {
      const group = toActivityGroup(item);
      return group ? [group] : [];
    }),
  };
}

export function toActivity(value: unknown): JiuwenMateActivity {
  const row = record(value);
  return {
    generatedAt: text(row.generated_at),
    members: (Array.isArray(row.members) ? row.members : []).flatMap((item) => {
      const member = toActivityMember(item);
      return member ? [member] : [];
    }),
  };
}

export function toSchedule(value: unknown): JiuwenMateSchedule {
  const plan = record(value);
  return {
    id: text(plan.plan_id),
    agentId: text(plan.agent_id),
    name: text(plan.name),
    prompt: text(plan.prompt),
    cronExpr: text(plan.cron_expr),
    timezone: text(plan.timezone),
    enabled: plan.enabled === true,
    nextFireAt: text(plan.next_fire_at) || null,
    scheduleKind: plan.schedule_kind === 'interval' || plan.schedule_kind === 'once' ? plan.schedule_kind : 'cron',
    intervalSeconds: typeof plan.interval_seconds === 'number' ? plan.interval_seconds : null,
    runAt: text(plan.run_at) || null,
    startsAt: text(plan.starts_at) || null,
    endsAt: text(plan.ends_at) || null,
    conversationId: text(plan.conversation_id) || `schedule:${text(plan.plan_id)}`,
    runCount: typeof plan.run_count === 'number' ? plan.run_count : 0,
    lastStatus: plan.last_status === 'failed' || plan.last_status === 'completed' || plan.last_status === 'paused'
      ? plan.last_status : plan.enabled === true ? 'running' : 'paused',
  };
}

function scheduleParams(draft: ScheduleDraft): Record<string, unknown> {
  return {
    name: draft.name,
    prompt: draft.prompt,
    cron_expr: draft.cronExpr,
    timezone: draft.timezone,
    schedule_kind: draft.scheduleKind,
    interval_seconds: draft.intervalSeconds,
    run_at: draft.runAt,
    starts_at: draft.startsAt,
    ends_at: draft.endsAt,
  };
}

const TRACE_ROLES = ['system', 'context', 'user', 'assistant', 'tool'] as const;
const TRACE_LANES = ['input', 'model', 'tools'] as const;

export function toChannel(value: unknown): JiuwenMateChannel {
  const wire = record(value);
  const platform = text(wire.platform);
  return {
    id: text(wire.id),
    name: text(wire.name) || text(wire.id),
    platform,
    artifacts: Number(wire.artifacts) || 0,
    tokenUsage: Number(wire.token_usage) || 0,
    lastResponseMinutes: Number(wire.last_response_minutes) || 0,
  };
}

export function toChannelMessage(value: unknown): JiuwenMateChannelMessage {
  const wire = record(value);
  const attachments = Number(wire.attachments) || 0;
  return {
    id: text(wire.id),
    sender: text(wire.sender),
    fromColleague: wire.sender_kind === 'agent',
    mentionColleague: false,
    content: text(wire.text),
    status: text(wire.status) || 'normal',
    ...(attachments > 0 ? { attachment: { title: String(attachments), kind: 'file' } } : {}),
  };
}

export function toChannelTrace(value: unknown): { startedAt: string; rows: JiuwenMateTraceRow[] } {
  const wire = record(value);
  const rows = (Array.isArray(wire.rows) ? wire.rows : []).map((item) => {
    const row = record(item);
    const role = text(row.role);
    const lane = text(row.lane);
    return {
      id: text(row.id),
      traceId: text(row.trace_id) || text(row.id),
      turn: Number(row.turn) || 1,
      role: TRACE_ROLES.includes(role as (typeof TRACE_ROLES)[number]) ? (role as JiuwenMateTraceRow['role']) : 'system',
      lane: TRACE_LANES.includes(lane as (typeof TRACE_LANES)[number]) ? (lane as JiuwenMateTraceRow['lane']) : 'input',
      content: text(row.content),
      startMs: Number(row.start_ms) || 0,
      durationMs: Number(row.duration_ms) || 0,
      inputTokens: Number(row.input_tokens) || 0,
      outputTokens: Number(row.output_tokens) || 0,
    };
  });
  return { startedAt: text(wire.started_at), rows };
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
    async loadActivity(): Promise<JiuwenMateActivity> {
      return toActivity(record(await request(JIUWENMATE_METHODS.activityGet)).activity);
    },
    async listSchedules(agentId?: string): Promise<JiuwenMateSchedule[]> {
      const listed = record(await request(JIUWENMATE_METHODS.scheduleList, agentId ? { agent_id: agentId } : {}));
      return (Array.isArray(listed.plans) ? listed.plans : []).map(toSchedule);
    },
    async createSchedule(agentId: string, draft: ScheduleDraft): Promise<JiuwenMateSchedule> {
      const created = record(
        await request(JIUWENMATE_METHODS.scheduleCreate, {
          agent_id: agentId,
          ...scheduleParams(draft),
        }),
      );
      return toSchedule(created.plan);
    },
    async updateSchedule(planId: string, draft: ScheduleDraft): Promise<JiuwenMateSchedule> {
      const updated = record(await request(JIUWENMATE_METHODS.scheduleUpdate, {
        plan_id: planId, ...scheduleParams(draft),
      }));
      return toSchedule(updated.plan);
    },
    async runSchedule(planId: string): Promise<string> {
      return text(record(await request(JIUWENMATE_METHODS.scheduleRun, { plan_id: planId })).request_id);
    },
    async scheduleHistory(planId: string): Promise<{ plan: JiuwenMateSchedule; runs: ScheduleRun[] }> {
      const result = record(await request(JIUWENMATE_METHODS.scheduleHistory, { plan_id: planId }));
      return {
        plan: toSchedule(result.plan),
        runs: (Array.isArray(result.runs) ? result.runs : []).map((item) => {
          const run = record(item);
          return {
            requestId: text(run.request_id), scheduledFor: text(run.scheduled_for),
            status: text(run.status), description: text(run.description),
          };
        }),
      };
    },
    async setScheduleEnabled(planId: string, enabled: boolean): Promise<JiuwenMateSchedule> {
      const updated = record(
        await request(JIUWENMATE_METHODS.scheduleSetEnabled, { plan_id: planId, enabled }),
      );
      return toSchedule(updated.plan);
    },
    async deleteSchedule(planId: string): Promise<void> {
      await request(JIUWENMATE_METHODS.scheduleDelete, { plan_id: planId });
    },
    async listChannels(agentId: string): Promise<JiuwenMateChannel[]> {
      const listed = record(await request(JIUWENMATE_METHODS.channelList, { agent_id: agentId }));
      return (Array.isArray(listed.channels) ? listed.channels : []).map(toChannel);
    },
    async channelMessages(agentId: string, groupId: string): Promise<JiuwenMateChannelMessage[]> {
      const listed = record(await request(JIUWENMATE_METHODS.channelMessages, { agent_id: agentId, group_id: groupId }));
      return (Array.isArray(listed.messages) ? listed.messages : []).map(toChannelMessage);
    },
    async channelTrace(agentId: string, groupId: string): Promise<{ startedAt: string; rows: JiuwenMateTraceRow[] }> {
      return toChannelTrace(await request(JIUWENMATE_METHODS.channelTrace, { agent_id: agentId, group_id: groupId }));
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
