/**
 * 工作空间管理 API（Gateway ``/api/v1/workspace/*``）。
 * 列表/用量/删除/预览走 webRequest；下载为手写文件流。
 */

import { isEnterprise } from '../../edition';
import {
  getManagerAccessToken,
  hasManagerSessionCredentials,
  managerAuthenticatedFetch,
} from '../../auth/manager/authSession';
import { webRequest } from '../../services/webClient';
import { buildRuntimeIdentityHeaders } from '../../services/runtimeScope';
import { getGatewayHttpBase } from '../../utils/env';
import { unwrapHttpUnary } from '../../services/webHttpClient';
import type {
  WorkspaceDeleteData,
  WorkspaceExpandCreateResult,
  WorkspacePreviewData,
  WorkspaceTreeData,
  WorkspaceUsageData,
} from './workspaceTypes';

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function asEntryList(raw: unknown): WorkspaceTreeData {
  if (!isRecord(raw)) {
    return { relative_path: '', entries: [] };
  }
  const entriesRaw = Array.isArray(raw.entries) ? raw.entries : [];
  const entries: WorkspaceTreeData['entries'] = [];
  for (const item of entriesRaw) {
    if (!isRecord(item)) continue;
    if (typeof item.name !== 'string' || typeof item.relative_path !== 'string') continue;
    entries.push({
      name: item.name,
      relative_path: item.relative_path,
      is_dir: Boolean(item.is_dir),
      size_bytes: typeof item.size_bytes === 'number' ? item.size_bytes : null,
      mtime_ms: typeof item.mtime_ms === 'number' ? item.mtime_ms : 0,
      ctime_ms: typeof item.ctime_ms === 'number' ? item.ctime_ms : 0,
      zone: typeof item.zone === 'string' ? item.zone : 'other',
      deletable: Boolean(item.deletable),
      mime_hint: typeof item.mime_hint === 'string' ? item.mime_hint : null,
    });
  }
  return {
    relative_path: typeof raw.relative_path === 'string' ? raw.relative_path : '',
    entries,
  };
}

export async function fetchWorkspaceTree(relativePath = ''): Promise<WorkspaceTreeData> {
  const params: Record<string, unknown> = {};
  if (relativePath) {
    params.relative_path = relativePath;
  }
  const payload = await webRequest<unknown>('workspace.tree', params, { timeoutMs: 60000 });
  return asEntryList(payload);
}

export async function fetchWorkspaceUsage(): Promise<WorkspaceUsageData> {
  const payload = await webRequest<WorkspaceUsageData>('workspace.usage', {}, { timeoutMs: 60000 });
  return {
    used_bytes: Number(payload?.used_bytes ?? 0),
    limit_bytes: Number(payload?.limit_bytes ?? 0),
    percent: Number(payload?.percent ?? 0),
    status: (payload?.status as WorkspaceUsageData['status']) || 'ok',
    user_id: payload?.user_id,
    group_id: payload?.group_id,
    bot_id: payload?.bot_id,
    source_policy_id: payload?.source_policy_id,
  };
}

export async function deleteWorkspaceEntries(
  relativePaths: string[],
): Promise<WorkspaceDeleteData> {
  const payload = await webRequest<WorkspaceDeleteData>(
    'workspace.entries.delete',
    { relative_paths: relativePaths },
    { timeoutMs: 120000 },
  );
  return {
    results: Array.isArray(payload?.results) ? payload.results : [],
  };
}

export async function previewWorkspaceFile(
  relativePath: string,
  maxBytes?: number,
): Promise<WorkspacePreviewData> {
  const params: Record<string, unknown> = { relative_path: relativePath };
  if (maxBytes != null) {
    params.max_bytes = maxBytes;
  }
  const payload = await webRequest<WorkspacePreviewData>('workspace.preview', params, {
    timeoutMs: 60000,
  });
  return {
    relative_path: String(payload?.relative_path || relativePath),
    truncated: Boolean(payload?.truncated),
    content: payload?.content == null ? null : String(payload.content),
  };
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
        ? String((body as Record<string, unknown>).detail || (body as Record<string, unknown>).message || '')
        : '';
    throw new Error(detail || `HTTP ${response.status}`);
  }
  const envelope = body as ManagerEnvelope<T>;
  if (!envelope || envelope.code !== 200) {
    throw new Error(envelope?.message || 'manager request failed');
  }
  return envelope.data;
}

export async function createWorkspaceExpandRequest(input: {
  jiuwenclawId: string;
  groupId: string;
  botId: string;
  requestedLimitBytes: number;
  reason: string;
  usedBytes?: number;
  approverId: string;
}): Promise<WorkspaceExpandCreateResult> {
  const payload = await managerJson<WorkspaceExpandCreateResult>(
    '/manager-api/v1/user-console/workspace/expand-requests',
    {
      method: 'POST',
      body: JSON.stringify({
        jiuwenclaw_id: input.jiuwenclawId,
        group_id: input.groupId,
        bot_id: input.botId,
        requested_limit_bytes: input.requestedLimitBytes,
        reason: input.reason,
        used_bytes: input.usedBytes,
        approver_id: input.approverId,
      }),
    },
  );
  return {
    order_num: String(payload?.order_num || ''),
    status: String(payload?.status || 'pending'),
  };
}

function nextRequestId(): string {
  return `req_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function parseFilenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const star = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (star?.[1]) {
    try {
      return decodeURIComponent(star[1].trim());
    } catch {
      return star[1].trim();
    }
  }
  const plain = /filename="([^"]+)"/i.exec(header) || /filename=([^;]+)/i.exec(header);
  return plain?.[1]?.trim() || null;
}

async function authenticatedFetch(input: RequestInfo | URL, init: RequestInit): Promise<Response> {
  if (isEnterprise() && hasManagerSessionCredentials()) {
    const headers = new Headers(init.headers);
    const authorization = headers.get('Authorization');
    const managerAccessToken = getManagerAccessToken();
    if (
      authorization &&
      authorization !== (managerAccessToken ? `Bearer ${managerAccessToken}` : null)
    ) {
      return fetch(input, init);
    }
    return managerAuthenticatedFetch(input, init);
  }
  return fetch(input, init);
}

/** 触发浏览器下载；失败时抛出带 message 的 Error。 */
export async function downloadWorkspaceEntry(relativePath: string): Promise<void> {
  const requestId = nextRequestId();
  const base = getGatewayHttpBase().replace(/\/+$/, '');
  const url = `${base}/workspace/download?relative_path=${encodeURIComponent(relativePath)}`;
  const response = await authenticatedFetch(url, {
    method: 'GET',
    headers: {
      ...buildRuntimeIdentityHeaders(requestId, {}),
      Accept: '*/*',
    },
  });

  const contentType = (response.headers.get('content-type') || '').toLowerCase();
  if (!response.ok || contentType.includes('application/json')) {
    const text = await response.text();
    let message = `download failed (${response.status})`;
    try {
      const unwrapped = unwrapHttpUnary(JSON.parse(text) as unknown, response.status);
      if (!unwrapped.ok) {
        message = unwrapped.message;
      }
    } catch {
      if (text.trim()) message = text.slice(0, 200);
    }
    throw new Error(message);
  }

  const blob = await response.blob();
  const filename =
    parseFilenameFromDisposition(response.headers.get('content-disposition')) ||
    relativePath.split('/').pop() ||
    'download.bin';
  const objectUrl = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement('a');
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.rel = 'noopener';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

export function formatBytes(bytes: number | null | undefined): string {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return '—';
  if (n < 1024) return `${Math.round(n)} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = n / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = value >= 100 ? 0 : value >= 10 ? 1 : 2;
  return `${value.toFixed(digits)} ${units[unit]}`;
}

export function formatPercent(percent: number): string {
  if (!Number.isFinite(percent)) return '0%';
  return `${Math.min(999, Math.max(0, percent)).toFixed(percent >= 10 ? 0 : 1)}%`;
}
