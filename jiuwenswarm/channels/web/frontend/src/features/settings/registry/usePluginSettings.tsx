import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { SettingRow } from '../components/SettingRow';
import { useSettingsServices } from '../services/SettingsServicesProvider';
import { WebUIHost } from '../../webui/react/WebUIHost';
import { useOptionalWebUIAppContext } from '../../webui/react/WebUIProvider';
import { useWebUISlotContext } from '../../webui/react/WebUISlotContext';
import { buttonIcon } from '../../webui/renderer/adapters/webuiButton/buttonIcon';
import { WEBUI_ICON_NAMES } from '../../webui/icons/names';
import { composePluginSettings, type SettingsContribution, type SettingsLocation } from './pluginLocations';
import type { SettingsPageDefinition } from './types';

type Translation = { zh: string; en: string };
type Declaration = {
  package_id: string;
  id: string;
  kind: 'menu' | 'section' | 'item';
  location: SettingsLocation;
  title?: Translation;
  description?: Translation;
  icon?: string;
  scope?: 'session' | 'application';
  instance_key?: string;
  presentation?: 'row' | 'content';
};
type Catalog = { revision: string; items: Declaration[]; errors: Record<string, string> };

function isText(value: unknown): value is Translation {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const text = value as Record<string, unknown>;
  return (
    Object.keys(text).length === 2 && ['zh', 'en'].every((key) => typeof text[key] === 'string' && !!text[key].trim())
  );
}

export function readSettingsCatalog(value: unknown): Catalog {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid settings catalog');
  const raw = value as Catalog;
  if (
    typeof raw.revision !== 'string' ||
    !raw.revision ||
    !Array.isArray(raw.items) ||
    raw.items.length > 512 ||
    !raw.errors ||
    typeof raw.errors !== 'object' ||
    Array.isArray(raw.errors) ||
    Object.values(raw.errors).some((error) => typeof error !== 'string')
  ) {
    throw new Error('Invalid settings catalog');
  }
  const seen = new Set<string>();
  const targets = new Set<string>();
  for (const item of raw.items) {
    if (
      !item ||
      typeof item !== 'object' ||
      !['menu', 'section', 'item'].includes(item.kind) ||
      typeof item.package_id !== 'string' ||
      !item.package_id.trim() ||
      typeof item.id !== 'string' ||
      !item.id.trim() ||
      (item.title !== undefined && !isText(item.title)) ||
      (item.kind !== 'item' && !isText(item.title)) ||
      (item.description !== undefined && (!item.title || !isText(item.description)))
    )
      throw new Error('Invalid settings declaration');
    if (
      item.kind === 'item' &&
      (!['session', 'application'].includes(item.scope ?? '') ||
        typeof item.instance_key !== 'string' ||
        !item.instance_key.trim() ||
        item.instance_key !== item.instance_key.trim() ||
        item.instance_key.includes('|') ||
        new TextEncoder().encode(item.instance_key).length > 200 ||
        !['row', 'content'].includes(item.presentation ?? '') ||
        (item.presentation === 'row') !== (item.title !== undefined))
    )
      throw new Error('Invalid settings item target');
    if (
      item.kind !== 'item' &&
      (item.scope !== undefined || item.instance_key !== undefined || item.presentation !== undefined)
    )
      throw new Error('Invalid settings group target');
    if (item.kind === 'item') {
      const target = JSON.stringify([item.package_id, item.scope, item.instance_key]);
      if (targets.has(target)) throw new Error('Duplicate settings render target');
      targets.add(target);
    }
    const location = item.location;
    const parent = item.kind === 'menu' ? [] : item.kind === 'section' ? ['menu'] : ['menu', 'section'];
    if (
      !location ||
      typeof location !== 'object' ||
      Array.isArray(location) ||
      parent.some((key) => typeof location[key as keyof SettingsLocation] !== 'string') ||
      Object.keys(location).some((key) => ![...parent, 'before', 'after'].includes(key)) ||
      (location.before !== undefined && location.after !== undefined) ||
      Object.values(location).some((entry) => typeof entry !== 'string' || !entry.trim())
    )
      throw new Error('Invalid settings location');
    if (item.kind === 'menu' && !WEBUI_ICON_NAMES.includes(item.icon as (typeof WEBUI_ICON_NAMES)[number]))
      throw new Error('Invalid settings icon');
    const identity = JSON.stringify([item.package_id, item.id]);
    if (seen.has(identity)) throw new Error('Duplicate settings declaration');
    seen.add(identity);
  }
  // Package order is canonical; declaration order within a package remains significant.
  return {
    revision: raw.revision,
    items: raw.items
      .map((item, index) => ({ item, index }))
      .sort((a, b) =>
        a.item.package_id < b.item.package_id ? -1 : a.item.package_id > b.item.package_id ? 1 : a.index - b.index,
      )
      .map(({ item }) => item),
    errors: raw.errors,
  };
}

function resourceId(value: string): string {
  return Array.from(new TextEncoder().encode(value), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function PluginSettingsItem({
  declaration,
  titleKey,
  descriptionKey,
}: {
  declaration: Declaration;
  titleKey?: string;
  descriptionKey?: string;
}) {
  const { t } = useTranslation();
  const control = (
    <WebUIHost
      slotId="application.settings"
      scope={declaration.scope}
      packageId={declaration.package_id}
      instanceKey={declaration.instance_key}
      style={{ width: '100%' }}
    />
  );
  return titleKey ? (
    <SettingRow title={t(titleKey)} description={descriptionKey ? t(descriptionKey) : undefined}>
      {control}
    </SettingRow>
  ) : (
    <SettingRow title={null} content={control} />
  );
}

export function usePluginSettings(base: SettingsPageDefinition) {
  const { i18n } = useTranslation();
  const { request, isConnected } = useSettingsServices();
  const webui = useOptionalWebUIAppContext();
  const { sessionId } = useWebUISlotContext();
  const hasSession = !!sessionId;
  const [catalog, setCatalog] = useState<Catalog>({ revision: '', items: [], errors: {} });
  const [loadError, setLoadError] = useState(false);
  const sequence = useRef(0);
  const refresh = useCallback(() => {
    const current = ++sequence.current;
    if (!webui || !isConnected) return;
    void request('webui.settings.catalog', {})
      .then((value) => {
        const next = readSettingsCatalog(value);
        if (current !== sequence.current) return;
        for (const item of next.items) {
          const prefix = `settingsPlugins.${resourceId(item.package_id)}.${resourceId(item.id)}`;
          for (const language of ['zh', 'en'] as const) {
            if (item.title) i18n.addResource(language, 'translation', `${prefix}.title`, item.title[language]);
            if (item.description)
              i18n.addResource(language, 'translation', `${prefix}.description`, item.description[language]);
          }
        }
        setCatalog((current) => (current.revision === next.revision ? current : next));
        setLoadError(false);
      })
      .catch(() => {
        if (current === sequence.current) setLoadError(true);
      });
  }, [i18n, isConnected, request, webui]);
  useEffect(() => {
    refresh();
    window.addEventListener('focus', refresh);
    return () => {
      sequence.current++;
      window.removeEventListener('focus', refresh);
    };
  }, [refresh]);
  const contributions = useMemo(() => {
    const values: (SettingsContribution & { scope?: 'session' | 'application' })[] = catalog.items.map(
      (declaration) => {
        const id = `${declaration.package_id}::${declaration.id}`;
        const prefix = `settingsPlugins.${resourceId(declaration.package_id)}.${resourceId(declaration.id)}`;
        const titleKey = declaration.title ? `${prefix}.title` : undefined;
        const descriptionKey = declaration.description ? `${prefix}.description` : undefined;
        if (declaration.kind === 'menu')
          return {
            kind: 'menu',
            location: declaration.location,
            value: { id, titleKey: titleKey!, descriptionKey, icon: () => buttonIcon(declaration.icon), sections: [] },
          };
        if (declaration.kind === 'section')
          return {
            kind: 'section',
            location: declaration.location,
            value: { id, titleKey, descriptionKey, items: [] },
          };
        return {
          kind: 'item',
          location: declaration.location,
          scope: declaration.scope,
          value: {
            id,
            component: 'custom',
            render: () => (
              <PluginSettingsItem declaration={declaration} titleKey={titleKey} descriptionKey={descriptionKey} />
            ),
          },
        };
      },
    );
    return values;
  }, [catalog]);
  const result = useMemo(
    () =>
      composePluginSettings(
        base,
        contributions.filter((entry) => entry.scope !== 'session' || hasSession),
      ),
    [base, contributions, hasSession],
  );
  return {
    definition: result.definition,
    loadError,
    contributionErrors: result.errors.length + Object.keys(catalog.errors).length,
  };
}
