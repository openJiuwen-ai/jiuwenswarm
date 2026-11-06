import { create } from 'zustand';
import { systemPluginApi } from '../services/extensionsV2Api';
import type { SystemPluginDetail, SystemPluginSummary } from '../types/systemPlugin';

type MutationResult = { ok: true } | { ok: false; error: string };

interface SystemPluginState {
  catalog: SystemPluginSummary[];
  mine: SystemPluginSummary[];
  detailCache: Record<string, SystemPluginDetail>;
  busyIds: Record<string, boolean>;
  loading: boolean;
  error: string | null;
  loadCatalog: () => Promise<void>;
  loadMine: () => Promise<void>;
  loadDetail: (id: string) => Promise<boolean>;
  install: (id: string) => Promise<MutationResult>;
  importPath: (path: string) => Promise<MutationResult>;
  uninstall: (id: string) => Promise<MutationResult>;
  reload: (id: string) => Promise<MutationResult>;
  clearError: () => void;
}

function messageOf(error: unknown): string {
  return error instanceof Error && error.message ? error.message : String(error);
}

export const useSystemPluginStore = create<SystemPluginState>((set, get) => {
  const setBusy = (id: string, busy: boolean) =>
    set((state) => ({ busyIds: { ...state.busyIds, [id]: busy } }));

  const refresh = async () => {
    await Promise.all([get().loadCatalog(), get().loadMine()]);
  };

  const isListed = (id: string) =>
    [...get().catalog, ...get().mine].some((item) => item.id === id);

  const syncDetail = async (id: string) => {
    if (!get().detailCache[id]) return;
    if (isListed(id)) {
      await get().loadDetail(id);
      return;
    }
    set((state) => {
      const detailCache = { ...state.detailCache };
      delete detailCache[id];
      return { detailCache };
    });
  };

  let pendingLoads = 0;

  const load = async (scope: 'catalog' | 'mine') => {
    pendingLoads += 1;
    set({ loading: true });
    try {
      const packages = await systemPluginApi.catalog(scope);
      set(scope === 'catalog' ? { catalog: packages } : { mine: packages });
    } catch (error) {
      set({ error: messageOf(error) });
    } finally {
      pendingLoads -= 1;
      if (pendingLoads === 0) set({ loading: false });
    }
  };

  const mutate = async (id: string, action: () => Promise<void>): Promise<MutationResult> => {
    setBusy(id, true);
    try {
      await action();
      await refresh();
      await syncDetail(id);
      return { ok: true };
    } catch (error) {
      const message = messageOf(error);
      set({ error: message });
      return { ok: false, error: message };
    } finally {
      setBusy(id, false);
    }
  };

  return {
    catalog: [],
    mine: [],
    detailCache: {},
    busyIds: {},
    loading: false,
    error: null,

    clearError: () => set({ error: null }),

    loadCatalog: () => load('catalog'),

    loadMine: () => load('mine'),

    loadDetail: async (id) => {
      try {
        const detail = await systemPluginApi.show(id);
        if (!detail) return false;
        set((state) => ({ detailCache: { ...state.detailCache, [id]: detail } }));
        return true;
      } catch (error) {
        set({ error: messageOf(error) });
        return false;
      }
    },

    install: (id) => mutate(id, () => systemPluginApi.install(id)),
    importPath: async (path) => {
      set({ loading: true });
      try {
        await systemPluginApi.importPath(path);
        await refresh();
        return { ok: true };
      } catch (error) {
        return { ok: false, error: messageOf(error) };
      } finally {
        set({ loading: false });
      }
    },
    uninstall: (id) => mutate(id, () => systemPluginApi.uninstall(id)),
    reload: (id) => mutate(id, () => systemPluginApi.reload(id)),
  };
});
