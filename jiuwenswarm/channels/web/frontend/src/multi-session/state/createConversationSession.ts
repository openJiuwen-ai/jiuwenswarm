/** session.create: AgentServer allocates the ID; timeout retries use create_token. */

import type { WorkMode } from '../../features/workspace/projectTypes';

export const SESSION_CREATE_TIMEOUT_MS = 60_000;
/** Kept under the old export names for source compatibility. */
export const SESSION_CREATE_METADATA_POLL_ATTEMPTS = 5;
export const SESSION_CREATE_METADATA_POLL_INTERVAL_MS = 500;

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
}

export interface CreatedConversationSession {
  session_id: string;
  project_id?: string;
  project_dir?: string;
  work_mode?: WorkMode;
}

export interface CreateConversationSessionOptions {
  metadataPollAttempts?: number;
  metadataPollIntervalMs?: number;
  sleep?: (ms: number) => Promise<void>;
}

function normalizeWorkMode(value: unknown): WorkMode | undefined {
  return value === 'work' || value === 'code' ? value : undefined;
}

function errorCode(error: unknown): string | undefined {
  if (!error || typeof error !== 'object') return undefined;
  const code = (error as { code?: unknown }).code;
  return typeof code === 'string' ? code : undefined;
}

export function isRequestTimeoutError(error: unknown): boolean {
  return errorCode(error) === 'REQUEST_TIMEOUT';
}

export function isAlreadyExistsError(error: unknown): boolean {
  return errorCode(error) === 'ALREADY_EXISTS';
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
  const attempts = Math.max(1, options.metadataPollAttempts ?? SESSION_CREATE_METADATA_POLL_ATTEMPTS);
  const intervalMs = Math.max(
    0,
    options.metadataPollIntervalMs ?? SESSION_CREATE_METADATA_POLL_INTERVAL_MS,
  );
  const sleep = options.sleep ?? defaultSleep;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (attempt > 0 && intervalMs > 0) await sleep(intervalMs);
    try {
      return await invokeSessionCreate(request, createParams);
    } catch (error) {
      if (!isRequestTimeoutError(error) || attempt === attempts - 1) throw error;
    }
  }
  throw new Error('session.create retry exhausted');
}
