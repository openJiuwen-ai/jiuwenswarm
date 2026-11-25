/**
 * 用户空间配额特性开关：读 index.html 运行时注入的 ``window.__WORKSPACE_QUOTA_ENABLED__``
 * （部署 env ``WORKSPACE_QUOTA_ENABLED``，企业版默认开启），且仅企业版生效。
 */

import { isEnterprise } from '../../edition';

function coerceRuntimeBool(raw: unknown, defaultValue: boolean): boolean {
  const text = String(raw ?? '').trim().toLowerCase();
  if (!text || text.startsWith('__')) return defaultValue;
  if (text === 'true' || text === '1' || text === 'yes' || text === 'on') return true;
  if (text === 'false' || text === '0' || text === 'no' || text === 'off') return false;
  return defaultValue;
}

export function isWorkspaceQuotaEnabled(): boolean {
  if (!isEnterprise()) return false;
  return coerceRuntimeBool(window.__WORKSPACE_QUOTA_ENABLED__, true);
}

/** 与页面注入同步；值在首屏 HTML 已固定，无需异步请求。 */
export function useWorkspaceQuotaEnabled(): boolean {
  return isWorkspaceQuotaEnabled();
}
