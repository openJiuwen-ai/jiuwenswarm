export type ChatRoute =
  | { kind: 'chat-new' }
  | { kind: 'group-chat'; groupId?: string }
  | { kind: 'chat-session'; sessionId: string }
  | { kind: 'design-project'; projectId: string; sessionId?: string }
  | { kind: 'system-plugin-recovery' }
  | { kind: 'not-found'; pathname: string };

function basePath(): string {
  const base = (import.meta.env.BASE_URL || '/').replace(/\/+$/, '');
  return base === '.' || base === '/' ? '' : base;
}

function withBasePath(pathname: string): string {
  return `${basePath()}${pathname}`;
}

export function parseChatRoute(pathname: string): ChatRoute | null {
  const base = basePath();
  if (base && pathname !== base && !pathname.startsWith(`${base}/`)) return null;
  const appPath = base ? pathname.slice(base.length) || '/' : pathname;
  const path = appPath.length > 1 ? appPath.replace(/\/+$/, '') : appPath;
  if (path === '/' || path === '/chat' || path === '/chat/new') return { kind: 'chat-new' };
<<<<<<< HEAD
  if (path === '/system-plugin-recovery') return { kind: 'system-plugin-recovery' };
||||||| e487e9a30
=======
  if (path === '/groups') return { kind: 'group-chat' };
  const groupMatch = path.match(/^\/groups\/([^/]+)$/);
  if (groupMatch) return { kind: 'group-chat', groupId: decodeURIComponent(groupMatch[1]) };
>>>>>>> origin/develop
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
<<<<<<< HEAD
  if (route.kind === 'chat-new') return '/chat/new';
  if (route.kind === 'system-plugin-recovery') return '/system-plugin-recovery';
  if (route.kind === 'chat-session') return `/chat/${encodeURIComponent(route.sessionId)}`;
||||||| e487e9a30
  if (route.kind === 'chat-new') return '/chat/new';
  if (route.kind === 'chat-session') return `/chat/${encodeURIComponent(route.sessionId)}`;
=======
  if (route.kind === 'chat-new') return withBasePath('/chat/new');
  if (route.kind === 'chat-session') return withBasePath(`/chat/${encodeURIComponent(route.sessionId)}`);
  if (route.kind === 'group-chat') return withBasePath(route.groupId ? `/groups/${encodeURIComponent(route.groupId)}` : '/groups');
>>>>>>> origin/develop
  if (route.kind === 'design-project') {
    const projectPath = `/design/${encodeURIComponent(route.projectId)}`;
    return route.sessionId
      ? withBasePath(`${projectPath}/${encodeURIComponent(route.sessionId)}`)
      : withBasePath(projectPath);
  }
  return route.pathname;
}
