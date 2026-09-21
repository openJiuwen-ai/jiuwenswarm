import { webRequest } from '../../services/webClient';
import type { WebError } from '../../types';
import type { WorkMode } from './projectTypes';

/**
 * 会话归档 API client。
 *
 * 归档协议以《会话与项目归档删除设计》为准：项目不再有归档状态，
 * `project.archive` / `project.unarchive` / `project.archived.list` 已移除，
 * 禁止再调用；项目级操作只剩 `project.delete`（直接级联删除），
 * 项目维度的批量会话操作由 projectRegistryClient 承担。
 * 请求通过注入的 request 函数发出（默认 `webRequest`），便于测试替换；
 * 请求失败由页面呈现错误态，不伪造空数据。
 */

export interface ArchivedSession {
  session_id: string;
  title: string;
  project_id: string;
  project_name: string | null;
  work_mode: WorkMode;
  archived: true;
  archived_at: number;
  stop_pending: boolean;
  /** 生命周期投影；仅在该会话存在未完成操作时非空。 */
  lifecycle_operation?: Record<string, unknown> | null;
  execution_blocked?: boolean;
}

export interface ArchivedListResponse<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

/** `session.archived.list` 的后端响应。页面层会统一规整为 `ArchivedListResponse`。 */
export interface ArchivedSessionListResponse {
  sessions: ArchivedSession[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

/** 后端在归档/恢复响应中附带的安全提示（如写入排空告警），仅作辅助详情展示。 */
export interface ArchiveWarning {
  code: string;
  message?: string;
}

export interface ArchivedListParams {
  work_mode?: WorkMode;
  project_id?: string;
  keyword?: string;
  limit?: number;
  offset?: number;
}

/** 批量会话操作（session.archive / session.unarchive）的单条结果。 */
export interface BatchSessionResultEntry {
  session_id: string;
  ok: boolean;
  /** 失败项的业务错误码；批量结果不嵌套在 error 对象中。 */
  code?: string;
  /** 失败项的可读错误信息。 */
  error?: string;
  stop_pending?: boolean;
  warnings?: ArchiveWarning[];
}

export interface BatchSessionArchiveResponse {
  succeeded_count: number;
  failed_count: number;
  results: BatchSessionResultEntry[];
}

/**
 * `project.delete` 的部分失败响应。
 * 后端可能已完成部分阶段（如 cron 删除成功、会话删除失败），
 * payload 会通过 WebError.payload 透传到这里；不能当“全部失败”处理。
 */
export interface ProjectOperationFailurePayload {
  operation_id?: string;
  project_id?: string;
  phase?: string;
  retryable?: boolean;
  completed_session_ids?: string[];
  completed_conversation_session_ids?: string[];
  completed_cron_job_ids?: string[];
  failed_items?: Array<{
    resource_type: 'session' | 'cron' | 'project';
    resource_id: string;
    code: string;
    error: string;
  }>;
}

export interface ProjectOperationFailure {
  /** `PARTIAL_PROJECT_DELETE_FAILED` 错误码，仅用于分支判断。 */
  code: string;
  phase: string;
  retryable: boolean;
  /** 后端提供的安全错误文本，可作为辅助详情展示。 */
  detail: string | null;
  deletedConversations: number;
  deletedCronJobs: number;
}

type ArchiveRequest = <T = unknown>(
  method: string,
  params?: Record<string, unknown>,
  options?: { timeoutMs?: number },
) => Promise<T>;

export function getArchiveErrorCode(error: unknown): string | null {
  if (!error || typeof error !== 'object' || !('code' in error)) return null;
  const code = (error as { code?: unknown }).code;
  return typeof code === 'string' && code ? code : null;
}

function isArchiveErrorRetriable(error: unknown): boolean {
  if (!error || typeof error !== 'object' || !('retriable' in error)) return false;
  return (error as { retriable?: unknown }).retriable === true;
}

function getArchiveErrorDetail(error: unknown): string | null {
  if (error instanceof Error && error.message) return error.message;
  return null;
}

/** 解析 `project.delete` 的部分失败 payload；非部分失败返回 null。 */
export function parseProjectOperationFailure(error: unknown): ProjectOperationFailure | null {
  const code = getArchiveErrorCode(error);
  if (code !== 'PARTIAL_PROJECT_DELETE_FAILED') {
    return null;
  }
  const webError = error as WebError;
  const payload = webError.payload as ProjectOperationFailurePayload | undefined;
  const detail = payload?.failed_items?.find((item) => typeof item?.error === 'string')?.error
    || getArchiveErrorDetail(error)
    || null;
  return {
    code,
    phase: (payload && typeof payload.phase === 'string' && payload.phase) || '',
    retryable: (payload && payload.retryable === true) || isArchiveErrorRetriable(error),
    detail,
    deletedConversations: payload?.completed_conversation_session_ids?.length ?? 0,
    deletedCronJobs: payload?.completed_cron_job_ids?.length ?? 0,
  };
}

/** 批量会话恢复/归档响应中取单个会话的结果；信封 ok 不代表该会话成功。 */
export function findBatchSessionResult(
  response: BatchSessionArchiveResponse,
  sessionId: string,
): BatchSessionResultEntry | null {
  const results = Array.isArray(response?.results) ? response.results : [];
  return results.find((entry) => entry && entry.session_id === sessionId) ?? null;
}

export function createArchivedTaskClient(request: ArchiveRequest) {
  return {
    // 归档列表是服务端全量扫描后的分页,慢环境(杀软扫描/冷缓存/大量归档)
    // 可能超过 webRequest 15s 默认超时;给足预算避免"偶现加载失败"。
    listArchivedSessions: (params: ArchivedListParams) =>
      request<ArchivedSessionListResponse>('session.archived.list', {
        ...(params.work_mode ? { work_mode: params.work_mode } : {}),
        ...(params.project_id ? { project_id: params.project_id } : {}),
        ...(params.keyword ? { keyword: params.keyword } : {}),
        ...(params.limit !== undefined ? { limit: params.limit } : {}),
        ...(params.offset !== undefined ? { offset: params.offset } : {}),
      }, { timeoutMs: 30000 }),
    archiveSession: (sessionId: string) =>
      request<BatchSessionArchiveResponse>('session.archive', { session_ids: [sessionId] }),
    unarchiveSession: (sessionId: string) =>
      request<BatchSessionArchiveResponse>('session.unarchive', { session_ids: [sessionId] }),
    /** 批量恢复（如项目批量归档 toast 的撤销）；逐项结果必须检查 ok。 */
    unarchiveSessions: (sessionIds: string[]) =>
      request<BatchSessionArchiveResponse>('session.unarchive', { session_ids: sessionIds }),
    deleteSession: (sessionId: string) =>
      request<{ session_id?: string; project_id?: string }>('session.delete', { session_id: sessionId }),
  };
}

export const archivedTaskClient = createArchivedTaskClient(webRequest);
