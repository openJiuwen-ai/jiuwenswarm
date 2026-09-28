import { webRequest } from '../../services/webClient';
import type { WorkMode } from './projectTypes';

/**
 * 会话归档 API client。
 *
 * 归档协议以《会话与项目归档删除设计》为准：项目不再有归档状态，
 * `project.archive` / `project.unarchive` / `project.archived.list` 已移除，
 * 禁止再调用；项目隐藏/恢复使用 `project.remove` / `project.restore`，
 * 项目维度的批量会话操作由 projectRegistryClient 承担。
 * 请求通过注入的 request 函数发出（默认 `webRequest`），便于测试替换；
 * 请求失败由页面呈现错误态，不伪造空数据。
 */

export interface ArchivedSession {
  session_id: string;
  title: string;
  project_id: string;
  project_name: string | null;
  /** 会话所属项目已被移除：归档页仍展示，撤销归档会连带恢复该项目。 */
  project_hidden?: boolean;
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
  /**
   * SESSION_BUSY 的细分：会话只是还在收尾，会自行结束——提示稍后重试，
   * 而不是让用户先手动停止一个已停过的会话。
   */
  finishing?: boolean;
  /** finishing 为真时的成因：常驻 subagent 正在退出，而非 Team 回合收尾。 */
  subagent_finishing?: boolean;
  warnings?: ArchiveWarning[];
}

export interface BatchSessionArchiveResponse {
  succeeded_count: number;
  failed_count: number;
  results: BatchSessionResultEntry[];
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

/**
 * SESSION_BUSY 的收尾细分标记。兼容两种错误形态：workspaceStore 归档把
 * 批量结果项的 finishing 挂到 Error 上；直接走 webRequest 的入口（删除）
 * 由 WebError.payload 透传服务端平铺的 details。
 */
export function getArchiveErrorFinishing(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false;
  if ('finishing' in error) {
    return (error as { finishing?: unknown }).finishing === true;
  }
  const payload = (error as { payload?: unknown }).payload;
  if (payload && typeof payload === 'object' && 'finishing' in payload) {
    return (payload as { finishing?: unknown }).finishing === true;
  }
  return false;
}

/** 收尾成因：常驻 subagent 正在退出，或 swarm flow 已结束、Team 回合仍在收尾。 */
export type ArchiveFinishingCause = 'subagent' | 'team';

function readDetailFlag(source: unknown, key: string): boolean {
  if (!source || typeof source !== 'object') return false;
  return (source as Record<string, unknown>)[key] === true;
}

/** 找出承载 details 的对象：Error 自身（批量路径）或 WebError.payload（直连路径）。 */
function finishingDetailSource(error: unknown): unknown {
  if (!error || typeof error !== 'object') return null;
  if ('finishing' in error) return error;
  return (error as { payload?: unknown }).payload ?? null;
}

/**
 * SESSION_BUSY 的收尾成因。同样是"会自行结束"，subagent 退出与 Team 回合
 * 收尾对用户是两件事，文案也要分开。未标记收尾时返回 null。
 */
export function getArchiveErrorFinishingCause(
  error: unknown,
): ArchiveFinishingCause | null {
  if (!error || typeof error !== 'object') return null;
  if (!getArchiveErrorFinishing(error)) return null;
  return readDetailFlag(finishingDetailSource(error), 'subagent_finishing')
    ? 'subagent'
    : 'team';
}

/** 批量结果项的收尾成因；非收尾项返回 null。 */
export function batchResultFinishingCause(
  entry: { finishing?: boolean; subagent_finishing?: boolean } | null | undefined,
): ArchiveFinishingCause | null {
  if (!entry || entry.finishing !== true) return null;
  return entry.subagent_finishing === true ? 'subagent' : 'team';
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
