/**
 * 用户空间配额特性开关：读 index.html 运行时注入的 ``window.__WORKSPACE_QUOTA_ENABLED__``
 * （部署 env ``WORKSPACE_QUOTA_ENABLED``，默认关闭）。
 */

function coerceRuntimeBool(raw: unknown): boolean {
  const text = String(raw ?? '').trim().toLowerCase();
  return text === 'true' || text === '1' || text === 'yes' || text === 'on';
}

export function isWorkspaceQuotaEnabled(): boolean {
  return coerceRuntimeBool(window.__WORKSPACE_QUOTA_ENABLED__);
}

/** 与页面注入同步；值在首屏 HTML 已固定，无需异步请求。 */
export function useWorkspaceQuotaEnabled(): boolean {
  return isWorkspaceQuotaEnabled();
}
