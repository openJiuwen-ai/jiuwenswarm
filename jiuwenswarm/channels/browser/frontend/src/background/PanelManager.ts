/**
 * PanelManager — unified side panel / popup window opener.
 *
 * Chrome 114+ supports chrome.sidePanel. Chromium-based browsers that do not
 * (e.g. 360 Safe Browser, QQ Browser, Sogou) get a popup-window fallback:
 * sidepanel.html opens in a dedicated popup window that is focused instead of
 * re-created when already open.
 *
 * All callers use openPanel(windowId?) — no direct chrome.sidePanel references
 * are needed outside this module.
 */

import { createLogger } from "@shared/logger";

const log = createLogger("panel-mgr");

const PANEL_URL = chrome.runtime.getURL("sidepanel/sidepanel.html");

/** True when the browser natively supports the Side Panel API. */
export const hasSidePanel: boolean =
  typeof chrome !== "undefined" &&
  typeof (chrome as Record<string, unknown>).sidePanel !== "undefined";

/** ID of the popup window we opened as a fallback, if any. */
let _popupWindowId: number | null = null;

/**
 * Open the JiuwenSwarm panel.
 *
 * - If the Side Panel API is available: calls chrome.sidePanel.open().
 * - Otherwise: opens (or focuses) sidepanel.html in a dedicated popup window.
 */
export async function openPanel(windowId?: number): Promise<void> {
  if (hasSidePanel) {
    if (windowId != null) {
      await chrome.sidePanel.open({ windowId });
    }
    return;
  }

  // Popup window fallback -----------------------------------------------

  // If a popup is already open, just focus it.
  if (_popupWindowId != null) {
    try {
      await chrome.windows.update(_popupWindowId, { focused: true });
      return;
    } catch {
      // Window was closed externally; fall through to create a new one.
      _popupWindowId = null;
    }
  }

  const win = await chrome.windows.create({
    url: PANEL_URL,
    type: "popup",
    width: 420,
    height: 700,
    focused: true,
  });

  _popupWindowId = win.id ?? null;
  log.info("opened popup window, id:", _popupWindowId);

  // Clean up the tracked ID when the popup is closed.
  if (_popupWindowId != null) {
    const tracked = _popupWindowId;
    const onRemoved = (removedId: number): void => {
      if (removedId === tracked) {
        _popupWindowId = null;
        chrome.windows.onRemoved.removeListener(onRemoved);
      }
    };
    chrome.windows.onRemoved.addListener(onRemoved);
  }
}
