import { useEffect } from 'react';

import { CRON_JOB_SYNC_INTERVAL_MS } from '../features/cron/cronJobSync';
import { useCronStore } from '../stores/cronStore';

/**
 * Pull-based cron run sync for transports without server push (enterprise HTTP).
 * Polls `cron.job.list` on an interval while the document is visible.
 */
export function useCronJobSync(enabled: boolean): void {
  const syncJobRuns = useCronStore((s) => s.syncJobRuns);
  const loadJobs = useCronStore((s) => s.loadJobs);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    let intervalId: number | undefined;
    let cancelled = false;

    const tick = () => {
      if (document.visibilityState !== 'visible') {
        return;
      }
      void syncJobRuns();
    };

    const bootstrap = async () => {
      if (useCronStore.getState().jobs.length === 0) {
        await loadJobs();
      }
      if (cancelled) {
        return;
      }
      tick();
      intervalId = window.setInterval(tick, CRON_JOB_SYNC_INTERVAL_MS);
    };

    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        tick();
      }
    };

    void bootstrap();
    document.addEventListener('visibilitychange', onVisibilityChange);

    return () => {
      cancelled = true;
      if (intervalId !== undefined) {
        window.clearInterval(intervalId);
      }
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [enabled, syncJobRuns, loadJobs]);
}
