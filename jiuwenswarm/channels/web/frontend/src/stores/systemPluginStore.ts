import { create } from 'zustand';
import { catalogScope, scheduleCatalogRefresh } from '../features/catalogCache';
import {
  isTransientManagementError,
  systemPluginApi,
  waitForClientContext,
} from '../services/extensionsV2Api';
import type { SystemPluginDetail, SystemPluginSummary } from '../types/systemPlugin';

type MutationFailure = { ok: false; error: string; code?: string };
type MutationResult = { ok: true } | MutationFailure;
type UninstallSuccess = { ok: true; detail: 'removed' | 'updated' | 'unavailable' };
type UninstallResult = UninstallSuccess | MutationFailure;
type SystemPluginLoadOptions = { refresh?: boolean; silent?: boolean };
/** ``reportError: false`` keeps the failure out of the global error toast;
 *  the caller shows its own, more specific prompt. */
type MutationOptions = { reportError?: boolean };
type ListLoadResult = { ok: true; packages?: SystemPluginSummary[] } | { ok: false };

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
  /** 安装请求进行中（卡片展示“安装中”），区别于卸载/开关的 busy。 */
  installingIds: Record<string, boolean>;
  loading: boolean;
  error: string | null;
  /** Stable report code of the last failure, e.g. ``EXTENSION_PACKAGE_BUSY``. */
  errorCode: string | null;
  loadCatalog: (options?: SystemPluginLoadOptions) => Promise<boolean>;
  loadMine: (options?: SystemPluginLoadOptions) => Promise<boolean>;
  loadDetail: (id: string, version?: string, cacheId?: string) => Promise<boolean>;
  install: (id: string, version?: string, options?: MutationOptions) => Promise<MutationResult>;
  importPath: (path: string) => Promise<MutationResult>;
  uninstall: (id: string, version?: string, detailId?: string) => Promise<UninstallResult>;
  reload: (id: string, version?: string) => Promise<MutationResult>;
  setEnabled: (id: string, enable: boolean, version?: string) => Promise<MutationResult>;
  clearError: () => void;
}

function uniqueById<T extends { id: string }>(items: T[]): T[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
}

function messageOf(error: unknown): string {
  return error instanceof Error && error.message ? error.message : String(error);
}

function errorCodeOf(error: unknown): string | undefined {
  if (!error || typeof error !== 'object' || !('code' in error)) return undefined;
  const code = (error as { code?: unknown }).code;
  return typeof code === 'string' ? code : undefined;
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

  const load = async (scope: 'catalog' | 'mine', options?: SystemPluginLoadOptions): Promise<ListLoadResult> => {
    const silent = options?.silent ?? false;
    const seq = ++listRequestSeq[scope];
    const requestScope = catalogScope();
    if (!silent) {
      pendingLoads += 1;
      set({ loading: true });
    }
    try {
      const result = await systemPluginApi.catalog(scope, options?.refresh === true);
      if (requestScope !== catalogScope()) return { ok: true };
      const packages = uniqueById(result);
      if (listRequestSeq[scope] === seq) {
        scheduleCatalogRefresh(
          `systemPluginStore.ts:${scope}`,
          result.cache,
          () => { void load(scope, { silent: true }); },
          () => listRequestSeq[scope] === seq,
        );
        set(scope === 'catalog' ? { catalog: packages } : { mine: packages });
      }
      return { ok: true, packages };
    } catch (error) {
      if (requestScope !== catalogScope() || listRequestSeq[scope] !== seq) return { ok: true };
      if (!silent && !isTransientManagementError(error)) set({ error: messageOf(error), errorCode: null });
      return { ok: false };
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

  const mutate = async <T extends { ok: true }>(
    id: string,
    action: () => Promise<T>,
    options?: MutationOptions,
  ): Promise<T | MutationFailure> => {
    setBusy(id, true);
    try {
      return await action();
    } catch (error) {
      const message = messageOf(error);
      const code = errorCodeOf(error);
      if (options?.reportError !== false) set({ error: message, errorCode: code ?? null });
      return { ok: false, error: message, code };
    } finally {
      setBusy(id, false);
    }
  };

  const mutateAndSync = (
    id: string,
    action: () => Promise<void>,
    options?: { tolerateTransient?: boolean; confirmed?: () => void } & MutationOptions,
  ): Promise<MutationResult> =>
    mutate(id, async () => {
      let handled = false;
      try {
        await action();
        handled = true;
      } catch (error) {
        // Only the toggle can fall back to the state below: its result is re-read and no
        // destructive UI action depends on it. Install/uninstall/reload must report failures,
        // otherwise a denied call would close the detail page as if the package was removed.
        if (!options?.tolerateTransient || !isTransientManagementError(error)) throw error;
        await waitForClientContext();
      }
      if (handled) options?.confirmed?.();
      await refresh();
      await syncDetail(id);
      if (handled) options?.confirmed?.();
      return { ok: true };
    }, options);

  return {
    catalog: [],
    mine: [],
    detailCache: {},
    busyIds: {},
    installingIds: {},
    loading: false,
    error: null,
    errorCode: null,

    clearError: () => set({ error: null, errorCode: null }),

    loadCatalog: async (options) => (await load('catalog', options)).ok,

    loadMine: async (options) => (await load('mine', options)).ok,

    loadDetail: async (id, version, cacheId = id) => {
      try {
        const detail = await systemPluginApi.show(id, version);
        if (!detail) return false;
        const next = reusePreviewUrl(detail);
        set((state) => ({ detailCache: { ...state.detailCache, [id]: next, [cacheId]: next } }));
        return true;
      } catch (error) {
        if (!isTransientManagementError(error)) set({ error: messageOf(error), errorCode: null });
        return false;
      }
    },

    install: async (id, version, options) => {
      set((state) => ({ installingIds: { ...state.installingIds, [id]: true } }));
      try {
        return await mutateAndSync(id, () => systemPluginApi.install(id, version), options);
      } finally {
        set((state) => {
          const installingIds = { ...state.installingIds };
          delete installingIds[id];
          return { installingIds };
        });
      }
    },
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
    uninstall: (id, version, detailId = id) =>
      mutate<UninstallSuccess>(id, async () => {
        await systemPluginApi.uninstall(id, version);
        void load('catalog', { silent: true });
        const mine = await load('mine');
        // The marketplace can keep listing Hub and preset packages after uninstall.
        const installed = mine.ok ? mine.packages?.some((item) => item.id === id || item.packageId === id) : undefined;
        if (version && installed && (await get().loadDetail(id, undefined, detailId))) {
          return { ok: true, detail: 'updated' };
        }
        set((state) => {
          const detailCache = { ...state.detailCache };
          for (const [key, detail] of Object.entries(detailCache)) {
            if (key === id || key === detailId || detail.packageId === id) delete detailCache[key];
          }
          return { detailCache };
        });
        return { ok: true, detail: !version || installed === false ? 'removed' : 'unavailable' };
      }),
    reload: (id, version) => mutateAndSync(id, () => systemPluginApi.reload(id, version)),
    setEnabled: (id, enable, version) =>
      mutateAndSync(id, () => systemPluginApi.setEnabled(id, enable, version), {
        tolerateTransient: true,
        confirmed: () => applyEnabled(id, enable),
      }),
  };
});
