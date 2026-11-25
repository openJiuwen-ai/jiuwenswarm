/**
 * Pure state rules for the right-workspace plugin tabs.
 *
 * A plugin registers through the existing `conversation.panel.right` slot. The host keeps an
 * ordered tab list with two kinds of entries:
 *
 * - `draft:<seq>` — a plugin-agnostic blank tab that shows the discovered plugins; the plus
 *   button creates one per click. Choosing a plugin converts that same tab in place into
 *   `plugin:<panelId>` and activates it.
 * - `plugin:<panelId>` — a tab owned by one discovered plugin; it can be closed like a draft.
 *
 * The state is host memory: conversation switches and panel unmounts keep it through the scoped
 * store, a page reload starts from an empty list because plugins are discovered again.
 */

export interface WebUIPluginPanelTabsState {
  /** Ordered tab ids: `plugin:<panelId>` or `draft:<seq>`. */
  tabs: readonly string[];
  /** Active tab id, or null when a native page owns the content area. */
  activeId: string | null;
  /** Monotonic counter that gives every draft tab a stable identity. */
  draftSeq: number;
}

const PLUGIN_TAB_PREFIX = 'plugin:';
const DRAFT_TAB_PREFIX = 'draft:';
const TAB_KEY_PREFIX = 'webui:';

export const EMPTY_WEBUI_PLUGIN_PANEL_TABS: WebUIPluginPanelTabsState = Object.freeze({
  tabs: Object.freeze([]) as readonly string[],
  activeId: null,
  draftSeq: 0,
});

/** State id of the plugin tab for one discovered panel. */
export function webUIPluginPanelTabId(panelId: string): string {
  return `${PLUGIN_TAB_PREFIX}${panelId}`;
}

/** State id of the blank tab created by one plus click. */
export function webUIPluginPanelDraftTabId(draftSeq: number): string {
  return `${DRAFT_TAB_PREFIX}${draftSeq}`;
}

export function isWebUIPluginPanelDraftTab(tabId: string): boolean {
  return tabId.startsWith(DRAFT_TAB_PREFIX);
}

/** Extract the panel id from a plugin tab id; draft ids return null. */
export function webUIPluginPanelIdFromTabId(tabId: string): string | null {
  return tabId.startsWith(PLUGIN_TAB_PREFIX) ? tabId.slice(PLUGIN_TAB_PREFIX.length) : null;
}

/** Tab key used by the shared tab bar; it never collides with a native tab key. */
export function webUIPluginPanelTabKey(tabId: string): string {
  return `${TAB_KEY_PREFIX}${tabId}`;
}

export function webUIPluginPanelTabIdFromKey(tabKey: string): string | null {
  return tabKey.startsWith(TAB_KEY_PREFIX) ? tabKey.slice(TAB_KEY_PREFIX.length) : null;
}

/** Add a new blank tab and activate it; every call creates a distinct draft. */
export function openWebUIPluginPanelDraft(state: WebUIPluginPanelTabsState): WebUIPluginPanelTabsState {
  const draftSeq = state.draftSeq + 1;
  const tabId = webUIPluginPanelDraftTabId(draftSeq);
  return { tabs: [...state.tabs, tabId], activeId: tabId, draftSeq };
}

/**
 * Turn one draft tab into the chosen plugin tab, keeping the tab position.
 *
 * An already opened plugin wins: the draft is removed and the existing tab is activated so the
 * same plugin instance never appears twice. A missing draft (for example a closed tab) appends a
 * new plugin tab, so the choice is not lost.
 */
export function chooseWebUIPluginPanel(
  state: WebUIPluginPanelTabsState,
  draftTabId: string,
  panelId: string,
): WebUIPluginPanelTabsState {
  if (!panelId) return state;
  const pluginTabId = webUIPluginPanelTabId(panelId);
  if (state.tabs.includes(pluginTabId)) {
    const tabs = state.tabs.includes(draftTabId) ? state.tabs.filter((tabId) => tabId !== draftTabId) : state.tabs;
    if (tabs === state.tabs && state.activeId === pluginTabId) return state;
    return { ...state, tabs, activeId: pluginTabId };
  }
  const draftIndex = state.tabs.indexOf(draftTabId);
  if (draftIndex < 0) {
    return { ...state, tabs: [...state.tabs, pluginTabId], activeId: pluginTabId };
  }
  const tabs = [...state.tabs];
  tabs.splice(draftIndex, 1, pluginTabId);
  return { ...state, tabs, activeId: pluginTabId };
}

/** Select an existing tab; unknown ids are ignored. */
export function selectWebUIPluginPanelTab(
  state: WebUIPluginPanelTabsState,
  tabId: string,
): WebUIPluginPanelTabsState {
  if (!state.tabs.includes(tabId) || state.activeId === tabId) return state;
  return { ...state, activeId: tabId };
}

/**
 * Close any draft or plugin tab.
 *
 * Closing the active tab selects the nearest visible tab to its right, then to its left.
 * Closing an inactive tab keeps the current page.
 */
export function closeWebUIPluginPanelTab(
  state: WebUIPluginPanelTabsState,
  tabId: string,
  availablePanelIds: readonly string[],
): WebUIPluginPanelTabsState {
  const closingIndex = state.tabs.indexOf(tabId);
  if (closingIndex < 0) return state;
  const tabs = state.tabs.filter((id) => id !== tabId);
  if (state.activeId !== tabId) return { ...state, tabs };

  const available = new Set(availablePanelIds);
  const isVisible = (id: string) => {
    const panelId = webUIPluginPanelIdFromTabId(id);
    return panelId === null || available.has(panelId);
  };
  const activeId = tabs.slice(closingIndex).find(isVisible)
    ?? tabs.slice(0, closingIndex).reverse().find(isVisible)
    ?? null;
  return { ...state, tabs, activeId };
}

/** A native tab was selected: the plugin panel no longer owns the content area. */
export function selectNativeWebUIPanelTab(state: WebUIPluginPanelTabsState): WebUIPluginPanelTabsState {
  if (state.activeId === null) return state;
  return { ...state, activeId: null };
}

/**
 * Hide plugin tabs whose instance-visibility preference is off instead of deleting them.
 *
 * The tab ids stay in the list so re-enabling the preference brings the tab back in its original
 * position without activation; the tab bar itself only renders tabs backed by the visible
 * discovery result, so a hidden tab does not show. When the hidden tab owns the content area the
 * native page takes over, matching the documented "hidden → native, re-enabled → not re-selected"
 * behaviour.
 */
export function hideWebUIPluginPanels(
  state: WebUIPluginPanelTabsState,
  hiddenPanelIds: readonly string[],
): WebUIPluginPanelTabsState {
  const hiddenTabs = new Set(hiddenPanelIds.map(webUIPluginPanelTabId));
  const activeId = state.activeId !== null && hiddenTabs.has(state.activeId) ? null : state.activeId;
  if (activeId === state.activeId) return state;
  return { ...state, activeId };
}

/**
 * Drop only contributions observed disappearing in the current conversation.
 */
export function removeWebUIPluginPanels(
  state: WebUIPluginPanelTabsState,
  removedPanelIds: readonly string[],
): WebUIPluginPanelTabsState {
  const removedTabs = new Set(removedPanelIds.map(webUIPluginPanelTabId));
  const tabs = state.tabs.filter((tabId) => !removedTabs.has(tabId));
  if (tabs.length === state.tabs.length) return state;

  const activeId = state.activeId !== null && removedTabs.has(state.activeId) ? null : state.activeId;
  return { ...state, tabs, activeId };
}
