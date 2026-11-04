/** 企业版 Skill 上传资格：查询与申请（走 Manager user-console）。 */
import { managerAuthenticatedFetch } from '../auth/manager/authSession';
import { resolveApiUrl } from '../utils/env';

export type SkillUploadPermissionSource = 'user' | 'org' | 'none';
export type SkillUploadApplicationStatus = 'pending' | 'rejected' | null;

export interface SkillUploadPermissionState {
  allowed: boolean;
  source: SkillUploadPermissionSource;
  /** 未授权时的最近一张申请状态；`allowed=true` 时恒为 null。 */
  application_status: SkillUploadApplicationStatus;
  order_num: string | null;
  reject_comment: string | null;
  checked_at: string | null;
}

export type SkillUploadPermissionErrorCode =
  | 'SKILL_UPLOAD_FEATURE_DISABLED'
  | 'SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE'
  | 'CONTEXT_NOT_ADMITTED'
  | 'REQUEST_FAILED';

export class SkillUploadPermissionError extends Error {
  constructor(
    readonly code: SkillUploadPermissionErrorCode,
    message: string,
    readonly status = 0,
  ) {
    super(message);
    this.name = 'SkillUploadPermissionError';
  }
}

interface ManagerResponse<T> {
  code: number;
  message?: string;
  data: T;
}

function messageOf(body: unknown, fallback: string): string {
  if (body && typeof body === 'object') {
    const value = body as Record<string, unknown>;
    if (typeof value.detail === 'string' && value.detail.trim()) return value.detail.trim();
    if (typeof value.message === 'string' && value.message.trim()) return value.message.trim();
  }
  return fallback;
}

async function requestManager<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await managerAuthenticatedFetch(resolveApiUrl(path), {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init.headers || {}) },
    });
  } catch (error) {
    throw new SkillUploadPermissionError(
      'SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE',
      `Permission request failed: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // 非 JSON 响应只保留 HTTP 状态语义。
  }
  if (!response.ok) {
    const detail = messageOf(body, `HTTP ${response.status}`);
    if (response.status === 403 && detail === 'SKILL_UPLOAD_FEATURE_DISABLED') {
      throw new SkillUploadPermissionError('SKILL_UPLOAD_FEATURE_DISABLED', detail, 403);
    }
    if (response.status === 403) {
      throw new SkillUploadPermissionError('CONTEXT_NOT_ADMITTED', detail, 403);
    }
    throw new SkillUploadPermissionError(
      'SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE',
      detail,
      response.status,
    );
  }
  const result = body as ManagerResponse<T> | null;
  if (!result || typeof result !== 'object' || result.code !== 200 || !result.data) {
    throw new SkillUploadPermissionError(
      'SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE',
      messageOf(body, 'Upload permission service returned an invalid response'),
    );
  }
  return result.data;
}

export async function fetchSkillUploadPermission(
  jiuwenclawId: string,
): Promise<SkillUploadPermissionState> {
  const query = jiuwenclawId ? `?jiuwenclaw_id=${encodeURIComponent(jiuwenclawId)}` : '';
  const data = await requestManager<SkillUploadPermissionState>(
    `/manager-api/v1/user-console/skills/upload-permission${query}`,
    { method: 'GET', headers: {} },
  );
  return {
    allowed: Boolean(data.allowed),
    source: data.source === 'user' || data.source === 'org' ? data.source : 'none',
    application_status:
      data.application_status === 'pending' || data.application_status === 'rejected'
        ? data.application_status
        : null,
    order_num: data.order_num ?? null,
    reject_comment: data.reject_comment ?? null,
    checked_at: data.checked_at ?? null,
  };
}

export interface ApprovalCandidate {
  user_id: string;
  display_name?: string;
}

/** 可选审批人：与配额扩容申请共用同一候选接口。 */
export async function fetchApprovalCandidates(): Promise<ApprovalCandidate[]> {
  const data = await requestManager<{ items: ApprovalCandidate[] }>(
    '/manager-api/v1/user-console/approvals/candidates',
    { method: 'GET', headers: {} },
  );
  return (data.items ?? []).filter((item) => item && item.user_id);
}

export interface SkillUploadPermissionRequest {
  jiuwenclawId: string;
  groupId: string;
  botId: string;
  reason: string;
  approverId: string;
}

export async function submitSkillUploadPermission(
  request: SkillUploadPermissionRequest,
): Promise<{ order_num: string; status: string; reused: boolean }> {
  const data = await requestManager<{
    order_num: string;
    status: string;
    reused?: boolean;
  }>('/manager-api/v1/user-console/skills/upload-permission-requests', {
    method: 'POST',
    body: JSON.stringify({
      jiuwenclaw_id: request.jiuwenclawId,
      group_id: request.groupId,
      bot_id: request.botId,
      reason: request.reason,
      approver_id: request.approverId,
    }),
  });
  return {
    order_num: String(data.order_num || ''),
    status: String(data.status || 'pending'),
    reused: Boolean(data.reused),
  };
}
