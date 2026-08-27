import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Loading } from '../../../components/ui';
import type { SettingValue, SettingsSource } from '../registry/types';
import { useSettingsServices } from './SettingsServicesProvider';
import { serializeConfigSettingValue } from './settingsSourceContract';
import { useSettingsConfig } from './useSettingsConfig';

export type SettingsSourceController = {
  values: Readonly<Record<string, unknown>>;
  savingKeys: ReadonlySet<string>;
  save: (updates: Record<string, SettingValue>, operation: string) => Promise<unknown>;
  patchLocal: (updates: Record<string, unknown>) => void;
};

const SettingsSourceContext = createContext<SettingsSourceController | null>(null);

export function useSettingsSource(): SettingsSourceController {
  const value = useContext(SettingsSourceContext);
  if (!value) throw new Error('Settings item requires a module settings source');
  return value;
}

function addSavingKeys(current: ReadonlySet<string>, keys: readonly string[]): Set<string> {
  return new Set([...current, ...keys]);
}

function removeSavingKeys(current: ReadonlySet<string>, keys: readonly string[]): Set<string> {
  const next = new Set(current);
  keys.forEach((key) => next.delete(key));
  return next;
}

function ConfigSourceProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const { config, setConfig, loading, error, save: saveConfig } = useSettingsConfig();
  const [savingKeys, setSavingKeys] = useState<ReadonlySet<string>>(() => new Set());

  const save = useCallback(
    async (updates: Record<string, SettingValue>, operation: string) => {
      const serialized = Object.fromEntries(
        Object.entries(updates).map(([key, value]) => [key, serializeConfigSettingValue(value)]),
      );
      const keys = Object.keys(serialized);
      const previous = Object.fromEntries(keys.map((key) => [key, config[key]]));
      setSavingKeys((current) => addSavingKeys(current, keys));
      setConfig((current) => ({ ...current, ...serialized }));
      try {
        return await saveConfig(serialized, operation);
      } catch (saveError) {
        setConfig((current) => {
          const next = { ...current };
          keys.forEach((key) => {
            if (previous[key] === undefined) delete next[key];
            else next[key] = previous[key];
          });
          return next;
        });
        throw saveError;
      } finally {
        setSavingKeys((current) => removeSavingKeys(current, keys));
      }
    },
    [config, saveConfig, setConfig],
  );
  const patchLocal = useCallback(
    (updates: Record<string, unknown>) => setConfig((current) => ({ ...current, ...updates })),
    [setConfig],
  );
  const value = useMemo<SettingsSourceController>(
    () => ({ values: config, savingKeys, save, patchLocal }),
    [config, patchLocal, save, savingKeys],
  );

  if (loading)
    return (
      <div className="settings-page__loading">
        <Loading aria-label={t('common.loading')} />
      </div>
    );
  if (error)
    return (
      <div className="settings-page__error" role="alert">
        {error}
      </div>
    );
  return <SettingsSourceContext.Provider value={value}>{children}</SettingsSourceContext.Provider>;
}

type BrowserSettingsState = {
  chrome_path: string;
  browser_type: 'auto' | 'chrome' | 'msedge';
  headless: boolean;
};

function normalizeBrowserType(value: unknown): BrowserSettingsState['browser_type'] {
  return value === 'chrome' || value === 'msedge' ? value : 'auto';
}

function normalizeBrowserState(value: Record<string, unknown> | undefined): BrowserSettingsState {
  return {
    chrome_path: typeof value?.chrome_path === 'string' ? value.chrome_path : '',
    browser_type: normalizeBrowserType(value?.browser_type),
    headless: value?.headless === undefined ? true : value.headless === true,
  };
}

function BrowserSourceProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const { isConnected, request, saveQueue } = useSettingsServices();
  const [values, setValues] = useState<BrowserSettingsState>(() => normalizeBrowserState(undefined));
  const [savingKeys, setSavingKeys] = useState<ReadonlySet<string>>(() => new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const requestId = useRef(0);

  useEffect(() => {
    if (!isConnected) {
      setLoading(false);
      return undefined;
    }
    const id = ++requestId.current;
    setLoading(true);
    setError('');
    void request<Record<string, unknown>>('path.get')
      .then((payload) => {
        if (id === requestId.current) setValues(normalizeBrowserState(payload));
      })
      .catch((loadError) => {
        if (id === requestId.current)
          setError(loadError instanceof Error ? loadError.message : t('browser.errors.loadPath'));
      })
      .finally(() => {
        if (id === requestId.current) setLoading(false);
      });
    return () => {
      requestId.current += 1;
    };
  }, [isConnected, request, t]);

  const save = useCallback(
    async (updates: Record<string, SettingValue>, operation: string) => {
      const keys = Object.keys(updates);
      const previous = values;
      const next = normalizeBrowserState({ ...values, ...updates });
      setValues(next);
      setError('');
      setSavingKeys((current) => addSavingKeys(current, keys));
      try {
        const result = await saveQueue.enqueue(operation, () =>
          request<Record<string, unknown>>('path.set', next, { timeoutMs: 600_000 }),
        );
        setValues((current) => normalizeBrowserState({ ...current, ...result }));
        return result;
      } catch (saveError) {
        setValues((current) =>
          normalizeBrowserState({
            ...current,
            ...Object.fromEntries(keys.map((key) => [key, previous[key as keyof BrowserSettingsState]])),
          }),
        );
        throw saveError;
      } finally {
        setSavingKeys((current) => removeSavingKeys(current, keys));
      }
    },
    [request, saveQueue, t, values],
  );
  const patchLocal = useCallback((updates: Record<string, unknown>) => {
    setValues((current) => normalizeBrowserState({ ...current, ...updates }));
  }, []);
  const value = useMemo<SettingsSourceController>(
    () => ({ values, savingKeys, save, patchLocal }),
    [patchLocal, save, savingKeys, values],
  );

  if (loading)
    return (
      <div className="settings-page__loading">
        <Loading aria-label={t('common.loading')} />
      </div>
    );
  if (error)
    return (
      <div className="settings-page__error" role="alert">
        {error}
      </div>
    );
  return <SettingsSourceContext.Provider value={value}>{children}</SettingsSourceContext.Provider>;
}

function LocaleSourceProvider({ children }: { children: ReactNode }) {
  const { i18n } = useTranslation();
  const { request, saveQueue } = useSettingsServices();
  const [language, setLanguage] = useState(i18n.language.startsWith('zh') ? 'zh' : 'en');
  const [savingKeys, setSavingKeys] = useState<ReadonlySet<string>>(() => new Set());

  const save = useCallback(
    async (updates: Record<string, SettingValue>, operation: string) => {
      if (Object.keys(updates).length !== 1 || typeof updates.preferred_language !== 'string') {
        throw new Error('Locale settings source only accepts preferred_language');
      }
      const next = updates.preferred_language;
      if (next !== 'zh' && next !== 'en') throw new Error(`Unsupported settings language: ${next}`);
      const previous = language;
      setLanguage(next);
      setSavingKeys(new Set(['preferred_language']));
      try {
        await i18n.changeLanguage(next);
        return await saveQueue.enqueue(operation, () => request('locale.set_conf', { preferred_language: next }));
      } catch (saveError) {
        setLanguage(previous);
        await i18n.changeLanguage(previous);
        throw saveError;
      } finally {
        setSavingKeys(new Set());
      }
    },
    [i18n, language, request, saveQueue],
  );
  const patchLocal = useCallback((updates: Record<string, unknown>) => {
    if (updates.preferred_language === 'zh' || updates.preferred_language === 'en') {
      setLanguage(updates.preferred_language);
    }
  }, []);
  const value = useMemo<SettingsSourceController>(
    () => ({ values: { preferred_language: language }, savingKeys, save, patchLocal }),
    [language, patchLocal, save, savingKeys],
  );
  return <SettingsSourceContext.Provider value={value}>{children}</SettingsSourceContext.Provider>;
}

export function SettingsSourceProvider({ source, children }: { source?: SettingsSource; children: ReactNode }) {
  if (source === 'config') return <ConfigSourceProvider>{children}</ConfigSourceProvider>;
  if (source === 'browser') return <BrowserSourceProvider>{children}</BrowserSourceProvider>;
  if (source === 'locale') return <LocaleSourceProvider>{children}</LocaleSourceProvider>;
  return children;
}
