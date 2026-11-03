/**
 * 环境变量工具
 *
 * 用于在前端配置后端 API/WS 地址
 */

import { isEnterprise } from '../edition';

function normalizeBase(input: string): string {
  return input.replace(/\/+$/, "");
}

export function getApiPrefix(): string {
  return normalizeBase(import.meta.env?.VITE_API_PREFIX || '');
}

export function resolveApiUrl(path: string): string {
  if (!path || /^(https?:|\/\/|data:|blob:)/i.test(path)) return path;
  const prefix = getApiPrefix();
  if (!prefix) return path;
  const cleanPath = path.startsWith('/') ? path : `/${path}`;
  return cleanPath === prefix || cleanPath.startsWith(`${prefix}/`) ? cleanPath : `${prefix}${cleanPath}`;
}

export function getApiBase(): string {
  const desktopApiBase = typeof window !== 'undefined' ? window.jiuwenDesktop?.apiBase : undefined;
  if (desktopApiBase) return normalizeBase(desktopApiBase);
  const raw = import.meta.env.VITE_API_BASE as string | undefined;
  if (!raw) return "";
  return normalizeBase(raw);
}

export function getWsBase(): string {
  const desktopWsBase = typeof window !== 'undefined' ? window.jiuwenDesktop?.wsBase : undefined;
  if (desktopWsBase) return normalizeBase(desktopWsBase);
  const raw = import.meta.env.VITE_WS_BASE as string | undefined;
  if (raw) return normalizeBase(raw);
  const apiBase = getApiBase();
  if (!apiBase) return "";
  return apiBase.replace(/^http:/, "ws:").replace(/^https:/, "wss:");
}

export type WebTransport = "websocket" | "http";

function parseWebTransportToken(raw: string): WebTransport | null {
  const value = raw.trim().toLowerCase();
  if (!value || value.startsWith("__")) {
    return null;
  }
  if (value === "http" || value === "a2") {
    return "http";
  }
  if (value === "websocket" || value === "ws") {
    return "websocket";
  }
  return null;
}

/**
 * 北向传输：显式 WEB_TRANSPORT / VITE_WEB_TRANSPORT（含 a2→http）优先。
 * 未指定时企业版默认 http，个人版默认 websocket。
 */
export function getWebTransport(): WebTransport {
  const injected = parseWebTransportToken(String(window.__JIUWEN_WEB_TRANSPORT__ ?? ""));
  if (injected) {
    return injected;
  }
  const fromEnv = parseWebTransportToken(
    String(import.meta.env.VITE_WEB_TRANSPORT ?? import.meta.env.VITE_TRANSPORT ?? ""),
  );
  if (fromEnv) {
    return fromEnv;
  }
  return isEnterprise() ? "http" : "websocket";
}

/**
 * 浏览器访问 Gateway `/api` 的根路径。
 * 个人版页面与 Gateway 同源，用 `/api`。
 * 企业版页面从管理端打开，用 `/gateway-api`，管理端再改写成 Gateway 的 `/api`。
 * 桌面端或显式绝对地址时，根是该地址下的 `/api`。
 */
export function getGatewayApiRoot(): string {
  const raw = import.meta.env.VITE_GATEWAY_HTTP_BASE ?? import.meta.env.VITE_WEB_HTTP_BASE;
  if (raw) {
    const normalized = normalizeBase(raw);
    if (normalized.endsWith("/api/v1") || normalized.endsWith("/v1")) {
      return normalized.slice(0, -"/v1".length);
    }
    return `${normalized}/api`;
  }
  const origin = getApiBase();
  if (/^https?:\/\//.test(origin)) {
    return `${origin}/api`;
  }
  const prefix = origin.startsWith("/") ? normalizeBase(origin) : "";
  return `${prefix}${isEnterprise() ? "/gateway-api" : "/api"}`;
}

/** Gateway `/api/v1` 在浏览器里的地址。 */
export function getGatewayHttpBase(): string {
  return `${getGatewayApiRoot()}/v1`;
}
