import { isEnterprise } from '../../edition';
import { getRuntimeScope } from '../../services/runtimeScope';

export type ChatRoute =
  | { kind: 'chat-new' }
  | { kind: 'chat-session'; sessionId: string }
  | { kind: 'not-found'; pathname: string };

function basePath(): string {
  const raw = import.meta.env.BASE_URL || '/';
  // Vite 相对 base 的 BASE_URL 是 './'，等价于无前缀（对齐 base:'./' 的既有部署形态），
  // 否则 withBasePath 会产出 './chat/new' 这类相对路径，replaceState 逐次解析叠加出 /chat/chat/...。
  if (raw === './' || raw === '.') return '';
  const base = raw.replace(/\/+$/, '');
  return base === '/' ? '' : base;
}

function withoutBasePath(pathname: string): string {
  const base = basePath();
  if (!base || pathname === base) return pathname === base ? '/' : pathname;
  return pathname.startsWith(`${base}/`) ? pathname.slice(base.length) : pathname;
}

function withBasePath(pathname: string): string {
  const base = basePath();
  if (!base || pathname === base || pathname.startsWith(`${base}/`)) return pathname;
  return `${base}${pathname.startsWith('/') ? pathname : `/${pathname}`}`;
}

export function parseChatRoute(pathname: string): ChatRoute | null {
  const appPath = withoutBasePath(pathname);
  const path = appPath.length > 1 ? appPath.replace(/\/+$/, '') : appPath;
  if (path === '/' || path === '/chat' || path === '/chat/new') return { kind: 'chat-new' };
  const match = path.match(/^\/chat\/([^/]+)$/);
  if (!match) return null;
  const sessionId = decodeURIComponent(match[1]);
  return { kind: 'chat-session', sessionId };
}

function appendEnterpriseScope(path: string): string {
  if (!isEnterprise()) return path;
  const scope = getRuntimeScope();
  const query = new URLSearchParams(window.location.search);
  if (scope.userId) query.set('user_id', scope.userId);
  if (scope.groupId) query.set('group_id', scope.groupId);
  if (scope.botId) query.set('bot_id', scope.botId);
  const suffix = query.toString();
  return suffix ? `${path}?${suffix}` : path;
}

export function chatRoutePath(route: ChatRoute): string {
  if (route.kind === 'chat-new') return appendEnterpriseScope(withBasePath('/chat/new'));
  if (route.kind === 'chat-session') return appendEnterpriseScope(withBasePath(`/chat/${encodeURIComponent(route.sessionId)}`));
  return appendEnterpriseScope(route.pathname);
}
