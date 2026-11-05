import { useCallback, useEffect, useState } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../../../components/ui/Button/Button';
import { Switch } from '../../../../components/ui/Switch/Switch';
import {
  SPLASH_SETTINGS_METHOD,
  SPLASH_SETTINGS_UPDATE_METHOD,
  displayedProviders,
  movedOrder,
  parseSplashSettings,
  toggledDisabled,
  type SplashSettings,
} from '../../../webui/applicationSettings';
import { APPEARANCE_PROVIDER_KEY, readItem } from '../../../webui/splash/launch';
import { SettingRow } from '../../components/SettingRow';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import './ExtensionsSettings.css';

type LoadState = 'loading' | 'ready' | 'error';

export function SplashProviderSettings({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected, request } = useSettingsServices();
  const [settings, setSettings] = useState<SplashSettings | null>(null);
  const [loadState, setLoadState] = useState<LoadState>('loading');
  const [saving, setSaving] = useState(false);
  const [saveFailed, setSaveFailed] = useState(false);

  useEffect(() => {
    if (!isConnected) return undefined;
    let cancelled = false;
    setLoadState('loading');
    request(SPLASH_SETTINGS_METHOD, {}).then(
      (answer) => {
        if (cancelled) return;
        const parsed = parseSplashSettings(answer);
        setSettings(parsed);
        setLoadState(parsed ? 'ready' : 'error');
      },
      () => { if (!cancelled) setLoadState('error'); },
    );
    return () => { cancelled = true; };
  }, [isConnected, request]);

  const update = useCallback(async (params: Record<string, unknown>) => {
    setSaving(true);
    setSaveFailed(false);
    try {
      const parsed = parseSplashSettings(await request(SPLASH_SETTINGS_UPDATE_METHOD, params));
      if (parsed) setSettings(parsed);
      else setSaveFailed(true);
    } catch {
      setSaveFailed(true);
    } finally {
      setSaving(false);
    }
  }, [request]);

  if (!isConnected || loadState !== 'ready' || !settings) {
    const key = !isConnected ? 'settingsPanel.extensions.unavailable'
      : loadState === 'error' ? 'settingsPanel.extensions.splash.loadFailed' : 'settingsPanel.extensions.loading';
    return (
      <SettingRow title={<span data-testid="settings-extensions-splash-status"
        data-variant={!isConnected ? 'disconnected' : loadState}>{t(key)}</span>} />
    );
  }
  if (settings.providers.length === 0) {
    return <SettingRow title={<span data-testid="settings-extensions-splash-empty">{t('settingsPanel.extensions.splash.empty')}</span>} />;
  }

  const rows = displayedProviders(settings, readItem(APPEARANCE_PROVIDER_KEY));
  const busy = disabled || saving;
  // Rows are direct children of the settings item so the grouped section draws one card with dividers.
  return (
    <>
      {rows.map((provider, index) => {
        const name = provider.title;
        const move = (delta: -1 | 1) => {
          const order = movedOrder(rows, provider.packageId, delta);
          if (order) void update({ order });
        };
        return (
          <SettingRow
            key={provider.packageId}
            title={<span data-testid="settings-extensions-splash-item" data-variant={provider.packageId}>{name}</span>}
            description={t(provider.skippable ? 'settingsPanel.extensions.splash.skippable'
              : 'settingsPanel.extensions.splash.notSkippable', { seconds: Math.round(provider.maxDurationMs / 1000) })}
          >
            <span className="settings-extensions__controls">
              <Button size="sm" variant="quiet" icon={<ChevronUp size={16} aria-hidden />}
                aria-label={t('settingsPanel.extensions.splash.moveUp', { name })}
                disabled={busy || index === 0} onClick={() => move(-1)}
                data-testid="settings-extensions-splash-up-btn" data-variant={provider.packageId} />
              <Button size="sm" variant="quiet" icon={<ChevronDown size={16} aria-hidden />}
                aria-label={t('settingsPanel.extensions.splash.moveDown', { name })}
                disabled={busy || index === rows.length - 1} onClick={() => move(1)}
                data-testid="settings-extensions-splash-down-btn" data-variant={provider.packageId} />
              <Switch checked={provider.enabled} disabled={busy}
                aria-label={t('settingsPanel.extensions.splash.toggle', { name })}
                onChange={(next) => void update({ disabled: toggledDisabled(settings.providers, provider.packageId, next) })}
                data-testid="settings-extensions-splash-switch" data-variant={provider.packageId} />
            </span>
          </SettingRow>
        );
      })}
      <SettingRow
        title={t('settingsPanel.extensions.splash.orderTitle')}
        description={
          <span data-testid="settings-extensions-splash-order" data-variant={settings.customized ? 'custom' : 'default'}>
            {t(settings.customized ? 'settingsPanel.extensions.splash.customOrder' : 'settingsPanel.extensions.splash.defaultOrder')}
          </span>
        }
      >
        {settings.customized ? (
          <Button size="sm" disabled={busy} onClick={() => void update({ order: null })}
            data-testid="settings-extensions-splash-reset-btn">
            {t('settingsPanel.extensions.splash.reset')}
          </Button>
        ) : null}
      </SettingRow>
      {saveFailed ? (
        <p className="settings-extensions__notice settings-extensions__notice--error settings-extensions__notice--card" role="alert"
          data-testid="settings-extensions-splash-save-error">
          {t('settingsPanel.extensions.splash.saveFailed')}
        </p>
      ) : null}
    </>
  );
}
