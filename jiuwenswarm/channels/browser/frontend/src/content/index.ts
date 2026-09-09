/**
 * JiuwenSwarm content script — injected into every page.
 *
 * Responsibilities:
 * 1. Extract page context (Readability + adapters) on load
 * 2. Push context to background service worker
 * 3. Listen for highlight / fill-form commands from background
 * 4. Monitor text selection for "ask about selection" commands
 */

import { createLogger } from "@shared/logger";
import { MSG } from "@shared/constants";
import { extractPageContext } from "./Extractor";
import { startSelectionMonitor } from "./SelectionMonitor";
import { startAnnotator } from "./Annotator";
import { startFormAssist } from "./FormAssist";

const log = createLogger("content");

// Start monitors immediately — these are lightweight listener registrations
startSelectionMonitor();
startAnnotator();
startFormAssist();

// Message listener: answers context-extraction requests and the scroll_to tool.
// chrome.tabs.sendMessage needs a response for the request to not reject.
chrome.runtime.onMessage.addListener((msg: { action: string; selector?: string }, _sender, sendResponse) => {
  if (msg.action === MSG.SCROLL_TO && msg.selector) {
    try {
      const el = document.querySelector(msg.selector);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
    } catch {
      // Invalid selector — ignore
    }
    return false;
  }
  if (msg.action === MSG.PAGE_CONTEXT) {
    try {
      sendResponse(extractPageContext());
    } catch (e) {
      log.warn("context extraction failed", e);
      sendResponse(null);
    }
    return false;
  }
  return false;
});

// Extract and push context once the DOM is ready
function pushContext(): void {
  try {
    const context = extractPageContext();
    chrome.runtime.sendMessage({ action: MSG.PAGE_CONTEXT, context });
    log.debug("context pushed", context.pageType, context.url);
  } catch (e) {
    log.warn("context extraction failed", e);
  }
}

if (document.readyState === "complete" || document.readyState === "interactive") {
  pushContext();
} else {
  document.addEventListener("DOMContentLoaded", pushContext, { once: true });
}

// Re-push on SPA navigation (history API)
let _lastUrl = location.href;
const _observer = new MutationObserver(() => {
  if (location.href !== _lastUrl) {
    _lastUrl = location.href;
    // Slight delay to let the new page content render
    setTimeout(pushContext, 500);
  }
});
_observer.observe(document.body, { childList: true, subtree: true });
