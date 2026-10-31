import { resolveUserId } from '../utils/userId';

export interface RuntimeScope {
  userId?: string;
  groupId?: string;
  botId?: string;
}

function pickString(value: unknown): string | undefined {
  if (typeof value !== 'string') {
    return undefined;
  }
  const normalized = value.trim();
  return normalized || undefined;
}

function pickQueryAlias(query: URLSearchParams, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const value = query.get(key)?.trim();
    if (value) return value;
  }
  return undefined;
}

/**
 * 从嵌入页 URL 解析 runtime 路由身份（group/bot/user）。
 * 个人版可仅有 user_id；企业版由宿主注入完整三元组。
 */
export function parseRuntimeScope(search: string): RuntimeScope {
  const query = new URLSearchParams(search);
  return {
    userId: pickQueryAlias(query, 'user_id', 'X-User-Id'),
    groupId: pickQueryAlias(query, 'group_id', 'X-Group-Id'),
    botId: pickQueryAlias(query, 'bot_id', 'X-Bot-Id'),
  };
}

let runtimeScope: RuntimeScope =
  typeof window === 'undefined' ? {} : parseRuntimeScope(window.location.search);

export function setRuntimeScope(scope: RuntimeScope): void {
  runtimeScope = {
    userId: scope.userId?.trim() || undefined,
    groupId: scope.groupId?.trim() || undefined,
    botId: scope.botId?.trim() || undefined,
  };
}

export function getRuntimeScope(): RuntimeScope {
  if (typeof window === 'undefined') {
    return runtimeScope;
  }
  const current = parseRuntimeScope(window.location.search);
  runtimeScope = {
    userId: current.userId ?? runtimeScope.userId,
    groupId: current.groupId ?? runtimeScope.groupId,
    botId: current.botId ?? runtimeScope.botId,
  };
  // 个人版兼容：URL/内存均无 user_id 时回落 localStorage
  if (!runtimeScope.userId) {
    const fallback = pickString(resolveUserId());
    if (fallback) runtimeScope = { ...runtimeScope, userId: fallback };
  }
  return runtimeScope;
}

function isByteString(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    if (value.charCodeAt(index) > 255) return false;
  }
  return true;
}

/** HTTP 请求头里的路由身份。业务参数仍留在 body / query。 */
export function buildRuntimeIdentityHeaders(
  requestId: string,
  params: Record<string, unknown>,
  scope: RuntimeScope = getRuntimeScope(),
): Record<string, string> {
  const headers: Record<string, string> = { 'X-Request-Id': requestId };
  const userId = scope.userId ?? pickString(params.user_id);
  const groupId = scope.groupId ?? pickString(params.group_id);
  const botId = scope.botId ?? pickString(params.bot_id);
  const sessionId = pickString(params.session_id);
  const token = typeof localStorage === 'undefined'
    ? ''
    : (localStorage.getItem('openjiuwen_access_token')?.trim() ?? '');
  if (userId && isByteString(userId)) headers['X-User-Id'] = userId;
  if (groupId && isByteString(groupId)) headers['X-Group-Id'] = groupId;
  if (botId && isByteString(botId)) headers['X-Bot-Id'] = botId;
  if (token && isByteString(token)) headers.Authorization = `Bearer ${token}`;
  if (typeof localStorage !== 'undefined' && userId) {
    try {
      const federation = JSON.parse(localStorage.getItem('openjiuwen_federation') || 'null');
      if (
        federation?.user_id === userId &&
        federation.token_type === 'Bearer' &&
        typeof federation.access_token === 'string' &&
        federation.access_token &&
        isByteString(federation.access_token)
      ) {
        headers['X-User-Token'] = `Bearer ${federation.access_token}`;
      }
    } catch {
      // Ignore an incomplete or outdated browser session record.
    }
  }
  if (sessionId && isByteString(sessionId)) headers['X-Session-Id'] = sessionId;
  return headers;
}

/** 把当前 runtime scope 写入 WebSocket 握手 query（最小三元组下发）。 */
export function appendRuntimeScopeQuery(
  query: URLSearchParams,
  scope: RuntimeScope = getRuntimeScope(),
): URLSearchParams {
  if (scope.userId) query.set('user_id', scope.userId);
  if (scope.groupId) query.set('group_id', scope.groupId);
  if (scope.botId) query.set('bot_id', scope.botId);
  return query;
}
