export type ChatRoute =
  | { kind: 'chat-new' }
  | { kind: 'group-chat'; groupId?: string }
  | { kind: 'chat-session'; sessionId: string }
  | { kind: 'design-project'; projectId: string; sessionId?: string }
  | { kind: 'not-found'; pathname: string };

export function parseChatRoute(pathname: string): ChatRoute | null {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname;
  if (path === '/' || path === '/chat' || path === '/chat/new') return { kind: 'chat-new' };
  if (path === '/groups') return { kind: 'group-chat' };
  const groupMatch = path.match(/^\/groups\/([^/]+)$/);
  if (groupMatch) return { kind: 'group-chat', groupId: decodeURIComponent(groupMatch[1]) };
  const designSessionMatch = path.match(/^\/design\/([^/]+)\/([^/]+)$/);
  if (designSessionMatch) {
    return {
      kind: 'design-project',
      projectId: decodeURIComponent(designSessionMatch[1]),
      sessionId: decodeURIComponent(designSessionMatch[2]),
    };
  }
  const designMatch = path.match(/^\/design\/([^/]+)$/);
  if (designMatch) {
    return { kind: 'design-project', projectId: decodeURIComponent(designMatch[1]) };
  }
  const match = path.match(/^\/chat\/([^/]+)$/);
  if (!match) return null;
  const sessionId = decodeURIComponent(match[1]);
  return { kind: 'chat-session', sessionId };
}

export function chatRoutePath(route: ChatRoute): string {
  if (route.kind === 'chat-new') return '/chat/new';
  if (route.kind === 'group-chat') return route.groupId ? `/groups/${encodeURIComponent(route.groupId)}` : '/groups';
  if (route.kind === 'chat-session') return `/chat/${encodeURIComponent(route.sessionId)}`;
  if (route.kind === 'design-project') {
    const projectPath = `/design/${encodeURIComponent(route.projectId)}`;
    return route.sessionId
      ? `${projectPath}/${encodeURIComponent(route.sessionId)}`
      : projectPath;
  }
  return route.pathname;
}
