/**
 * 华为账号（Account Kit）登录客户端：只做 HTTP 调用，状态在 `stores/authStore.ts`。
 *
 * 流程：`authorize()` 拿地址 → 打开新标签页授权 → 回调
 * → 回到应用后 `claim()` 取回会话。后端接口见 `gateway/channel_manager/web/web_http_auth.py`。
 */

import { getApiBase } from '../utils/env';

/**
 * 没有 cookie 的环境回落到请求头带会话 id。
 *
 * 存 `localStorage` 而不是 `sessionStorage`：桌面 WebView 拿不到种在系统浏览器里的 cookie，
 * 只剩这一条路，而 `sessionStorage` 关掉窗口就清空——重开应用会显示未登录，其实网关那边会话还在
 */
const SESSION_HEADER = 'X-Auth-Session';
const SESSION_STORAGE_KEY = 'jiuwenswarm.auth.sessionId';

/**
 * 发起登录 / 认领 / 登出必须带的请求头，与 web_http_auth.AUTH_REQUEST_HEADER 一致：
 * 带自定义头的跨站请求要先过 CORS 预检，Gateway 不放行，别的网页就没法盲发这几个请求。
 */
const AUTH_REQUEST_HEADER = 'X-Jiuwen-Auth';

/** 回调落地页广播用的频道名和消息类型，与 web_http_auth.AUTH_CALLBACK_CHANNEL / AUTH_CALLBACK_MESSAGE 一致。 */
export const AUTH_CALLBACK_CHANNEL = 'jiuwenswarm:auth';
export const AUTH_CALLBACK_MESSAGE = 'jiuwenswarm:auth-callback';

export interface AuthorizeResponse {
  authorizeUrl: string;
  state: string;
  /** 只下发给发起方的认领凭证，不出现在授权 URL 里。 */
  claimToken: string;
  expiresIn?: number;
}

/**
 * 活动状态，决定界面显示什么对应：`campaign_state`：
 * `active` 正常；`ended` 活动已结束；`unavailable` 拉不到配置；
 * `off` 本地关掉了，什么都不显示。老版本 Gateway 不返回这个字段。
 */
export type CampaignState = 'active' | 'ended' | 'unavailable' | 'off';

export interface AuthStatus {
  islogin: boolean;
  enabled: boolean;
  state?: CampaignState;
  accountCenterUrl?: string;
  provider?: string;
  userId?: string | null;
  userName?: string | null;
  sessionId?: string;
  expired?: boolean;
}

export interface AuthErrorBody {
  code: string;
  message: string;
}

export class AuthApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(message: string, code: string, status: number) {
    super(message);
    this.name = 'AuthApiError';
    this.code = code;
    this.status = status;
  }
}

function readStoredSessionId(): string {
  try {
    return localStorage.getItem(SESSION_STORAGE_KEY) || '';
  } catch {
    return '';
  }
}

function writeStoredSessionId(sessionId: string): void {
  try {
    if (sessionId) localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
    else localStorage.removeItem(SESSION_STORAGE_KEY);
  } catch {
    /* 隐私模式下 localStorage 可能抛错，忽略即可——cookie 仍然生效 */
  }
}

/** 会改登录状态的请求（发起 / 认领 / 登出）用的请求头。 */
function stateChangingHeaders(extra?: Record<string, string>): Record<string, string> {
  return authHeaders({ ...(extra || {}), [AUTH_REQUEST_HEADER]: '1' });
}

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...(extra || {}) };
  const sessionId = readStoredSessionId();
  if (sessionId) headers[SESSION_HEADER] = sessionId;
  return headers;
}

function authUrl(path: string): string {
  // 登录路由挂在 WebChannel 自己的 FastAPI app 上（见 web_channel_app.
  // register_http_routes），和 WS 同源同端口。getApiBase() 默认空串 = 同源，
  // 与 trajectoryClient 用的是同一套约定。
  return `${getApiBase()}/api/v1/auth${path}`;
}

async function readError(response: Response, fallback: string): Promise<AuthApiError> {
  let code = `http_${response.status}`;
  let message = fallback;
  try {
    const body = (await response.json()) as { error?: AuthErrorBody; message?: string };
    if (body?.error?.message) {
      message = body.error.message;
      code = body.error.code || code;
    } else if (body?.message) {
      message = body.message;
    }
  } catch {
    /* 非 JSON 响应，用兜底文案 */
  }
  return new AuthApiError(message, code, response.status);
}

/** 发起登录：拿到浏览器要打开的授权地址。 */
export async function authorize(): Promise<AuthorizeResponse> {
  const response = await fetch(authUrl('/authorize'), {
    method: 'POST',
    credentials: 'include',
    headers: stateChangingHeaders(),
  });
  if (!response.ok) throw await readError(response, '获取授权地址失败');
  const data = (await response.json()) as AuthorizeResponse;
  if (!data?.authorizeUrl || !data?.state || !data?.claimToken) {
    throw new AuthApiError('授权地址返回不完整', 'authorize_malformed', 500);
  }
  return data;
}

export type ClaimOutcome = { kind: 'pending' } | { kind: 'done'; status: AuthStatus };

/**
 * 取回登录结果。202 = 还没完成授权（不是错误，等下一次触发再来）。
 * 成功后存下 session id：桌面 WebView 拿不到种在系统浏览器里的 cookie，只能靠请求头带会话。
 */
export async function claim(state: string, claimToken: string): Promise<ClaimOutcome> {
  const response = await fetch(authUrl('/claim'), {
    method: 'POST',
    credentials: 'include',
    headers: stateChangingHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ state, claimToken }),
  });
  if (response.status === 202) return { kind: 'pending' };
  if (!response.ok) throw await readError(response, '登录失败，请重试');
  const headerSession = response.headers.get(SESSION_HEADER);
  const data = (await response.json()) as AuthStatus;
  writeStoredSessionId(headerSession || data.sessionId || '');
  // claim 的响应里没有 enabled 字段——能认领成功，登录当然是开着的
  return { kind: 'done', status: { ...data, enabled: true } };
}

/**
 * 放弃这次登录，让Gateway别再替它向鉴权服务认领。发不出去时Gateway也会在state过期后自己停。
 * `keepalive` 让页面关闭时发起的这次请求也能送达。
 */
export async function cancelLogin(state: string, claimToken: string): Promise<void> {
  try {
    await fetch(authUrl('/cancel'), {
      method: 'POST',
      credentials: 'include',
      keepalive: true,
      headers: stateChangingHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ state, claimToken }),
    });
  } catch {
    /* 见上 */
  }
}

export function openExternal(url: string): Window | null {
  const opened = window.open(url, '_blank');
  if (opened) {
    try {
      opened.opener = null;
    } catch {
      /* 个别 WebView 不允许改写 */
    }
  }
  return opened;
}

/** 查询当前登录状态。后端未开启登录时返回 `enabled: false`。 */
export async function status(): Promise<AuthStatus> {
  const response = await fetch(authUrl('/status'), {
    method: 'GET',
    credentials: 'include',
    headers: authHeaders({ Accept: 'application/json' }),
  });
  if (!response.ok) throw await readError(response, '获取登录状态失败');
  const data = (await response.json()) as AuthStatus;
  // 会话确实过期了才丢掉本地句柄：它的优先级高于 cookie，留着会把还能用的 cookie 挡住。
  // 未登录状态不算，活动下线时也是未登录，那时清掉，活动恢复后还需要重登一次
  if (data.expired) writeStoredSessionId('');
  return data;
}

export interface LoginModelInfo {
  model_name: string;
  display_name: string;
  description?: string;
}

/** 登录后自动获得的模型。它们同时也并进了普通模型列表，这里是给账号面板看的。 */
export async function listLoginModels(): Promise<LoginModelInfo[]> {
  const response = await fetch(authUrl('/models'), {
    method: 'GET',
    credentials: 'include',
    headers: authHeaders({ Accept: 'application/json' }),
  });
  if (!response.ok) throw await readError(response, '获取模型列表失败');
  const data = (await response.json()) as { models?: LoginModelInfo[] };
  return data.models ?? [];
}

/**
 * 免费积分：LiteLLM key 上的 `max_budget` / `spend`，1:1，不带币种。
 * `-1` 表示未知，**不要当成 0 展示**（0 会被读成"用完了"）。
 */
export interface ModelQuota {
  total: number;
  balance: number;
  used: number;
  /** 后端判定的"所剩不多"（已用达到总量 90%），前端不自己按阈值算。 */
  low_balance: boolean;
  exhausted: boolean;
  /** 额度刷新周期（LiteLLM `budget_duration` 原文，如 `7d`）；空串 = 用完不会自动恢复。 */
  reset_period: string;
  /** 下次刷新时间（ISO 8601）；没有周期时为空串。 */
  reset_at: string;
}

export interface QuotaResult {
  /** 这套部署有没有接 APIG。false 时整块额度 UI 不展示，而不是显示报错。 */
  available: boolean;
  quota: ModelQuota | null;
}

/** 查免费积分。积分用完了也是正常返回（`exhausted=true`），不是错误。 */
export async function fetchQuota(): Promise<QuotaResult> {
  const response = await fetch(authUrl('/quota'), {
    method: 'GET',
    credentials: 'include',
    headers: authHeaders({ Accept: 'application/json' }),
  });
  if (!response.ok) throw await readError(response, '获取额度失败');
  const data = (await response.json()) as QuotaResult;
  return { available: Boolean(data?.available), quota: data?.quota ?? null };
}

export async function logout(): Promise<void> {
  await fetch(authUrl('/logout'), {
    method: 'POST',
    credentials: 'include',
    headers: stateChangingHeaders(),
  }).catch(() => undefined);
  writeStoredSessionId('');
}

/**
 * 打开新标签页去授权；被拦截时返回 `null`，由调用方展示地址让用户手动打开。
 *
 * **打开后立刻切断 opener**，不让授权窗口拿到能反向导航本页的引用。不直接传 `noopener`：
 * 那样 `window.open` 恒返回 `null`，就判断不了弹窗是否被拦截。
 */
export function openAuthorizeUrl(url: string): Window | null {
  const opened = window.open(url, '_blank');
  if (opened) {
    try {
      opened.opener = null;
    } catch {
      /* 个别 WebView 不允许改写；授权照常进行 */
    }
  }
  return opened;
}
