import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Tabs } from '../../../../components/ui/Tabs/Tabs';
import {
  SETTINGS_PAGES_METHOD,
  SETTINGS_PAGE_INSTANCE_KEY,
  SETTINGS_PAGE_SLOT_ID,
  parseSettingsPages,
  type ApplicationSettingsPage,
} from '../../../webui/applicationSettings';
import { WebUIHost } from '../../../webui/react/WebUIHost';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import './ExtensionsSettings.css';

/** One tab per Gateway package that declares application.settings.page; only the selected one is mounted. */
export function ExtensionSettingsPages(_props: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected, request } = useSettingsServices();
  const [pages, setPages] = useState<ApplicationSettingsPage[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [selected, setSelected] = useState('');

  useEffect(() => {
    if (!isConnected) return undefined;
    let cancelled = false;
    setFailed(false);
    request(SETTINGS_PAGES_METHOD, {}).then(
      (answer) => { if (!cancelled) setPages(parseSettingsPages(answer)); },
      () => { if (!cancelled) setFailed(true); },
    );
    return () => { cancelled = true; };
  }, [isConnected, request]);

  if (!isConnected || failed || pages === null) {
    const key = !isConnected ? 'settingsPanel.extensions.unavailable'
      : failed ? 'settingsPanel.extensions.pages.loadFailed' : 'settingsPanel.extensions.loading';
    return (
      <p className="settings-extensions__notice" data-testid="settings-extensions-pages-status"
        data-variant={!isConnected ? 'disconnected' : failed ? 'error' : 'loading'}>
        {t(key)}
      </p>
    );
  }
  if (pages.length === 0) {
    return <p className="settings-extensions__notice" data-testid="settings-extensions-pages-empty">{t('settingsPanel.extensions.pages.empty')}</p>;
  }
  const active = pages.find((page) => page.packageId === selected) ?? pages[0];
  return (
    <div className="settings-extensions__pages" data-testid="settings-extensions-pages">
      <Tabs role="tablist" ariaLabel={t('settingsPanel.extensions.pages.ariaLabel')} bordered
        wrapperTestId="settings-extensions-pages-tabs" itemTestId="settings-extensions-pages-tab"
        value={active.packageId} onChange={setSelected}
        items={pages.map((page) => ({ value: page.packageId, label: page.title }))} />
      <div className="settings-extensions__page" data-testid="settings-extensions-pages-host" data-variant={active.packageId}>
        <WebUIHost key={active.packageId} slotId={SETTINGS_PAGE_SLOT_ID} packageId={active.packageId}
          instanceKey={SETTINGS_PAGE_INSTANCE_KEY} />
      </div>
    </div>
  );
}
