import { resolveUserId } from '../utils/userId';

export interface RuntimeScope {
  userId?: string;
  groupId?: string;
  botId?: string;
  /** Manager 计算的平台展示名（agent_{runtime_template_id}_{agent_template_id}） */
  agentName?: string;
  /** "{runtime_template_id}_{agent_template_id}" */
  agentId?: string;
  runtimeTemplateId?: string;
  /** Agent 配置模板 id：本次上传门禁只需要该字段 */
  agentTemplateId?: string;
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
 * 从嵌入页 URL 解析 runtime 路由身份（group/bot/user）、Agent 模板 id 与平台展示身份。
 * 个人版可仅有 user_id；企业版由宿主注入完整三元组。
 * 展示身份用 ``agent_identity_id`` 作为 query 键，避免与既有的 ``agent_id``
 * 路由参数（模式内 Agent id）冲突。
 */
export function parseRuntimeScope(search: string): RuntimeScope {
  const query = new URLSearchParams(search);
  return {
    userId: pickQueryAlias(query, 'user_id', 'X-User-Id'),
    groupId: pickQueryAlias(query, 'group_id', 'X-Group-Id'),
    botId: pickQueryAlias(query, 'bot_id', 'X-Bot-Id'),
    agentName: pickQueryAlias(query, 'agent_name', 'X-Agent-Name'),
    agentId: pickQueryAlias(query, 'agent_identity_id', 'X-Agent-Identity-Id'),
    runtimeTemplateId: pickQueryAlias(query, 'runtime_template_id', 'X-Runtime-Template-Id'),
    agentTemplateId: pickQueryAlias(query, 'agent_template_id', 'X-Agent-Template-Id'),
  };
}

let runtimeScope: RuntimeScope =
  typeof window === 'undefined' ? {} : parseRuntimeScope(window.location.search);

export function setRuntimeScope(scope: RuntimeScope): void {
  runtimeScope = {
    userId: scope.userId?.trim() || undefined,
    groupId: scope.groupId?.trim() || undefined,
    botId: scope.botId?.trim() || undefined,
    agentName: scope.agentName?.trim() || undefined,
    agentId: scope.agentId?.trim() || undefined,
    runtimeTemplateId: scope.runtimeTemplateId?.trim() || undefined,
    agentTemplateId: scope.agentTemplateId?.trim() || undefined,
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
    agentName: current.agentName ?? runtimeScope.agentName,
    agentId: current.agentId ?? runtimeScope.agentId,
    runtimeTemplateId: current.runtimeTemplateId ?? runtimeScope.runtimeTemplateId,
    agentTemplateId: current.agentTemplateId ?? runtimeScope.agentTemplateId,
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
  const agentName = scope.agentName;
  const agentId = scope.agentId;
  const runtimeTemplateId = scope.runtimeTemplateId;
  const agentTemplateId = scope.agentTemplateId;
  const sessionId = pickString(params.session_id);
  const token = typeof localStorage === 'undefined'
    ? ''
    : (localStorage.getItem('openjiuwen_access_token')?.trim() ?? '');
  if (userId && isByteString(userId)) headers['X-User-Id'] = userId;
  if (groupId && isByteString(groupId)) headers['X-Group-Id'] = groupId;
  if (botId && isByteString(botId)) headers['X-Bot-Id'] = botId;
  if (agentName && isByteString(agentName)) headers['X-Agent-Name'] = agentName;
  if (agentId && isByteString(agentId)) headers['X-Agent-Identity-Id'] = agentId;
  if (runtimeTemplateId && isByteString(runtimeTemplateId)) headers['X-Runtime-Template-Id'] = runtimeTemplateId;
  if (agentTemplateId && isByteString(agentTemplateId)) headers['X-Agent-Template-Id'] = agentTemplateId;
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

/** 把当前 runtime scope 写入 WebSocket 握手 query（最小三元组 + Agent 模板 id + 展示身份下发）。 */
export function appendRuntimeScopeQuery(
  query: URLSearchParams,
  scope: RuntimeScope = getRuntimeScope(),
): URLSearchParams {
  if (scope.userId) query.set('user_id', scope.userId);
  if (scope.groupId) query.set('group_id', scope.groupId);
  if (scope.botId) query.set('bot_id', scope.botId);
  // agent_id 已被路由层的「模式内 Agent id」占用，展示身份改用 agent_identity_id。
  if (scope.agentName) query.set('agent_name', scope.agentName);
  if (scope.agentId) query.set('agent_identity_id', scope.agentId);
  if (scope.runtimeTemplateId) query.set('runtime_template_id', scope.runtimeTemplateId);
  if (scope.agentTemplateId) query.set('agent_template_id', scope.agentTemplateId);
  return query;
}
