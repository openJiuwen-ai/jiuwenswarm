export interface RuntimeScope {
  userId?: string;
  groupId?: string;
  botId?: string;
  /** 透传扩展字段，键为 REQUEST_EXT_FIELDS 中的 name；仅保存在内存。 */
  ext: Record<string, string>;
}

/**
 * HTTP 透传扩展字段注册表：前端唯一需要维护的透传字段清单。出厂为空：
 * 透传字段属于部署契约，由接入方按需启用（方式见下方示例）。
 *
 * - ``name`` 是后端 JIUWENSWARM_REQUEST_EXT_FORWARD_HEADERS 白名单中的原始字段名，
 *   同时作为入站 URL query 键与出站 HTTP header 名（网关按大小写不敏感匹配）。
 * - ``queryAliases``（可选）：整页跳转后恢复上下文时额外兼容的 URL query
 *   别名，仅当宿主 URL 参数名与 ``name`` 不一致时才需要；不写则只按
 *   ``name`` 解析。
 * - 新增透传字段：在此追加一项，并同步服务端白名单环境变量即可；
 *   解析、WS 握手 query、HTTP header、入口 URL 恢复全部自动生效。
 *
 * 示例——新增机构号字段 ``orgCode``（宿主在入口 URL 注入 ``?orgCode=001``）：
 * 1. 本数组追加一行 ``{ name: 'orgCode' }``；宿主 URL 用别名传递时写
 *    ``{ name: 'orgCode', queryAliases: ['orgNo'] }``。
 * 2. 服务端白名单追加：``JIUWENSWARM_REQUEST_EXT_FORWARD_HEADERS=...,orgCode``。
 * 之后 Agent 侧经 ``get_ext()`` 读取；值含中文等非 ASCII 字符时出站 header
 * 自动 ``encodeURIComponent`` 编码。
 *
 * 注意：字段名应避免下划线——默认配置的 nginx 会丢弃带下划线的请求头
 * （如需下划线名，须在代理链各层 nginx 开 ``underscores_in_headers on``，
 * 见 docker/web.nginx.conf.template）。
 */
export interface RequestExtField {
  name: string;
  queryAliases?: string[];
}

export const REQUEST_EXT_FIELDS: RequestExtField[] = [];

/** Authorization 有本地 token 兜底且不做百分号编码，在通用透传循环中单独处理。 */
const AUTH_FIELD = 'Authorization';

/** Dispatched when enterprise routing identity changes; WS should reconnect. */
export const RUNTIME_SCOPE_CHANGED_EVENT = 'jiuwenclaw:runtime-scope-changed';

function pickString(value: unknown): string | undefined {
  if (typeof value !== 'string') {
    return undefined;
  }
  const normalized = value.trim();
  return normalized || undefined;
}

function pickQueryValue(query: URLSearchParams, key: string): string | undefined {
  const value = query.get(key)?.trim();
  return value || undefined;
}

function pickQueryAlias(query: URLSearchParams, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const value = pickQueryValue(query, key);
    if (value) return value;
  }
  return undefined;
}

function isByteString(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    if (value.charCodeAt(index) > 0xff) return false;
  }
  return true;
}

function encodeHttpHeaderValue(value: string): string {
  return isByteString(value) ? value : encodeURIComponent(value);
}

function storedAccessToken(): string | undefined {
  try {
    const storage = typeof localStorage === 'undefined' ? null : localStorage;
    return pickString(storage?.getItem?.('openjiuwen_access_token'));
  } catch {
    return undefined;
  }
}

/** 按 REQUEST_EXT_FIELDS 过滤并归一化扩展字段（未知键丢弃，值 trim）。 */
function pickExt(ext: Record<string, string> | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  if (!ext) return out;
  for (const field of REQUEST_EXT_FIELDS) {
    const value = pickString(ext[field.name]);
    if (value) out[field.name] = value;
  }
  return out;
}

/** 导出注册表顺序的 [字段名, 值] 列表，供入口 URL 恢复等场景复用。 */
export function requestExtEntries(
  scope: RuntimeScope | undefined,
): Array<[string, string]> {
  const entries: Array<[string, string]> = [];
  if (!scope?.ext) return entries;
  for (const field of REQUEST_EXT_FIELDS) {
    const value = scope.ext[field.name];
    if (value) entries.push([field.name, value]);
  }
  return entries;
}

function scopeSignature(scope: RuntimeScope): string {
  const ext: Record<string, string> = {};
  for (const field of REQUEST_EXT_FIELDS) {
    ext[field.name] = scope.ext?.[field.name] || '';
  }
  return JSON.stringify({
    userId: scope.userId || '',
    groupId: scope.groupId || '',
    botId: scope.botId || '',
    ext,
  });
}

/**
 * Read the runtime routing and request extension fields supplied by the embedding shell.
 *
 * HTTP 透传字段与 JIUWENSWARM_REQUEST_EXT_FORWARD_HEADERS 对齐；仅保存在内存，
 * 不写 local/sessionStorage。URL 查询仅用于嵌入入口在整页跳转后恢复上下文。
 */
export function parseRuntimeScope(search: string): RuntimeScope {
  const query = new URLSearchParams(search);
  const ext: Record<string, string> = {};
  for (const field of REQUEST_EXT_FIELDS) {
    const value = pickQueryAlias(query, field.name, ...(field.queryAliases ?? []));
    if (value) ext[field.name] = value;
  }
  return {
    userId: pickQueryAlias(query, 'user_id', 'X-User-Id'),
    groupId: pickQueryAlias(query, 'group_id', 'X-Group-Id'),
    botId: pickQueryAlias(query, 'bot_id', 'X-Bot-Id'),
    ext,
  };
}

let runtimeScope: RuntimeScope =
  typeof window === 'undefined' ? { ext: {} } : parseRuntimeScope(window.location.search);

export function setRuntimeScope(scope: RuntimeScope): void {
  const next: RuntimeScope = {
    userId: pickString(scope.userId),
    groupId: pickString(scope.groupId),
    botId: pickString(scope.botId),
    ext: pickExt(scope.ext),
  };
  const changed = scopeSignature(runtimeScope) !== scopeSignature(next);
  runtimeScope = next;
  if (changed && typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(RUNTIME_SCOPE_CHANGED_EVENT));
  }
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
    ext: { ...runtimeScope.ext, ...current.ext },
  };
  return runtimeScope;
}

/** Add the current runtime scope to a WebSocket handshake query. */
export function appendRuntimeScopeQuery(
  query: URLSearchParams,
  scope: RuntimeScope = getRuntimeScope(),
): URLSearchParams {
  if (scope.userId) query.set('user_id', scope.userId);
  if (scope.groupId) query.set('group_id', scope.groupId);
  if (scope.botId) query.set('bot_id', scope.botId);
  for (const [name, value] of requestExtEntries(scope)) {
    query.set(name, value);
  }
  return query;
}

/** Build HTTP routing and request-extension headers without adding them to a business payload. */
export function buildRuntimeIdentityHeaders(
  requestId: string,
  params: Record<string, unknown>,
  scope: RuntimeScope = getRuntimeScope(),
): Record<string, string> {
  const headers: Record<string, string> = { 'X-Request-Id': requestId };
  const accessToken = storedAccessToken();
  const userId = scope.userId ?? pickString(params.user_id);
  const groupId = scope.groupId ?? pickString(params.group_id);
  const botId = scope.botId ?? pickString(params.bot_id);
  const authorization = scope.ext?.[AUTH_FIELD] ?? (accessToken ? `Bearer ${accessToken}` : undefined);
  const sessionId = pickString(params.session_id);

  // X-* headers preserve the existing Gateway routing contract for ASCII values.
  // The configured request_ext fields are always sent as ASCII-safe headers so
  // values such as user_name=张三 are not moved into an HTTP URL query.
  if (userId && isByteString(userId)) headers['X-User-Id'] = userId;
  if (groupId && isByteString(groupId)) headers['X-Group-Id'] = groupId;
  if (botId && isByteString(botId)) headers['X-Bot-Id'] = botId;
  if (userId) headers.user_id = encodeHttpHeaderValue(userId);
  if (groupId) headers.group_id = encodeHttpHeaderValue(groupId);
  if (botId) headers.bot_id = encodeHttpHeaderValue(botId);
  if (authorization && isByteString(authorization)) headers[AUTH_FIELD] = authorization;
  for (const field of REQUEST_EXT_FIELDS) {
    if (field.name === AUTH_FIELD) continue;
    const value = scope.ext?.[field.name] ?? pickString(params[field.name]);
    if (value) headers[field.name] = encodeHttpHeaderValue(value);
  }
  if (sessionId) headers['X-Session-Id'] = sessionId;
  return headers;
}
