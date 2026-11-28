/**
 * session.create: AgentServer allocates the ID; timeout retries use create_token.
 *
 * 冷启动时 Runtime Manager 会快失败返回 `NO_POD_AVAILABLE`，Gateway 折叠成
 * `SERVICE_UNAVAILABLE`。首次进入欢迎页发送消息时偶发撞上这个窗口，这里
 * 对 `NO_POD_AVAILABLE` 单独做最多 30s 的短重试，并复用同一个 `create_token`
 * （AgentServer 按 token 幂等，重复请求不会建出多个会话）。
 */

import type { WorkMode } from '../../features/workspace/projectTypes';

export const SESSION_CREATE_TIMEOUT_MS = 60_000;
/** Kept under the old export names for source compatibility. */
export const SESSION_CREATE_METADATA_POLL_ATTEMPTS = 5;
export const SESSION_CREATE_METADATA_POLL_INTERVAL_MS = 500;

/**
 * NO_POD_AVAILABLE 的重试预算：总时长不超过 30s。
 * 现场实测一次 Pod deploy 约 22.06s，30s 覆盖一个完整冷启动周期并留余量；
 * 间隔 1s→2s 递增（服务端 `retry_after` 为 1s）。
 */
export const SESSION_CREATE_NO_POD_RETRY_WINDOW_MS = 30_000;
export const SESSION_CREATE_NO_POD_RETRY_BASE_DELAY_MS = 1_000;
export const SESSION_CREATE_NO_POD_RETRY_MAX_DELAY_MS = 2_000;

export type SessionCreateRequestFn = <T = unknown>(
  method: string,
  params?: Record<string, unknown>,
  options?: { timeoutMs?: number },
) => Promise<T>;

export interface SessionCreatePayload {
  session_id?: string;
  sessionId?: string;
  project_id?: string;
  projectId?: string;
  project_dir?: string;
  projectDir?: string;
  work_mode?: WorkMode | string;
  workMode?: WorkMode | string;
  persist_session?: boolean;
  persistSession?: boolean;
  session_equipment?: CreatedConversationSession['session_equipment'];
}

export interface CreatedConversationSession {
  session_id: string;
  project_id?: string;
  project_dir?: string;
  work_mode?: WorkMode;
  persist_session: boolean;
  session_equipment?: { agent_template_name?: string; plugin_names?: string[]; mcp?: string[] };
}

export interface CreateConversationSessionOptions {
  metadataPollAttempts?: number;
  metadataPollIntervalMs?: number;
  noPodRetryWindowMs?: number;
  noPodRetryBaseDelayMs?: number;
  noPodRetryMaxDelayMs?: number;
  sleep?: (ms: number) => Promise<void>;
}

export interface PersistSessionCommand {
  content: string;
  persistSession: boolean;
}

/**
 * Persist Session is a creation-time choice.  Keep the command parsing in the
 * client so the control prefix never becomes part of the user message or the
 * generated session title.
 */
export function parsePersistSessionCommand(input: string): PersistSessionCommand {
  const match = input.match(/^\/persist(?=$|\s)/i);
  if (!match) return { content: input, persistSession: false };

  const remainder = input.slice(match[0].length).trim();
  return { content: remainder, persistSession: true };
}

function normalizeWorkMode(value: unknown): WorkMode | undefined {
  return value === 'work' || value === 'code' || value === 'design' ? value : undefined;
}

function errorCode(error: unknown): string | undefined {
  if (!error || typeof error !== 'object') return undefined;
  const code = (error as { code?: unknown }).code;
  return typeof code === 'string' ? code : undefined;
}

export function isRequestTimeoutError(error: unknown): boolean {
  return errorCode(error) === 'REQUEST_TIMEOUT';
}

/**
 * Runtime Manager 的 `NO_POD_AVAILABLE`（冷启动窗口内没有可用 Pod）。
 * Gateway 会把它折叠成 `SERVICE_UNAVAILABLE`，错误码因此可能丢失，
 * 所以同时用文案兜底识别。
 */
const NO_POD_AVAILABLE_CODE = 'NO_POD_AVAILABLE';
const NO_POD_AVAILABLE_HINTS = ['NO_POD_AVAILABLE', 'Pod 冷启动中'];

function errorText(error: unknown): string {
  if (typeof error === 'string') return error;
  if (!error || typeof error !== 'object') return '';
  const record = error as {
    code?: unknown;
    message?: unknown;
    error?: unknown;
    payload?: unknown;
  };
  const parts: unknown[] = [record.code, record.message, record.error];
  if (record.payload && typeof record.payload === 'object') {
    const payload = record.payload as { code?: unknown; message?: unknown; error?: unknown };
    parts.push(payload.code, payload.message, payload.error);
  }
  return parts.filter((value): value is string => typeof value === 'string').join(' | ');
}

export function isNoPodAvailableError(error: unknown): boolean {
  if (errorCode(error) === NO_POD_AVAILABLE_CODE) return true;
  const text = errorText(error);
  return NO_POD_AVAILABLE_HINTS.some((hint) => text.includes(hint));
}

export function isAlreadyExistsError(error: unknown): boolean {
  return errorCode(error) === 'ALREADY_EXISTS';
}

export function isWorkspaceQuotaError(error: unknown): boolean {
  if (typeof error === 'string') {
    return error.includes('WORKSPACE_QUOTA_EXCEEDED');
  }
  if (!error || typeof error !== 'object') return false;
  const record = error as { code?: unknown; message?: unknown; error?: unknown };
  return [record.code, record.message, record.error].some(
    (value) => typeof value === 'string' && value.includes('WORKSPACE_QUOTA_EXCEEDED'),
  );
}

export function resolveCreatedSessionId(
  payload: SessionCreatePayload | null | undefined,
): string | undefined {
  const direct = payload?.session_id ?? payload?.sessionId;
  return typeof direct === 'string' && direct.trim() ? direct.trim() : undefined;
}

function normalizeCreatedSession(payload: SessionCreatePayload): CreatedConversationSession {
  const sessionId = resolveCreatedSessionId(payload);
  if (!sessionId) throw new Error('session.create did not return a session id');
  return {
    session_id: sessionId,
    project_id: payload.project_id ?? payload.projectId,
    project_dir: payload.project_dir ?? payload.projectDir,
    work_mode: normalizeWorkMode(payload.work_mode ?? payload.workMode),
    persist_session: (payload.persist_session ?? payload.persistSession) === true,
    ...(payload.session_equipment ? { session_equipment: payload.session_equipment } : {}),
  };
}

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function invokeSessionCreate(
  request: SessionCreateRequestFn,
  createParams: Record<string, unknown>,
): Promise<CreatedConversationSession> {
  const payload = await request<SessionCreatePayload>('session.create', createParams, {
    timeoutMs: SESSION_CREATE_TIMEOUT_MS,
  });
  return normalizeCreatedSession(payload);
}

export async function createConversationSession(
  request: SessionCreateRequestFn,
  createParams: Record<string, unknown>,
  options: CreateConversationSessionOptions = {},
): Promise<CreatedConversationSession> {
  if (typeof createParams.create_token !== 'string' || !createParams.create_token.trim()) {
    throw new Error('session.create requires create_token');
  }
  const timeoutAttempts = Math.max(
    1,
    options.metadataPollAttempts ?? SESSION_CREATE_METADATA_POLL_ATTEMPTS,
  );
  const timeoutIntervalMs = Math.max(
    0,
    options.metadataPollIntervalMs ?? SESSION_CREATE_METADATA_POLL_INTERVAL_MS,
  );
  const noPodWindowMs = Math.max(
    0,
    options.noPodRetryWindowMs ?? SESSION_CREATE_NO_POD_RETRY_WINDOW_MS,
  );
  const noPodBaseDelayMs = Math.max(
    0,
    options.noPodRetryBaseDelayMs ?? SESSION_CREATE_NO_POD_RETRY_BASE_DELAY_MS,
  );
  const noPodMaxDelayMs = Math.max(
    0,
    options.noPodRetryMaxDelayMs ?? SESSION_CREATE_NO_POD_RETRY_MAX_DELAY_MS,
  );
  const sleep = options.sleep ?? defaultSleep;
  const noPodDeadline = Date.now() + noPodWindowMs;
  let timeoutRetries = 0;
  let noPodRetries = 0;
  for (;;) {
    try {
      return await invokeSessionCreate(request, createParams);
    } catch (error) {
      // 响应丢失：AgentServer 可能已经建好会话，用同一个 create_token
      // 立刻再问一次即可幂等回放。
      if (isRequestTimeoutError(error)) {
        if (timeoutRetries + 1 >= timeoutAttempts) throw error;
        timeoutRetries += 1;
        if (timeoutIntervalMs > 0) await sleep(timeoutIntervalMs);
        continue;
      }
      // 冷启动窗口：只对 NO_POD_AVAILABLE 在 30s 预算内继续等，其余错误照旧抛出。
      if (isNoPodAvailableError(error)) {
        const remaining = noPodDeadline - Date.now();
        if (remaining > 0) {
          const delayMs = Math.min(
            noPodBaseDelayMs * 2 ** noPodRetries,
            noPodMaxDelayMs,
          );
          noPodRetries += 1;
          await sleep(Math.min(delayMs, remaining));
          continue;
        }
      }
      throw error;
    }
  }
}
