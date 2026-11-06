import { useEffect, useState } from 'react';
import {
  SETTINGS_PAGES_METHOD,
  SETTINGS_PAGE_INSTANCE_KEY,
  SETTINGS_PAGE_SLOT_ID,
  parseSettingsPages,
  type ApplicationSettingsPage,
} from '../../../webui/applicationSettings';
import { WebUIHost } from '../../../webui/react/WebUIHost';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import './ExtensionsSettings.css';

/**
 * Settings entries of Gateway packages that declare application.settings.page (Gateway F_13 D-08).
 *
 * Each package gets its own navigation entry right after the host's Extensions module, which keeps only
 * host features. The entries share that module's access result: no Extensions module, no plugin entries.
 */
export const PLUGIN_PAGES_ANCHOR = 'extensions';
const ENTRY_PREFIX = 'extension-page:';

export function pluginEntryId(packageId: string): string {
  return `${ENTRY_PREFIX}${packageId}`;
}

export interface SettingsNavEntry<M> {
  id: string;
  module?: M;
  page?: ApplicationSettingsPage;
}

/** Insert one entry per plugin page right after the Extensions module, in the order the Gateway lists them. */
export function withPluginEntries<M extends { module: { id: string } }>(
  modules: readonly M[],
  pages: readonly ApplicationSettingsPage[],
): SettingsNavEntry<M>[] {
  const entries: SettingsNavEntry<M>[] = modules.map((module) => ({ id: module.module.id, module }));
  const anchor = modules.findIndex((module) => module.module.id === PLUGIN_PAGES_ANCHOR);
  if (anchor < 0) return entries;
  const plugins = pages.map((page) => ({ id: pluginEntryId(page.packageId), page }));
  return [...entries.slice(0, anchor + 1), ...plugins, ...entries.slice(anchor + 1)];
}

/**
 * The packages with a settings page, read while ``enabled`` and connected.
 *
 * The last answer is kept while the connection is down so an open plugin entry does not disappear;
 * a failed read shows no plugin entries and is retried on the next connection.
 */
export function useApplicationSettingsPages(enabled: boolean): ApplicationSettingsPage[] {
  const { isConnected, request } = useSettingsServices();
  const [pages, setPages] = useState<ApplicationSettingsPage[]>([]);
  useEffect(() => {
    if (!enabled || !isConnected) return undefined;
    let cancelled = false;
    request(SETTINGS_PAGES_METHOD, {}).then(
      (answer) => {
        if (!cancelled) setPages(parseSettingsPages(answer));
      },
      (error: unknown) => {
        if (cancelled) return;
        setPages([]);
        console.warn('[settings] cannot read extension settings pages', error);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [enabled, isConnected, request]);
  return enabled ? pages : [];
}

/** The settings page of one package; remounted when another package is selected. */
export function PluginSettingsPage({ page }: { page: ApplicationSettingsPage }) {
  return (
    <div className="settings-extension-page" data-testid="settings-extension-page" data-variant={page.packageId}>
      <WebUIHost
        key={page.packageId}
        slotId={SETTINGS_PAGE_SLOT_ID}
        packageId={page.packageId}
        instanceKey={SETTINGS_PAGE_INSTANCE_KEY}
      />
    </div>
  );
}
