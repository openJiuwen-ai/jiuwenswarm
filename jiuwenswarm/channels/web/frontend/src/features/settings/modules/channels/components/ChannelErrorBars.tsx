import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../../../../components/ui';

type ChannelErrorBarsProps = {
  toastNotice: { seq: number; message: string } | null;
  configurationsError: string;
  channelsError: string | null;
  onRetry: () => void;
};

// Failure surfaces that live outside the config dialog: a transient 2s toast for operation
// failures (enable toggle, unbind) plus persistent banners with a shared retry for load
// failures. Errors of the currently open dialog are owned by the dialog status bar instead.
export function ChannelErrorBars({ toastNotice, configurationsError, channelsError, onRetry }: ChannelErrorBarsProps) {
  const { t } = useTranslation();
  const [toast, setToast] = useState<string | null>(null);
  const lastShownSeqRef = useRef(0);

  // The toast stays transient (2s, like the pre-fix behavior) and fires once per failure
  // occurrence: the effect reacts only to the failure sequence increasing, so a repeat failure
  // re-raises the toast with the latest message, while the latest failure moving to the open
  // dialog (excluded upstream) or being resolved clears it instead of replaying an older one.
  const toastSeq = toastNotice?.seq ?? 0;
  useEffect(() => {
    if (!toastNotice || toastNotice.seq <= lastShownSeqRef.current) {
      setToast(null);
      return;
    }
    lastShownSeqRef.current = toastNotice.seq;
    setToast(toastNotice.message);
    const timer = window.setTimeout(() => setToast(null), 2000);
    return () => window.clearTimeout(timer);
  }, [toastSeq]);

  return (
    <>
      {toast ? (
        <div className="settings-channels-panel__error-toast" data-testid="settings-channels-panel-error-toast">
          {toast}
        </div>
      ) : null}

      {configurationsError ? (
        <div
          className="settings-channels-panel__fetch-error settings-channels-panel__config-error"
          data-testid="settings-channels-panel-config-error"
        >
          <span>
            {t('channels.configLoadFailed')}: {configurationsError}
          </span>
          <Button variant="quiet" size="sm" onClick={onRetry} data-testid="settings-channels-panel-config-retry-btn">
            {t('channels.retry')}
          </Button>
        </div>
      ) : null}

      {channelsError ? (
        <div className="settings-channels-panel__fetch-error" data-testid="settings-channels-panel-fetch-error">
          <span>
            {t('channels.fetchFailed')}: {channelsError}
          </span>
          <Button variant="quiet" size="sm" onClick={onRetry} data-testid="settings-channels-panel-retry-btn">
            {t('channels.retry')}
          </Button>
        </div>
      ) : null}
    </>
  );
}
