import { useCallback, useEffect } from 'react';
import { useTranslation } from 'react-i18next';

// Minimal shape of a channel form controller needed for cross-channel status aggregation.
type ChannelFormStatus = {
  loading: boolean;
  loaded: boolean;
  error: string | null;
  failureSeq: number;
  load: () => Promise<boolean>;
};

// Aggregates cross-channel status for the channels settings page and owns the reload entry
// points: `configurationsLoading` gates the list skeleton, `configurationsError` feeds the
// persistent configuration-load banner, `toastNotice` is the source of the page-level
// transient toast, and `reloadChannels` re-runs the channel list plus all configuration loads.
export function useChannelControllersStatus(
  controllers: Record<string, ChannelFormStatus>,
  loadChannels: () => Promise<void>,
  dialogOpen: boolean,
  activeChannelId: string,
) {
  const { t } = useTranslation();

  // The controllers record is rebuilt on every render, so it must not appear in dependency
  // lists. The individual load callbacks are stable and their count is fixed, which keeps
  // loadConfigurations (and therefore the mount load below) referentially stable: React
  // compares hook dependencies element-wise (Object.is per element) and never by array
  // reference, so the freshly built array itself does not invalidate the memoization.
  const configurationLoads = Object.values(controllers).map((controller) => controller.load);
  const loadConfigurations = useCallback(async () => {
    await Promise.all(configurationLoads.map((load) => load()));
  }, configurationLoads);

  const reloadChannels = useCallback(async () => {
    await Promise.all([loadChannels(), loadConfigurations()]);
  }, [loadChannels, loadConfigurations]);

  useEffect(() => {
    void loadConfigurations();
  }, [loadConfigurations]);

  // Page-level toast only covers failures that have no better surface: the configuration load
  // failures are shown by the persistent configurationsError banner, and a save failure for the
  // open dialog is shown by the dialog status bar. Everything else (enable toggle, unbind) stays
  // on the toast, which the panel keeps transient.
  // The toast is occurrence-driven, not aggregate-string-driven: every failure records a globally
  // increasing sequence, the toast shows the message of the latest failure alone (older persistent
  // errors must not accumulate into a duplicated join), and it re-raises on a repeat failure with
  // the same message. ChannelErrorBars reacts only to a sequence increase, so the latest failure
  // moving to the open dialog (excluded below) clears the toast instead of replaying an older one.
  const toastNotice = Object.entries(controllers)
    .filter(([channelId, controller]) =>
      controller.loaded && controller.error !== null && !(dialogOpen && channelId === activeChannelId),
    )
    .reduce<{ seq: number; message: string } | null>((latest, [, controller]) => {
      const message = controller.error;
      if (message === null) return latest;
      return !latest || controller.failureSeq > latest.seq ? { seq: controller.failureSeq, message } : latest;
    }, null);

  const configurationsError = Array.from(
    new Set(
      Object.values(controllers)
        .map((controller) => (!controller.loaded ? controller.error : null))
        .filter((message): message is string => Boolean(message)),
    ),
  ).join(t('common.and'));

  const configurationsLoading = Object.values(controllers).some(
    (controller) => (!controller.loaded && controller.error === null) || controller.loading,
  );

  return { configurationsLoading, configurationsError, toastNotice, reloadChannels };
}
