import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Loading } from '../../../components/ui';
import type { SettingValue, SettingsSource } from '../registry/types';
import { useMultimodalSettings } from './useMultimodalSettings';
import { MULTIMODAL_CAPABILITIES, multimodalModelOptions, type MultimodalCapability } from './multimodalSettings';
import { useSettingsServices } from './SettingsServicesProvider';
import { serializeConfigSettingValue } from './settingsSourceContract';
import { useSettingsConfig } from './useSettingsConfig';
import { applyBrowserDisplaySettings, browserDisplayRevision } from '../../desktopBrowserDisplay';

export type SettingsSourceController = {
  optionsFor?: (key: string) => readonly { value: SettingValue; label: string }[];
  requiresConfiguration?: (key: string) => boolean;
  values: Readonly<Record<string, unknown>>;
  savingKeys: ReadonlySet<string>;
  save: (
    updates: Record<string, SettingValue>,
    operation: string,
    options?: { optimistic?: boolean },
  ) => Promise<unknown>;
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
    async (updates: Record<string, SettingValue>, operation: string, options?: { optimistic?: boolean }) => {
      const serialized = Object.fromEntries(
        Object.entries(updates).map(([key, value]) => [key, serializeConfigSettingValue(value)]),
      );
      const keys = Object.keys(serialized);
      const previous = Object.fromEntries(keys.map((key) => [key, config[key]]));
      setSavingKeys((current) => addSavingKeys(current, keys));
      const optimistic = options?.optimistic !== false;
      const visible = { ...serialized };
      delete visible.moa_jev_api_key;
      delete visible.moa_jev_api_key_clear;
      if (optimistic) setConfig((current) => ({ ...current, ...visible }));
      try {
        return await saveConfig(serialized, operation);
      } catch (saveError) {
        if (optimistic) {
          setConfig((current) => {
            const next = { ...current };
            Object.keys(visible).forEach((key) => {
              if (previous[key] === undefined) delete next[key];
              else next[key] = previous[key];
            });
            return next;
          });
        }
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
      <div className="settings-page__loading" data-testid="settings-source-loading" data-variant="config">
        <Loading aria-label={t('common.loading')} />
      </div>
    );
  if (error)
    return (
      <div className="settings-page__error" role="alert" data-testid="settings-source-error" data-variant="config">
        {error}
      </div>
    );
  return <SettingsSourceContext.Provider value={value}>{children}</SettingsSourceContext.Provider>;
}

type BrowserDecisionMode = 'llm' | 'shadow' | 'hybrid';
type BrowserDecisionProvider = 'openrouter' | 'typesafe' | 'custom';

type BrowserSettingsState = {
  chrome_path: string;
  headless: boolean;
  decision_mode: BrowserDecisionMode;
  decision_provider: BrowserDecisionProvider;
  decision_model: string;
  decision_display_name: string;
  decision_api_base: string;
  // Read-only: path.get reports whether the provider's key is set, never the key.
  jev_key_configured: boolean;
};

const BROWSER_DECISION_MODES: readonly unknown[] = ['llm', 'shadow', 'hybrid'];
const BROWSER_DECISION_PROVIDERS: readonly unknown[] = ['openrouter', 'typesafe', 'custom'];

function normalizeBrowserState(value: Record<string, unknown> | undefined): BrowserSettingsState {
  return {
    chrome_path: typeof value?.chrome_path === 'string' ? value.chrome_path : '',
    headless: value?.headless === undefined ? true : value.headless === true,
    decision_mode: BROWSER_DECISION_MODES.includes(value?.decision_mode)
      ? (value?.decision_mode as BrowserDecisionMode)
      : 'llm',
    decision_provider: BROWSER_DECISION_PROVIDERS.includes(value?.decision_provider)
      ? (value?.decision_provider as BrowserDecisionProvider)
      : 'typesafe',
    jev_key_configured: value?.jev_key_configured === true,
    decision_model: typeof value?.decision_model === 'string' ? value.decision_model : '',
    decision_display_name: typeof value?.decision_display_name === 'string' ? value.decision_display_name : '',
    decision_api_base: typeof value?.decision_api_base === 'string' ? value.decision_api_base : '',
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
    const displayRevision = browserDisplayRevision();
    setLoading(true);
    setError('');
    void request<Record<string, unknown>>('path.get')
      .then((payload) => {
        if (id === requestId.current) {
          setValues(normalizeBrowserState(payload));
          void applyBrowserDisplaySettings(payload, displayRevision);
        }
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
          request<Record<string, unknown>>('path.set', {
            ...next,
            // Only the Jev dialog edits these fields; unrelated saves must not reset them.
            decision_model: updates.decision_model,
            decision_display_name: updates.decision_display_name,
            decision_api_base: updates.decision_api_base,
          }, { timeoutMs: 600_000 }),
        );
        setValues((current) => normalizeBrowserState({ ...current, ...result }));
        await applyBrowserDisplaySettings(result);
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
      <div className="settings-page__loading" data-testid="settings-source-loading" data-variant="browser">
        <Loading aria-label={t('common.loading')} />
      </div>
    );
  if (error)
    return (
      <div className="settings-page__error" role="alert" data-testid="settings-source-error" data-variant="browser">
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

function MultimodalSourceProvider({ children }: { children: ReactNode }) {
  const { snapshot, saving, store } = useMultimodalSettings();
  const values = useMemo(() => Object.fromEntries(MULTIMODAL_CAPABILITIES.flatMap((tool) => [
    [`${tool}_enabled`, snapshot?.tools[tool].enabled ?? false],
    [`${tool}_model_id`, snapshot?.tools[tool].model_id ?? ''],
  ])), [snapshot]);
  const save = useCallback(async (updates: Record<string, SettingValue>) => {
    // A row writes one tool. Binding and enabled are submitted together when
    // present; the server owns automatic selection, confirmation and projection.
    const tool = MULTIMODAL_CAPABILITIES.find((id) => `${id}_enabled` in updates || `${id}_model_id` in updates);
    if (!tool) throw new Error('Unknown multimodal setting');
    const params: Record<string, unknown> = { tool_id: tool };
    if (`${tool}_enabled` in updates) params.enabled = updates[`${tool}_enabled`];
    if (`${tool}_model_id` in updates) params.model_id = updates[`${tool}_model_id`];
    return store.write('tools.update', params);
  }, [store]);
  const controller = useMemo<SettingsSourceController>(() => ({
    values,
    savingKeys: new Set(saving || !snapshot ? Object.keys(values) : []),
    save,
    patchLocal: () => undefined,
    requiresConfiguration: (key) => !snapshot?.tools[key.replace(/_enabled$/, '') as MultimodalCapability]?.model_id,
    optionsFor: (key) => {
      const tool = key.replace(/_model_id$/, '') as MultimodalCapability;
      return multimodalModelOptions(snapshot?.models ?? [], tool);
    },
  }), [save, saving, snapshot, values]);
  return <SettingsSourceContext.Provider value={controller}>{children}</SettingsSourceContext.Provider>;
}

export function SettingsSourceProvider({ source, children }: { source?: SettingsSource; children: ReactNode }) {
  if (source === 'config') return <ConfigSourceProvider>{children}</ConfigSourceProvider>;
  if (source === 'browser') return <BrowserSourceProvider>{children}</BrowserSourceProvider>;
  if (source === 'locale') return <LocaleSourceProvider>{children}</LocaleSourceProvider>;
  if (source === 'multimodal') return <MultimodalSourceProvider>{children}</MultimodalSourceProvider>;
  return children;
}
