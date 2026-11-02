/**
 * Skill 上传资格 API（企业版 Manager 用户控制台）。
 *
 * Manager 侧以 `skill_upload_permission` 统一授权记录（管理员直授 + 审批通过）
 * 维护资格；本模块只负责用户面读取/申请，不做本地判定。
 */
import { hasManagerSessionCredentials, managerAuthenticatedFetch } from '../auth/manager/authSession';
import { resolveApiUrl } from '../utils/env';

interface ManagerResponse<T> {
  code: number;
  message?: string;
  data: T;
}

export interface SkillUploadPermissionInfo {
  allowed?: boolean;
  application_status?: 'pending' | 'approved' | 'none' | string;
  order_num?: string | null;
}

export type SkillUploadPermissionState =
  | { phase: 'checking' }
  | { phase: 'allowed' }
  | { phase: 'pending'; orderNum: string | null }
  | { phase: 'not-allowed' }
  | { phase: 'unavailable' };

export interface ApproverCandidate {
  user_id: string;
  display_name: string;
}

export interface SkillUploadPermissionContext {
  jiuwenclawId: string;
  groupId: string;
  botId: string;
}

function responseMessage(body: unknown, fallback: string): string {
  if (body && typeof body === 'object') {
    const value = body as Record<string, unknown>;
    if (typeof value.detail === 'string' && value.detail.trim()) return value.detail;
    if (typeof value.message === 'string' && value.message.trim()) return value.message;
  }
  return fallback;
}

async function requestManagerJson<T>(path: string, init: RequestInit = {}): Promise<ManagerResponse<T>> {
  const response = await managerAuthenticatedFetch(resolveApiUrl(path), init);
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // 状态码兜底：非 JSON 响应按 HTTP 状态处理。
  }
  if (!response.ok) throw new Error(responseMessage(body, `HTTP ${response.status}`));
  const result = body as ManagerResponse<T>;
  if (!result || result.code !== 200) throw new Error(result?.message || `code ${result?.code}`);
  return result;
}

/** 企业版（manager 会话）才有资格校验的前提；个人部署直接返回 null。 */
export function skillUploadPermissionAvailable(): boolean {
  return hasManagerSessionCredentials();
}

export async function fetchSkillUploadPermission(
  contextId?: string,
): Promise<SkillUploadPermissionInfo> {
  const qs = contextId ? `?context_id=${encodeURIComponent(contextId)}` : '';
  const result = await requestManagerJson<SkillUploadPermissionInfo>(
    `/manager-api/v1/user-console/skills/upload-permission${qs}`,
  );
  return result.data ?? {};
}

export async function fetchApproverCandidates(): Promise<ApproverCandidate[]> {
  const result = await requestManagerJson<{ items?: ApproverCandidate[] }>(
    '/manager-api/v1/user-console/approvals/candidates',
  );
  return result.data?.items ?? [];
}

export interface SubmitSkillUploadRequestInput extends SkillUploadPermissionContext {
  reason: string;
  approverId: string;
}

export async function submitSkillUploadRequest(
  input: SubmitSkillUploadRequestInput,
): Promise<{ order_num: string | null; status: string; already_granted?: boolean }> {
  const result = await requestManagerJson<{
    order_num: string | null;
    status: string;
    already_granted?: boolean;
  }>('/manager-api/v1/user-console/skills/upload-permission-requests', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      context_id: input.jiuwenclawId,
      group_id: input.groupId,
      bot_id: input.botId,
      reason: input.reason,
      approver_id: input.approverId,
    }),
  });
  return result.data ?? {};
}

interface CancelSkillUploadRequestResult {
  order_num: string;
  status: string;
}

/** 撤回本人 pending 的 Skill 上传资格申请；非 pending 或非本人由后端拒绝。 */
export async function cancelSkillUploadRequest(orderNum: string): Promise<CancelSkillUploadRequestResult> {
  const result = await requestManagerJson<CancelSkillUploadRequestResult>(
    `/manager-api/v1/user-console/approvals/${encodeURIComponent(orderNum)}/cancel`,
    { method: 'POST' },
  );
  return result.data;
}

/** 把 Manager 资格信息规整为 UI 可用的门控状态。 */
export function toPermissionState(info: SkillUploadPermissionInfo | null): SkillUploadPermissionState {
  if (!info) return { phase: 'unavailable' };
  if (info.allowed) return { phase: 'allowed' };
  if (info.order_num) return { phase: 'pending', orderNum: info.order_num };
  return { phase: 'not-allowed' };
}
