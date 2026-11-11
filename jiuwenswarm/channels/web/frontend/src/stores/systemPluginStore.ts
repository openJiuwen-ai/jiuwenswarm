import { create } from 'zustand';
import { catalogScope, scheduleCatalogRefresh } from '../features/catalogCache';
import {
  isTransientManagementError,
  systemPluginApi,
  waitForClientContext,
} from '../services/extensionsV2Api';
import type { SystemPluginDetail, SystemPluginSummary } from '../types/systemPlugin';

type MutationResult = { ok: true } | { ok: false; error: string };
type SystemPluginLoadOptions = { refresh?: boolean; silent?: boolean };

// Hub preview links are short-lived presigned URLs that change on every request.
const PREVIEW_URL_TTL_MS = 20 * 60 * 1000;
const PREVIEW_URL_LIMIT = 32;
const previewUrls = new Map<string, { url: string; at: number }>();

function reusePreviewUrl(detail: SystemPluginDetail): SystemPluginDetail {
  if (!detail.preview || !/^https?:\/\//i.test(detail.preview)) return detail;
  const key = `${detail.assetId || detail.packageId || detail.id}@${detail.version}`;
  const cached = previewUrls.get(key);
  if (cached && Date.now() - cached.at < PREVIEW_URL_TTL_MS) {
    previewUrls.delete(key);
    previewUrls.set(key, cached);
    return { ...detail, preview: cached.url };
  }
  previewUrls.set(key, { url: detail.preview, at: Date.now() });
  while (previewUrls.size > PREVIEW_URL_LIMIT) {
    const oldest = previewUrls.keys().next().value;
    if (oldest === undefined) break;
    previewUrls.delete(oldest);
  }
  return detail;
}

interface SystemPluginState {
  catalog: SystemPluginSummary[];
  mine: SystemPluginSummary[];
  detailCache: Record<string, SystemPluginDetail>;
  busyIds: Record<string, boolean>;
  loading: boolean;
  error: string | null;
  loadCatalog: (options?: SystemPluginLoadOptions) => Promise<void>;
  loadMine: (options?: SystemPluginLoadOptions) => Promise<void>;
  loadDetail: (id: string, version?: string) => Promise<boolean>;
  install: (id: string, version?: string) => Promise<MutationResult>;
  importPath: (path: string) => Promise<MutationResult>;
  uninstall: (id: string, version?: string) => Promise<MutationResult>;
  reload: (id: string) => Promise<MutationResult>;
  setEnabled: (id: string, enable: boolean) => Promise<MutationResult>;
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
  const listRequestSeq: Record<'catalog' | 'mine', number> = { catalog: 0, mine: 0 };

  const load = async (scope: 'catalog' | 'mine', options?: SystemPluginLoadOptions) => {
    const silent = options?.silent ?? false;
    const seq = ++listRequestSeq[scope];
    const requestScope = catalogScope();
    if (!silent) {
      pendingLoads += 1;
      set({ loading: true });
    }
    try {
      const packages = await systemPluginApi.catalog(scope, options?.refresh === true);
      if (requestScope !== catalogScope() || listRequestSeq[scope] !== seq) return;
      scheduleCatalogRefresh(
        `systemPluginStore.ts:${scope}`,
        packages.cache,
        () => { void load(scope, { silent: true }); },
        () => listRequestSeq[scope] === seq,
      );
      set(scope === 'catalog' ? { catalog: packages } : { mine: packages });
    } catch (error) {
      if (requestScope !== catalogScope() || listRequestSeq[scope] !== seq) return;
      if (!isTransientManagementError(error)) set({ error: messageOf(error) });
    } finally {
      if (!silent) {
        pendingLoads -= 1;
        if (pendingLoads === 0) set({ loading: false });
      }
    }
  };

  // The toggle result is authoritative once the management call has resolved: the follow-up
  // catalog/detail reads can still be answered from a snapshot taken before it, or be rejected
  // while the client declarations swap, which left the switch showing the old state until a
  // manual reload. Keep the confirmed value on the item instead of relying on those reads.
  const applyEnabled = (id: string, enable: boolean) =>
    set((state) => {
      const patch = <T extends SystemPluginSummary>(item: T): T =>
        item.id === id || item.packageId === id ? { ...item, enabled: enable, active: enable } : item;
      const detail = state.detailCache[id];
      return {
        catalog: state.catalog.map(patch),
        mine: state.mine.map(patch),
        ...(detail ? { detailCache: { ...state.detailCache, [id]: patch(detail) } } : {}),
      };
    });

  const mutate = async (
    id: string,
    action: () => Promise<void>,
    confirmed?: () => void,
  ): Promise<MutationResult> => {
    setBusy(id, true);
    try {
      let handled = false;
      try {
        await action();
        handled = true;
      } catch (error) {
        if (!isTransientManagementError(error)) throw error;
        // The declarations swap or the login gate may reject the call after the package
        // state already changed. Resync from the server instead of reporting a failure.
        await waitForClientContext();
      }
      if (handled) confirmed?.();
      await refresh();
      await syncDetail(id);
      if (handled) confirmed?.();
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

    loadCatalog: (options) => load('catalog', options),

    loadMine: (options) => load('mine', options),

    loadDetail: async (id, version) => {
      try {
        const detail = await systemPluginApi.show(id, version);
        if (!detail) return false;
        const next = reusePreviewUrl(detail);
        set((state) => ({ detailCache: { ...state.detailCache, [id]: next } }));
        return true;
      } catch (error) {
        if (!isTransientManagementError(error)) set({ error: messageOf(error) });
        return false;
      }
    },

    install: (id, version) => mutate(id, () => systemPluginApi.install(id, version)),
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
    uninstall: (id, version) => mutate(id, () => systemPluginApi.uninstall(id, version)),
    reload: (id) => mutate(id, () => systemPluginApi.reload(id)),
    setEnabled: (id, enable) =>
      mutate(id, () => systemPluginApi.setEnabled(id, enable), () => applyEnabled(id, enable)),
  };
});
