/**
 * In-memory owner of the plugin tab state for one panel region.
 *
 * The panel host can unmount without a page reload (hiding the workspace, narrow viewports,
 * leaving the chat page), so the added tabs live outside React. State is intentionally not
 * persisted: a page reload re-discovers the registered plugins and starts from an empty state.
 * Each slot gets its own store so a future plugin panel region does not share tabs with the
 * right workspace.
 */

import {
  EMPTY_WEBUI_PLUGIN_PANEL_TABS,
  selectNativeWebUIPanelTab,
  type WebUIPluginPanelTabsState,
} from './pluginPanelTabs.ts';

export interface WebUIPluginPanelTabsStore {
  getState(): WebUIPluginPanelTabsState;
  subscribe(listener: () => void): () => void;
  /** Apply a pure transition; an unchanged state reference does not notify subscribers. */
  update(
    updater: WebUIPluginPanelTabsState
      | ((current: WebUIPluginPanelTabsState) => WebUIPluginPanelTabsState),
  ): void;
  /** Test helper: clear this scope without a page reload. */
  reset(): void;
}

export function createWebUIPluginPanelTabsStore(): WebUIPluginPanelTabsStore {
  let state: WebUIPluginPanelTabsState = EMPTY_WEBUI_PLUGIN_PANEL_TABS;
  const listeners = new Set<() => void>();

  const update: WebUIPluginPanelTabsStore['update'] = (updater) => {
    const next = typeof updater === 'function' ? updater(state) : updater;
    if (next === state) return;
    state = next;
    for (const listener of listeners) listener();
  };

  return {
    getState: () => state,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    update,
    reset: () => update(EMPTY_WEBUI_PLUGIN_PANEL_TABS),
  };
}

const stores = new Map<string, WebUIPluginPanelTabsStore>();

/** One state scope per panel region; callers pass the region's slot id. */
export function getWebUIPluginPanelTabsStore(slotId: string): WebUIPluginPanelTabsStore {
  const existing = stores.get(slotId);
  if (existing) return existing;
  const store = createWebUIPluginPanelTabsStore();
  stores.set(slotId, store);
  return store;
}

/** Give a native page ownership even when the panel host is currently unmounted. */
export function activateNativeWebUIPanel(slotId: string): void {
  getWebUIPluginPanelTabsStore(slotId).update(selectNativeWebUIPanelTab);
}
