import { useEffect } from 'react';

import { useAuthStore } from '../../stores/authStore';

export function useFreeModelsCampaign(): boolean {
  const enabled = useAuthStore((state) => state.enabled);
  const initialized = useAuthStore((state) => state.initialized);
  const refresh = useAuthStore((state) => state.refresh);

  useEffect(() => {
    if (!initialized) void refresh();
  }, [initialized, refresh]);

  useEffect(() => {
    if (enabled) return undefined;
    const recheck = (): void => {
      if (document.visibilityState === 'visible') void refresh();
    };
    document.addEventListener('visibilitychange', recheck);
    window.addEventListener('focus', recheck);
    return () => {
      document.removeEventListener('visibilitychange', recheck);
      window.removeEventListener('focus', recheck);
    };
  }, [enabled, refresh]);

  return enabled;
}
