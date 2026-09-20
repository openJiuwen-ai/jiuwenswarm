/**
 * Popup entry point.
 * Shows connection status, active session, and pinned page count.
 * Provides quick actions: open side panel, open settings.
 */

import { createLogger } from "@shared/logger";
import { MSG } from "@shared/constants";
import { getPinnedPagesBySession } from "@shared/storage";
import { initI18n, applyStaticI18n, t } from "@shared/i18n";

const log = createLogger("popup");

const statusDot = document.getElementById("status-dot")!;
const statusText = document.getElementById("status-text")!;
const sessionName = document.getElementById("session-name")!;
const pinCount = document.getElementById("pin-count")!;
const openPanelBtn = document.getElementById("open-panel-btn")!;
const openOptionsBtn = document.getElementById("open-options-btn")!;

initI18n();
applyStaticI18n();

// Request status from background via one-shot message
chrome.runtime.sendMessage({ action: MSG.GET_STATUS }, async (resp) => {
  if (!resp) {
    statusText.textContent = t("popup.serverNotReachable");
    return;
  }
  const connected: boolean = resp.connected;
  statusDot.classList.toggle("connected", connected);
  statusText.textContent = connected ? t("popup.connected") : t("popup.notConnected");

  const activeId: string | null = resp.activeSessionId;
  if (activeId) {
    sessionName.textContent = resp.activeSessionTitle ?? "Session";

    const pages = await getPinnedPagesBySession(activeId);
    pinCount.textContent = String(pages.length);
  } else {
    sessionName.textContent = t("popup.none");
  }
});

openPanelBtn.addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  chrome.runtime.sendMessage({ action: MSG.OPEN_PANEL, windowId: tab?.windowId });
  window.close();
});

openOptionsBtn.addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
  window.close();
});

log.debug("popup loaded");
