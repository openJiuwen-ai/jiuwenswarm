import { useEffect } from 'react';

import type { CampaignState } from '../../services/authClient';
import { useAuthStore } from '../../stores/authStore';
import type { CampaignError } from './campaignError';

export interface FreeModelsCampaign {
  state: CampaignState;
  /** Why the campaign state could not be loaded; null unless state is `unavailable`. */
  error: CampaignError | null;
}

export function useFreeModelsCampaign(): FreeModelsCampaign {
  const state = useAuthStore((store) => store.campaignState);
  const error = useAuthStore((store) => store.campaignError);
  const enabled = useAuthStore((store) => store.enabled);
  const initialized = useAuthStore((store) => store.initialized);
  const refresh = useAuthStore((store) => store.refresh);

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

  return { state, error };
}
