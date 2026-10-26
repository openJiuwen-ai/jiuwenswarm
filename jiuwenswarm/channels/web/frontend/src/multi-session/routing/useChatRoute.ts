import { useCallback, useEffect, useState } from 'react';
import { chatRoutePath, parseChatRoute, type ChatRoute } from './route';

export function useChatRoute() {
  const [route, setRoute] = useState<ChatRoute>(() => parseChatRoute(window.location.pathname) ?? { kind: 'chat-new' });
  useEffect(() => {
    const onPopState = () => setRoute(parseChatRoute(window.location.pathname) ?? { kind: 'not-found', pathname: window.location.pathname });
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);
  const navigate = useCallback((next: ChatRoute, options?: { replace?: boolean }) => {
    const method = options?.replace ? 'replaceState' : 'pushState';
    window.history[method](null, '', chatRoutePath(next));
    setRoute(next);
  }, []);
  return { route, navigate };
}
