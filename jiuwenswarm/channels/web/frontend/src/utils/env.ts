/**
 * 环境变量工具
 *
 * 用于在前端配置后端 API/WS 地址
 */
function normalizeBase(input: string): string {
  return input.replace(/\/+$/, "");
}

export function getApiBase(): string {
  const raw = import.meta.env.VITE_API_BASE as string | undefined;
  if (!raw) return "";
  return normalizeBase(raw);
}

export function getWsBase(): string {
  const raw = import.meta.env.VITE_WS_BASE as string | undefined;
  if (raw) return normalizeBase(raw);
  const apiBase = getApiBase();
  if (!apiBase) return "";
  return apiBase.replace(/^http:/, "ws:").replace(/^https:/, "wss:");
}
