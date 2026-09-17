import { isEnterprise } from '../../edition';
import { getRuntimeScope } from '../../services/runtimeScope';

export type ChatRoute =
  | { kind: 'chat-new' }
  | { kind: 'chat-session'; sessionId: string }
  | { kind: 'not-found'; pathname: string };

export function parseChatRoute(pathname: string): ChatRoute | null {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname;
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
  if (route.kind === 'chat-new') return appendEnterpriseScope('/chat/new');
  if (route.kind === 'chat-session') return appendEnterpriseScope(`/chat/${encodeURIComponent(route.sessionId)}`);
  return appendEnterpriseScope(route.pathname);
}
