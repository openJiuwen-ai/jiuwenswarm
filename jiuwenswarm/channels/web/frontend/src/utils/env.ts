/**
 * 环境变量工具
 *
 * 用于在前端配置后端 API/WS 地址
 */
import { isEnterprise } from "../edition";

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
 * 北向传输：显式 ``WEB_TRANSPORT`` / ``VITE_WEB_TRANSPORT``（含 ``a2``→http）优先；
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

/** Gateway A2 前缀，默认同源 `/gateway-api/v1`，避免与 Manager `/api/v1` 冲突。 */
export function getGatewayHttpBase(): string {
  const raw = import.meta.env.VITE_GATEWAY_HTTP_BASE ?? import.meta.env.VITE_WEB_HTTP_BASE;
  if (!raw) {
    return "/gateway-api/v1";
  }
  const normalized = normalizeBase(raw);
  if (normalized.endsWith("/api/v1")) {
    return normalized;
  }
  return `${normalized}/api/v1`;
}
