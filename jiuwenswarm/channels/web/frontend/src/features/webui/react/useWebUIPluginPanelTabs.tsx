/**
 * Host state for a plugin panel region: discovery, tabs, blank draft tabs and pruning of vanished
 * contributions. A host only supplies the slot, the active session and its component style
 * overrides, so the same state machine can back another panel region later.
 *
 * The plus button creates a blank tab that lists the discovered plugins. Choosing one converts
 * that same tab into the plugin tab and activates it; the blank tab is closable like any plugin
 * tab. Closing the active tab activates its visible neighbour or falls back to the native page.
 * Closing an inactive tab keeps the current page. Closed plugin tabs stay
 * discovered, so a blank tab can add them again at any time.
 *
 * A contribution hidden only by the instance-visibility client preference keeps its tab parked:
 * the tab bar renders tabs backed by the visible discovery result, and re-enabling the
 * preference restores the parked tab without re-selecting it. Only a contribution that left both
 * the visible and the hidden discovery result is pruned.
 */

import { useEffect, useMemo, useRef, useSyncExternalStore, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import PluginIcon from '../../../assets/agent-management/extension.svg?react';
import type { PanelOpenRequest } from '../client/panelNavigation.ts';
import {
  chooseWebUIPluginPanel,
  closeWebUIPluginPanelTab,
  hideWebUIPluginPanels,
  isWebUIPluginPanelDraftTab,
  openWebUIPluginPanelDraft,
  removeWebUIPluginPanels,
  selectNativeWebUIPanelTab,
  selectWebUIPluginPanelTab,
  webUIPluginPanelIdFromTabId,
  webUIPluginPanelTabIdFromKey,
  webUIPluginPanelTabKey,
} from './pluginPanelTabs.ts';
import { getWebUIPluginPanelTabsStore } from './pluginPanelTabsStore.ts';
import type { WebUIPanelEntry } from './useWebUIPanels.tsx';
import { WebUIPluginPanelHome } from './WebUIPluginPanelHome.tsx';
import { webUIPluginPanelIcon } from './webuiPanelIcon.tsx';

const noHiddenPanels: readonly WebUIPanelEntry[] = Object.freeze([]);

export interface WebUIPluginPanelTab {
  /** Tab key for the shared tab bar; never collides with a native tab key. */
  key: string;
  label: string;
  icon: ReactNode;
  closable: boolean;
  animateClose: boolean;
  closeLabel: string;
  content: ReactNode;
}

export interface WebUIPluginPanelTabs {
  /** Every registered plugin in discovery order; blank tabs render this list. */
  panels: readonly WebUIPanelEntry[];
  /** Tabs in bar order, including blank draft tabs and added plugin tabs. */
  tabs: WebUIPluginPanelTab[];
  /** Active tab key, or null when a native page is shown. */
  activeTabKey: string | null;
  /** Create a blank tab that lists the discovered plugins and activate it. */
  openDraft(): void;
  /** Select an existing tab by key; returns false when the key is not a plugin panel tab. */
  selectTab(tabKey: string): boolean;
  /** Close a tab by key; returns false when the key is not a plugin panel tab. */
  closeTab(tabKey: string): boolean;
  selectNativeTab(): void;
}

export interface UseWebUIPluginPanelTabsOptions {
  slotId: string;
  sessionId: string | null;
  /** Discovery result owned by the host; the panel region never mounts the slot itself. */
  panels: readonly WebUIPanelEntry[];
  /**
   * Panels still mounted in the scope but hidden by the instance-visibility client preference.
   * Their tabs are parked, not pruned, so re-enabling the preference restores them.
   */
  hiddenPanels?: readonly WebUIPanelEntry[];
  /** Render one discovered panel body; the host keeps the mount lease and style overrides. */
  renderPanel: (key: string) => ReactNode;
  /** Explicit open request raised by a plugin through `Button.openPanel`. */
  openRequest?: PanelOpenRequest | null;
  consumeOpenRequest?: (id: number) => void;
  /** False when the host is not showing this region; pending requests are left untouched. */
  enabled?: boolean;
}

export function useWebUIPluginPanelTabs({
  slotId,
  sessionId,
  panels,
  hiddenPanels = noHiddenPanels,
  renderPanel,
  openRequest = null,
  consumeOpenRequest,
  enabled = true,
}: UseWebUIPluginPanelTabsOptions): WebUIPluginPanelTabs {
  const { t } = useTranslation();
  const store = useMemo(() => getWebUIPluginPanelTabsStore(slotId), [slotId]);
  const state = useSyncExternalStore(store.subscribe, store.getState, store.getState);
  const availablePanelIds = useMemo(() => panels.map((panel) => panel.panelId), [panels]);
  const hiddenPanelIds = useMemo(() => hiddenPanels.map((panel) => panel.panelId), [hiddenPanels]);
  const sessionKey = sessionId ?? '';
  const sessionRef = useRef(sessionKey);
  const lastKnownPanelIdsRef = useRef<readonly string[]>([]);

  useEffect(() => {
    if (sessionRef.current !== sessionKey) {
      // A conversation switch remounts the slot; contributions that have not rendered yet are
      // not removals, so the next discovery result becomes the new comparison baseline.
      sessionRef.current = sessionKey;
      lastKnownPanelIdsRef.current = [...availablePanelIds, ...hiddenPanelIds];
      return;
    }
    const previousPanelIds = lastKnownPanelIdsRef.current;
    lastKnownPanelIdsRef.current = [...availablePanelIds, ...hiddenPanelIds];
    const known = new Set(availablePanelIds);
    for (const panelId of hiddenPanelIds) known.add(panelId);
    // Only a contribution that left both the visible and the hidden discovery result is truly
    // gone (cleared or unmounted); a preference-hidden panel keeps its tab parked.
    const removedPanelIds = previousPanelIds.filter((panelId) => !known.has(panelId));
    if (removedPanelIds.length > 0) {
      store.update((current) => removeWebUIPluginPanels(current, removedPanelIds));
    }
  }, [availablePanelIds, hiddenPanelIds, sessionKey, store]);

  useEffect(() => {
    // A parked tab must not own the content area while its panel is hidden, even when this hook
    // mounts or the conversation switches with the preference already off. The discovery history
    // only guards pruning against contributions that have not rendered yet; hiding is idempotent.
    if (hiddenPanelIds.length === 0) return;
    store.update((current) => hideWebUIPluginPanels(current, hiddenPanelIds));
  }, [hiddenPanelIds, store]);

  useEffect(() => {
    if (!enabled || !openRequest || !sessionId || openRequest.sessionId !== sessionId) return;
    const panel = panels.find((candidate) => candidate.key === openRequest.key);
    if (!panel) return;
    // An explicit open request adds the tab when it is absent and activates it; no draft involved.
    store.update((current) => chooseWebUIPluginPanel(current, '', panel.panelId));
    consumeOpenRequest?.(openRequest.id);
  }, [enabled, openRequest, panels, sessionId, store, consumeOpenRequest]);

  const pluginPanelIds = useMemo(
    () => state.tabs.flatMap((tabId) => {
      const panelId = webUIPluginPanelIdFromTabId(tabId);
      return panelId ? [panelId] : [];
    }),
    [state.tabs],
  );

  const openDraft = () => {
    store.update(openWebUIPluginPanelDraft);
  };

  const choosePanel = (draftTabId: string, panelId: string) => {
    store.update((current) => chooseWebUIPluginPanel(current, draftTabId, panelId));
  };

  const tabs = useMemo(
    () => state.tabs.flatMap<WebUIPluginPanelTab>((tabId) => {
      const tabKey = webUIPluginPanelTabKey(tabId);
      if (isWebUIPluginPanelDraftTab(tabId)) {
        return [{
          key: tabKey,
          label: t('toolPanel.plugins.draftTab'),
          icon: <PluginIcon className="h-4 w-4 shrink-0" aria-hidden />,
          closable: true,
          animateClose: true,
          closeLabel: t('toolPanel.plugins.closeTab'),
          content: (
            <WebUIPluginPanelHome
              panels={panels}
              openPanelIds={pluginPanelIds}
              onOpenPanel={(panelId) => choosePanel(tabId, panelId)}
            />
          ),
        }];
      }
      const panelId = webUIPluginPanelIdFromTabId(tabId);
      const panel = panelId ? panels.find((candidate) => candidate.panelId === panelId) : undefined;
      if (!panel) return [];
      return [{
        key: tabKey,
        label: panel.title,
        icon: webUIPluginPanelIcon(panel),
        closable: true,
        animateClose: true,
        closeLabel: t('toolPanel.plugins.closeTab'),
        content: renderPanel(panel.key),
      }];
    }),
    [state.tabs, panels, pluginPanelIds, renderPanel, t],
  );

  const activeId = state.activeId;
  const activeTabKey = activeId !== null && tabs.some((tab) => tab.key === webUIPluginPanelTabKey(activeId))
    ? webUIPluginPanelTabKey(activeId)
    : null;

  const selectTab = (tabKey: string): boolean => {
    const tabId = webUIPluginPanelTabIdFromKey(tabKey);
    if (!tabId || !state.tabs.includes(tabId)) return false;
    store.update((current) => selectWebUIPluginPanelTab(current, tabId));
    return true;
  };

  const closeTab = (tabKey: string): boolean => {
    const tabId = webUIPluginPanelTabIdFromKey(tabKey);
    if (!tabId || !state.tabs.includes(tabId)) return false;
    store.update((current) => closeWebUIPluginPanelTab(current, tabId, availablePanelIds));
    return true;
  };

  const selectNativeTab = () => {
    store.update(selectNativeWebUIPanelTab);
  };

  return {
    panels,
    tabs,
    activeTabKey,
    openDraft,
    selectTab,
    closeTab,
    selectNativeTab,
  };
}
