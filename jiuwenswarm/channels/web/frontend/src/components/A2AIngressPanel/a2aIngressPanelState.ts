export type A2AIngressState = 'disabled' | 'starting' | 'running' | 'stopping' | 'error';

export interface A2AIngressSnapshot {
  enabled: boolean;
  state: A2AIngressState;
  desired_host: string;
  desired_port: number;
  desired_rpc_path: string;
  desired_card_path: string;
  desired_extended_card_path: string;
  desired_protocol_version: string;
  desired_app_name: string;
  desired_app_description: string;
  desired_app_version: string;
  desired_expose_reasoning: boolean;
  desired_rpc_url: string;
  desired_card_url: string;
  desired_extended_card_url: string;
  effective_host: string | null;
  effective_port: number | null;
  effective_rpc_path: string | null;
  effective_card_path: string | null;
  effective_extended_card_path: string | null;
  effective_rpc_url: string | null;
  effective_card_url: string | null;
  effective_extended_card_url: string | null;
  exposure_warning: string | null;
  started_at: number | null;
  last_error: string | null;
  config_revision: number;
  desired_auth_type: 'none' | 'bearer' | 'api_key';
  desired_api_key_header: string;
  desired_card_auth_required: boolean;
  credential_configured: boolean;
  effective_auth_type: string | null;
  effective_card_auth_required: boolean | null;
  security_pending_apply: boolean;
}

export interface A2AIngressDraft {
  host: string;
  port: string;
  rpc_path: string;
  protocol_version: string;
  card_path: string;
  extended_card_path: string;
  app_name: string;
  app_description: string;
  app_version: string;
  expose_reasoning: boolean;
  auth_type: 'none' | 'bearer' | 'api_key';
  api_key_header: string;
  card_auth_required: boolean;
  credential: string;
  credential_configured: boolean;
  clear_credential: boolean;
}

export type A2AIngressRequestStatus = 'processing' | 'completed' | 'failed' | 'canceled';

export interface A2AIngressRequestRecord {
  request_id: string;
  context_id: string | null;
  message_id: string | null;
  operation: string;
  status: A2AIngressRequestStatus;
  started_at: number;
  finished_at: number | null;
  duration_ms: number | null;
  error: string | null;
}

export interface A2AIngressHistory {
  items: A2AIngressRequestRecord[];
  total: number;
}

export type A2AOutboundDispatchStatus =
  | 'created'
  | 'submitting'
  | 'accepted'
  | 'working'
  | 'completed'
  | 'failed'
  | 'canceled'
  | 'rejected'
  | 'input_required'
  | 'auth_required'
  | 'unknown'
  | 'timed_out'
  | 'dispatch_failed';

export interface A2AOutboundDispatchRecord {
  dispatch_id: string;
  agent_id: string;
  agent_name: string;
  mode: 'sync' | 'async';
  status: A2AOutboundDispatchStatus;
  remote_task_id: string | null;
  created_at: string;
  updated_at: string;
  accepted_at: string | null;
  finished_at: string | null;
  error_code: string | null;
  error_summary: string | null;
}

export interface A2AOutboundDispatchHistory {
  items: A2AOutboundDispatchRecord[];
  total: number;
}

export const DEFAULT_API_KEY_HEADER = 'X-API-Key';
const RESERVED_API_KEY_HEADERS = [
  'authorization',
  'host',
  'content-length',
  'content-type',
  'connection',
  'transfer-encoding',
  'cookie',
  'set-cookie',
  'accept',
  'origin',
];

export function isValidA2AIngressApiKeyHeader(value: string): boolean {
  const header = value.trim();
  return /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(header) && !RESERVED_API_KEY_HEADERS.includes(header.toLowerCase());
}

const DEFAULT_DRAFT: A2AIngressDraft = {
  host: '127.0.0.1',
  port: '19100',
  rpc_path: '/a2a',
  protocol_version: '1.0.0',
  card_path: '/.well-known/agent-card.json',
  extended_card_path: '/agent/authenticatedExtendedCard',
  app_name: 'JiuwenSwarm Gateway A2A Server',
  app_description: 'A2A ingress for JiuwenSwarm Gateway',
  app_version: '0.1.0',
  expose_reasoning: true,
  auth_type: 'none',
  api_key_header: DEFAULT_API_KEY_HEADER,
  card_auth_required: false,
  credential: '',
  credential_configured: false,
  clear_credential: false,
};

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}
function asNumber(value: unknown, fallback = 0): number {
  if (value === null || value === undefined || value === '') return fallback;
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

export function normalizeA2AIngressSnapshot(value: unknown): A2AIngressSnapshot | null {
  if (!value || typeof value !== 'object') return null;
  const payload = value as Record<string, unknown>;
  const state = asString(payload.state);
  if (!['disabled', 'starting', 'running', 'stopping', 'error'].includes(state)) return null;
  return {
    enabled: payload.enabled === true,
    state: state as A2AIngressState,
    desired_host: asString(payload.desired_host, DEFAULT_DRAFT.host),
    desired_port: asNumber(payload.desired_port, Number(DEFAULT_DRAFT.port)),
    desired_rpc_path: asString(payload.desired_rpc_path, DEFAULT_DRAFT.rpc_path),
    desired_card_path: asString(payload.desired_card_path, DEFAULT_DRAFT.card_path),
    desired_extended_card_path: asString(payload.desired_extended_card_path, DEFAULT_DRAFT.extended_card_path),
    desired_protocol_version: asString(payload.desired_protocol_version, DEFAULT_DRAFT.protocol_version),
    desired_app_name: asString(payload.desired_app_name, DEFAULT_DRAFT.app_name),
    desired_app_description: asString(payload.desired_app_description, DEFAULT_DRAFT.app_description),
    desired_app_version: asString(payload.desired_app_version, DEFAULT_DRAFT.app_version),
    desired_expose_reasoning: payload.desired_expose_reasoning !== false,
    desired_rpc_url: asString(payload.desired_rpc_url),
    desired_card_url: asString(payload.desired_card_url),
    desired_extended_card_url: asString(payload.desired_extended_card_url),
    effective_host: typeof payload.effective_host === 'string' ? payload.effective_host : null,
    effective_port: typeof payload.effective_port === 'number' ? payload.effective_port : null,
    effective_rpc_path: typeof payload.effective_rpc_path === 'string' ? payload.effective_rpc_path : null,
    effective_card_path: typeof payload.effective_card_path === 'string' ? payload.effective_card_path : null,
    effective_extended_card_path: typeof payload.effective_extended_card_path === 'string' ? payload.effective_extended_card_path : null,
    effective_rpc_url: typeof payload.effective_rpc_url === 'string' ? payload.effective_rpc_url : null,
    effective_card_url: typeof payload.effective_card_url === 'string' ? payload.effective_card_url : null,
    effective_extended_card_url: typeof payload.effective_extended_card_url === 'string' ? payload.effective_extended_card_url : null,
    exposure_warning: typeof payload.exposure_warning === 'string' ? payload.exposure_warning : null,
    started_at: typeof payload.started_at === 'number' ? payload.started_at : null,
    last_error: typeof payload.last_error === 'string' ? payload.last_error : null,
    config_revision: asNumber(payload.config_revision),
    desired_auth_type: payload.desired_auth_type === 'bearer' || payload.desired_auth_type === 'api_key' ? payload.desired_auth_type : 'none',
    desired_api_key_header: asString(payload.desired_api_key_header, 'X-API-Key'),
    desired_card_auth_required: payload.desired_card_auth_required === true,
    credential_configured: payload.credential_configured === true,
    effective_auth_type: typeof payload.effective_auth_type === 'string' ? payload.effective_auth_type : null,
    effective_card_auth_required: typeof payload.effective_card_auth_required === 'boolean' ? payload.effective_card_auth_required : null,
    security_pending_apply: payload.security_pending_apply === true,
  };
}

export function normalizeA2AIngressHistory(value: unknown): A2AIngressHistory | null {
  if (!value || typeof value !== 'object') return null;
  const payload = value as Record<string, unknown>;
  if (!Array.isArray(payload.items)) return null;
  const statuses = new Set<A2AIngressRequestStatus>(['processing', 'completed', 'failed', 'canceled']);
  const items: A2AIngressRequestRecord[] = [];
  for (const rawItem of payload.items) {
    if (!rawItem || typeof rawItem !== 'object') continue;
    const item = rawItem as Record<string, unknown>;
    const requestId = asString(item.request_id).trim();
    const status = asString(item.status) as A2AIngressRequestStatus;
    const startedAt = asNumber(item.started_at, Number.NaN);
    if (!requestId || !statuses.has(status) || !Number.isFinite(startedAt)) continue;
    items.push({
      request_id: requestId,
      context_id: typeof item.context_id === 'string' ? item.context_id : null,
      message_id: typeof item.message_id === 'string' ? item.message_id : null,
      operation: asString(item.operation, 'message'),
      status,
      started_at: startedAt,
      finished_at: typeof item.finished_at === 'number' ? item.finished_at : null,
      duration_ms: typeof item.duration_ms === 'number' ? item.duration_ms : null,
      error: typeof item.error === 'string' ? item.error : null,
    });
  }
  return { items, total: asNumber(payload.total, items.length) };
}

export function normalizeA2AOutboundDispatchHistory(value: unknown): A2AOutboundDispatchHistory | null {
  if (!value || typeof value !== 'object') return null;
  const payload = value as Record<string, unknown>;
  if (!Array.isArray(payload.items)) return null;
  const statuses = new Set<A2AOutboundDispatchStatus>([
    'created',
    'submitting',
    'accepted',
    'working',
    'completed',
    'failed',
    'canceled',
    'rejected',
    'input_required',
    'auth_required',
    'unknown',
    'timed_out',
    'dispatch_failed',
  ]);
  const items: A2AOutboundDispatchRecord[] = [];
  for (const rawItem of payload.items) {
    if (!rawItem || typeof rawItem !== 'object') continue;
    const item = rawItem as Record<string, unknown>;
    const dispatchId = asString(item.dispatch_id).trim();
    const agentId = asString(item.agent_id).trim();
    const mode = asString(item.mode);
    const status = asString(item.status) as A2AOutboundDispatchStatus;
    const createdAt = asString(item.created_at);
    const updatedAt = asString(item.updated_at, createdAt);
    if (!dispatchId || !agentId || !['sync', 'async'].includes(mode) || !statuses.has(status) || !createdAt) continue;
    items.push({
      dispatch_id: dispatchId,
      agent_id: agentId,
      agent_name: asString(item.agent_name).trim() || agentId,
      mode: mode as 'sync' | 'async',
      status,
      remote_task_id: typeof item.remote_task_id === 'string' ? item.remote_task_id : null,
      created_at: createdAt,
      updated_at: updatedAt,
      accepted_at: typeof item.accepted_at === 'string' ? item.accepted_at : null,
      finished_at: typeof item.finished_at === 'string' ? item.finished_at : null,
      error_code: typeof item.error_code === 'string' ? item.error_code : null,
      error_summary: typeof item.error_summary === 'string' ? item.error_summary : null,
    });
  }
  return { items, total: asNumber(payload.total, items.length) };
}

export function draftFromA2AIngressSnapshot(snapshot: A2AIngressSnapshot, credential = ''): A2AIngressDraft {
  return {
    host: snapshot.desired_host,
    port: String(snapshot.desired_port),
    rpc_path: snapshot.desired_rpc_path,
    protocol_version: snapshot.desired_protocol_version,
    card_path: snapshot.desired_card_path,
    extended_card_path: snapshot.desired_extended_card_path,
    app_name: snapshot.desired_app_name,
    app_description: snapshot.desired_app_description,
    app_version: snapshot.desired_app_version,
    expose_reasoning: snapshot.desired_expose_reasoning,
    auth_type: snapshot.desired_auth_type,
    api_key_header: snapshot.desired_api_key_header,
    card_auth_required: snapshot.desired_card_auth_required,
    credential,
    credential_configured: snapshot.credential_configured,
    clear_credential: false,
  };
}

export function validateA2AIngressDraft(draft: A2AIngressDraft): string | null {
  if (!['none', 'bearer', 'api_key'].includes(draft.auth_type)) return 'auth_type';
  if (draft.credential && (!/^[!-~]{16,512}$/.test(draft.credential) || draft.clear_credential)) return 'credential';
  if (draft.auth_type !== 'none' && !draft.credential && (!draft.credential_configured || draft.clear_credential)) return 'credential';
  if (draft.card_auth_required && draft.auth_type === 'none') return 'card_auth_required';
  if (draft.auth_type === 'api_key' && !isValidA2AIngressApiKeyHeader(draft.api_key_header)) return 'api_key_header';
  if (!draft.host.trim()) return 'host';
  const port = Number(draft.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) return 'port';
  for (const field of ['rpc_path', 'card_path', 'extended_card_path'] as const) if (!draft[field].trim().startsWith('/')) return field;
  for (const field of ['protocol_version', 'app_name', 'app_version'] as const) if (!draft[field].trim()) return field;
  return null;
}

export function toA2AIngressPatch(draft: A2AIngressDraft): Record<string, string | number | boolean> {
  return {
    host: draft.host.trim(),
    port: Number(draft.port),
    rpc_path: draft.rpc_path.trim(),
    protocol_version: draft.protocol_version.trim(),
    card_path: draft.card_path.trim(),
    extended_card_path: draft.extended_card_path.trim(),
    app_name: draft.app_name.trim(),
    app_description: draft.app_description.trim(),
    app_version: draft.app_version.trim(),
    expose_reasoning: draft.expose_reasoning,
    auth_type: draft.auth_type,
    api_key_header:
      draft.auth_type === 'api_key' || isValidA2AIngressApiKeyHeader(draft.api_key_header)
        ? draft.api_key_header.trim()
        : DEFAULT_API_KEY_HEADER,
    card_auth_required: draft.card_auth_required,
    ...(draft.credential ? { credential: draft.credential } : {}),
    ...(draft.clear_credential ? { clear_credential: true } : {}),
  };
}

export function isA2AIngressTransitioning(state: A2AIngressState | undefined): boolean {
  return state === 'starting' || state === 'stopping';
}

export function canOperateA2AIngress(
  snapshot: A2AIngressSnapshot | null,
  isConnected: boolean,
  isBusy: boolean,
  isDirty: boolean,
  operation: 'enable' | 'disable' | 'reload',
): boolean {
  if (!snapshot || !isConnected || isBusy || isDirty) return false;
  if (operation === 'enable') return snapshot.state !== 'running';
  if (operation === 'disable') return snapshot.state !== 'disabled';
  return snapshot.enabled;
}

export function shouldAcceptA2AIngressResponse(responseGeneration: number, currentGeneration: number): boolean {
  return responseGeneration === currentGeneration;
}
