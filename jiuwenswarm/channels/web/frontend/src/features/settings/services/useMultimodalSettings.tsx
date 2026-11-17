import { createContext, useContext, useEffect, useMemo, useSyncExternalStore, type ReactNode } from 'react';
import { useSettingsServices } from './SettingsServicesProvider';
import { SETTINGS_CONFIG_CHANGED_EVENT } from './settingsContract';
import { MultimodalSettingsStore } from './multimodalSettings';

const MultimodalContext = createContext<MultimodalSettingsStore | null>(null);

export function MultimodalSettingsProvider({ children }: { children: ReactNode }) {
  const { request, saveQueue } = useSettingsServices();
  const store = useMemo(() => new MultimodalSettingsStore(request, saveQueue), [request, saveQueue]);
  useEffect(() => {
    const refresh = () => {
      if (store.getSnapshot().snapshot) void store.load();
    };
    window.addEventListener(SETTINGS_CONFIG_CHANGED_EVENT, refresh);
    return () => window.removeEventListener(SETTINGS_CONFIG_CHANGED_EVENT, refresh);
  }, [store]);
  return <MultimodalContext.Provider value={store}>{children}</MultimodalContext.Provider>;
}

export function useMultimodalSettings() {
  const store = useContext(MultimodalContext);
  if (!store) throw new Error('MultimodalSettingsProvider is required');
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  const { isConnected } = useSettingsServices();
  useEffect(() => {
    if (isConnected) void store.load();
  }, [isConnected, store]);
  return { ...state, store };
}
