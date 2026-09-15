import { webRequest } from '../../services/webClient';
import type { WebError } from '../../types';
import type { WorkMode } from './projectTypes';

/**
 * 项目与会话归档 API client。
 *
 * 归档是独立于活跃资源的新协议：`project.archived.list` / `session.archived.list`
 * 只返回已归档资源，旧协议 `project.remove` / `project.restore` 已废弃，禁止再调用。
 * 请求通过注入的 request 函数发出（默认 `webRequest`），便于测试替换；
 * 请求失败由页面呈现错误态，不伪造空数据。
 */

export interface ArchivedProject {
  project_id: string;
  name: string;
  project_dir: string;
  work_mode: WorkMode;
  hidden: true;
  archived_at: number;
  created_at: number;
  updated_at: number;
  stop_pending: boolean;
}

export interface ArchivedSession {
  session_id: string;
  title: string;
  project_id: string;
  project_name: string | null;
  project_archived: boolean;
  work_mode: WorkMode;
  archived: true;
  archived_at: number;
  stop_pending: boolean;
}

export interface ArchivedListResponse<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

/** `project.archived.list` 的后端响应。页面层会统一规整为 `ArchivedListResponse`。 */
export interface ArchivedProjectListResponse {
  projects: ArchivedProject[];
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

/** 后端在归档/恢复响应中附带的安全提示（如 cron 未自动恢复），仅作辅助详情展示。 */
export interface ArchiveWarning {
  code: string;
  message?: string;
}

export interface ArchivedListParams {
  work_mode?: WorkMode;
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
 * `project.archive` / `project.delete` 的部分失败响应。
 * 后端可能已完成部分阶段（如 cron 停用成功、删除记录失败），
 * payload 会通过 WebError.payload 透传到这里；不能当“全部失败”处理。
 */
export interface ProjectOperationFailurePayload {
  operation_id?: string;
  project_id?: string;
  phase?: string;
  retryable?: boolean;
  completed_session_ids?: string[];
  completed_cron_job_ids?: string[];
  failed_items?: Array<{
    resource_type: 'session' | 'cron' | 'project';
    resource_id: string;
    code: string;
    error: string;
  }>;
}

export interface ProjectOperationFailure {
  /** PARTIAL_PROJECT_ARCHIVE_FAILED / PARTIAL_PROJECT_DELETE_FAILED 等错误码，仅用于分支判断。 */
  code: string;
  phase: string;
  retryable: boolean;
  /** 后端提供的安全错误文本，可作为辅助详情展示。 */
  detail: string | null;
}

interface ProjectOperationResponse {
  project_id?: string;
  warnings?: ArchiveWarning[];
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

/** 解析 `project.archive` / `project.delete` 的部分失败 payload；非部分失败返回 null。 */
export function parseProjectOperationFailure(error: unknown): ProjectOperationFailure | null {
  const code = getArchiveErrorCode(error);
  if (code !== 'PARTIAL_PROJECT_ARCHIVE_FAILED' && code !== 'PARTIAL_PROJECT_DELETE_FAILED') {
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
    listArchivedProjects: (params: ArchivedListParams) =>
      request<ArchivedProjectListResponse>('project.archived.list', {
        ...(params.work_mode ? { work_mode: params.work_mode } : {}),
        ...(params.keyword ? { keyword: params.keyword } : {}),
        ...(params.limit !== undefined ? { limit: params.limit } : {}),
        ...(params.offset !== undefined ? { offset: params.offset } : {}),
      }),
    listArchivedSessions: (params: ArchivedListParams) =>
      request<ArchivedSessionListResponse>('session.archived.list', {
        ...(params.work_mode ? { work_mode: params.work_mode } : {}),
        ...(params.keyword ? { keyword: params.keyword } : {}),
        ...(params.limit !== undefined ? { limit: params.limit } : {}),
        ...(params.offset !== undefined ? { offset: params.offset } : {}),
      }),
    archiveProject: (projectId: string) =>
      request<ProjectOperationResponse>('project.archive', { project_id: projectId }),
    unarchiveProject: (projectId: string) =>
      request<ProjectOperationResponse>('project.unarchive', { project_id: projectId }),
    deleteArchivedProject: (projectId: string) =>
      request<ProjectOperationResponse>('project.delete', { project_id: projectId }),
    archiveSession: (sessionId: string) =>
      request<BatchSessionArchiveResponse>('session.archive', { session_ids: [sessionId] }),
    unarchiveSession: (sessionId: string) =>
      request<BatchSessionArchiveResponse>('session.unarchive', { session_ids: [sessionId] }),
    deleteSession: (sessionId: string) =>
      request<{ session_id?: string }>('session.delete', { session_id: sessionId }),
  };
}

export const archivedTaskClient = createArchivedTaskClient(webRequest);
