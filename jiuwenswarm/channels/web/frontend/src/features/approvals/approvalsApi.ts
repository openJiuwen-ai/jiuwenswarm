/**
 * 用户面通用审批 API（Manager ``/user-console/approvals/*``）。
 * 扩容提交仍走 workspace/expand-requests；列表与撤回对本模块通用。
 */

import { managerAuthenticatedFetch } from '../../auth/manager/authSession';

export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'cancelled';

export interface ApprovalApprover {
  user_id: string;
  display_name: string;
}

export interface ApprovalOrderItem {
  order_num: string;
  cluster_id?: string;
  business_type: string;
  title: string;
  status: ApprovalStatus | string;
  reason: string;
  group_id?: string;
  bot_id?: string;
  applicant_id?: string;
  approver_id?: string | null;
  apply_data?: Record<string, unknown>;
  result_data?: Record<string, unknown> | null;
  latest_reject_comment?: string;
  current_approvers: ApprovalApprover[];
  created_at: string;
  updated_at?: string;
}

interface ManagerEnvelope<T> {
  code: number;
  message?: string;
  data: T;
}

async function managerJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await managerAuthenticatedFetch(path, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...Object.fromEntries(new Headers(init.headers).entries()),
    },
  });
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // keep null
  }
  if (!response.ok) {
    const detail =
      body && typeof body === 'object'
        ? String(
            (body as Record<string, unknown>).detail ||
              (body as Record<string, unknown>).message ||
              '',
          )
        : '';
    throw new Error(detail || `HTTP ${response.status}`);
  }
  const envelope = body as ManagerEnvelope<T>;
  if (!envelope || envelope.code !== 200) {
    throw new Error(envelope?.message || 'manager request failed');
  }
  return envelope.data;
}

export async function listMyApprovals(options?: {
  businessType?: string;
  status?: string;
  /** 匹配单号、标题、申请说明 */
  search?: string;
}): Promise<ApprovalOrderItem[]> {
  const params = new URLSearchParams();
  if (options?.businessType) params.set('business_type', options.businessType);
  if (options?.status) params.set('status', options.status);
  const search = options?.search?.trim();
  if (search) params.set('search', search);
  const query = params.toString() ? `?${params.toString()}` : '';
  const payload = await managerJson<{ items?: ApprovalOrderItem[] }>(
    `/manager-api/v1/user-console/approvals/mine${query}`,
  );
  return Array.isArray(payload?.items) ? payload.items : [];
}

/** 扩容申请可选审批人（持有 approval:act）。 */
export async function listApproverCandidates(): Promise<ApprovalApprover[]> {
  const payload = await managerJson<{ items?: ApprovalApprover[] }>(
    '/manager-api/v1/user-console/approvals/candidates',
  );
  return Array.isArray(payload?.items) ? payload.items : [];
}

export async function cancelMyApproval(orderNum: string): Promise<{
  order_num: string;
  status: string;
}> {
  const payload = await managerJson<{ order_num?: string; status?: string }>(
    `/manager-api/v1/user-console/approvals/${encodeURIComponent(orderNum)}/cancel`,
    { method: 'POST', body: '{}' },
  );
  return {
    order_num: String(payload?.order_num || orderNum),
    status: String(payload?.status || 'cancelled'),
  };
}
