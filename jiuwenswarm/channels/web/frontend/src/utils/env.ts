/**
 * 环境变量工具
 *
 * 用于在前端配置后端 API/WS 地址
 */
import { isEnterprise } from "../edition";

function normalizeBase(input: string): string {
  return input.replace(/\/+$/, "");
}

/**
 	  * 获取API前缀的函数
 	  * 从环境变量中读取VITE_API_PREFIX配置，并进行规范化处理后返回
 	  * @returns {string} 返回处理后的API前缀字符串，如果环境变量未定义则返回空字符串
 	  */
 	 export function getApiPrefix(): string {
 	   const raw = import.meta.env?.VITE_API_PREFIX as string | undefined; // 从环境变量中获取原始API前缀值，可能为undefined；用可选链防御测试等非Vite环境下import.meta.env未定义
 	   if (!raw) return ""; // 如果原始值为空，则直接返回空字符串
 	   return normalizeBase(raw); // 调用normalizeBase函数对原始值进行规范化处理并返回
 	 }
 	 
 	 export function resolveApiUrl(path: string): string {
 	   if (!path || /^(https?:|\/\/|data:|blob:)/i.test(path)) {
 	     return path;
 	   }
 	   const prefix = getApiPrefix();
 	   if (!prefix) {
 	     return path;
 	   }
 	   const cleanPath = path.startsWith("/") ? path : `/${path}`;
 	   if (cleanPath === prefix || cleanPath.startsWith(`${prefix}/`)) {
 	     return cleanPath;
 	   }
 	   return `${prefix}${cleanPath}`;
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
  if (apiBase) {
    return apiBase.replace(/^http:/, "ws:").replace(/^https:/, "wss:");
  }
  // 未显式配置 VITE_WS_BASE 但配置了接口前缀时，基于当前页面 origin + 前缀推导 WS 地址。
  const prefix = getApiPrefix();
  if (prefix && typeof window !== "undefined") {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${protocol}//${window.location.host}${prefix}/ws`;
  }
  return "";
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
    // 默认走同源 gateway-api，配置接口前缀时需带上前缀以命中门户代理。
    const prefix = getApiPrefix();
    return prefix ? `${prefix}/gateway-api/v1` : "/gateway-api/v1";
  }
  const normalized = normalizeBase(raw);
  if (normalized.endsWith("/api/v1")) {
    return normalized;
  }
  return `${normalized}/api/v1`;
}
