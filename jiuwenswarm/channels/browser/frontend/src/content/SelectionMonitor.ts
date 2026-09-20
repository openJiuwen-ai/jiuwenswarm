/**
 * Monitors text selection in the page and responds to background requests
 * for the current selected text.
 */

import { MSG } from "@shared/constants";

let _lastSelection = "";

export function startSelectionMonitor(): void {
  document.addEventListener("selectionchange", () => {
    _lastSelection = window.getSelection()?.toString().trim() ?? "";
  });

  // Background can ask for selection synchronously
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.action === MSG.SELECTION_TEXT) {
      sendResponse({ text: _lastSelection });
    }
    return false; // synchronous
  });
}

export function getLastSelection(): string {
  return _lastSelection;
}
