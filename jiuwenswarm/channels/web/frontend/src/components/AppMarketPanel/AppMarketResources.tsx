import { useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { getWsBase } from '../../utils/env';
import { marketResources } from './marketResources';

export function resourceHostLabel(desktop: boolean, endpoint: string): string {
  if (desktop) return 'appMarket.localMachineResources';
  try {
    const host = new URL(endpoint).hostname;
    if (host === 'localhost' || host === '[::1]' || /^127\.\d+\.\d+\.\d+$/.test(host)) {
      return 'appMarket.localServiceResources';
    }
  } catch { /* An unknown endpoint must not be described as the user's machine. */ }
  return 'appMarket.serverResources';
}

function usageColor(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return 'app-market-usage-unknown';
  if (value >= 85) return 'app-market-usage-high';
  return value >= 50 ? 'app-market-usage-medium' : 'app-market-usage-low';
}

export function AppMarketResources({ appId }: { appId?: string }) {
  const { t } = useTranslation();
  const snapshot = useSyncExternalStore(marketResources.subscribe, marketResources.getSnapshot);
  const desktop = !!window.jiuwenDesktop?.isElectron;
  const percent = (value?: number | null) => value == null || !Number.isFinite(value) ? '—' : `${value.toFixed(1)}%`;
  if (!appId) {
    const system = snapshot?.system;
    return (
      <span className="app-market-resources" data-testid="app-market-resources" data-variant="total"
        title={t(desktop ? 'appMarket.systemResourceHint' : 'appMarket.serverResourceHint')}>
        {t(resourceHostLabel(desktop, getWsBase() || window.location.href))} CPU{' '}
        <span className={usageColor(system?.cpuPercent)} data-testid="app-market-system-cpu-percent">{percent(system?.cpuPercent)}</span>
        {' · '}{t('appMarket.resourceMemory')}{' '}
        <span className={usageColor(system?.memoryPercent)} data-testid="app-market-system-memory-percent">{percent(system?.memoryPercent)}</span>
        {snapshot?.highUsage && <span className="app-market-resources-warning" role="status"
          data-testid="app-market-resources-warning">{t('appMarket.highResourceUsage')}</span>}
      </span>
    );
  }
  const reading = snapshot?.apps.find(item => item.appId === appId);
  const memory = reading?.memoryBytes;
  return (
    <span className="app-market-resources" data-testid="app-market-resources" data-variant={appId}
      title={t(desktop ? 'appMarket.pageResourceHint' : 'appMarket.serviceResourceHint')}>
      {t(desktop ? 'appMarket.pageResources' : 'appMarket.serviceResources')} CPU {percent(reading?.cpuPercent)}
      {' · '}{t('appMarket.resourceMemory')} {memory == null ? '—' : `${Math.round(memory / 1048576)} MiB`}
      {reading?.shared && <span data-testid="app-market-resources-shared"> · {t('appMarket.sharedProcess')}</span>}
      {!desktop && <span className="app-market-resources-note" data-testid="app-market-resources-note">{t('appMarket.webPageUnavailable')}</span>}
    </span>
  );
}
