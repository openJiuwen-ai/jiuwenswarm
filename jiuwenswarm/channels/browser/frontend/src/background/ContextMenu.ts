/**
 * Right-click context menu entries for JiuwenSwarm.
 *
 * Menus:
 * - "Ask JiuwenSwarm about selection" — on selected text
 * - "Pin this page to research session" — on page background
 * - "Summarize this page" — on page background
 */

import { createLogger } from "@shared/logger";

const log = createLogger("bg/menu");

const MENU_ASK = "jiuwen_ask_selection";
const MENU_PIN = "jiuwen_pin_page";
const MENU_SUMMARIZE = "jiuwen_summarize";
const MENU_READER = "jiuwen_reader";
const MENU_SEARCH = "jiuwen_search_pinned";

// Dynamic context-menu APIs (onShown / update / refresh) exist in MV3 but
// are not declared in the pinned @types/chrome version. Declare the slice we use.
type DynamicMenuApi = {
  onShown: {
    addListener(
      cb: (
        info: chrome.contextMenus.OnClickData,
        tab?: chrome.tabs.Tab
      ) => void
    ): void;
  };
  update(id: string, props: { visible?: boolean; title?: string }): Promise<void>;
  refresh(): Promise<void>;
};
const dynMenu = chrome.contextMenus as unknown as DynamicMenuApi;

export type ContextMenuHandler = (
  action: "ask" | "toggle_pin" | "summarize" | "reader" | "search_selection",
  info: chrome.contextMenus.OnClickData,
  tab: chrome.tabs.Tab | undefined
) => void;

export class ContextMenu {
  constructor(
    private readonly _onAction: ContextMenuHandler,
    private readonly _isUrlPinned: (url: string) => Promise<boolean>
  ) {}

  setup(): void {
    // Remove stale items from a previous SW instantiation
    chrome.contextMenus.removeAll(() => {
      chrome.contextMenus.create({
        id: MENU_ASK,
        title: "Ask JiuwenSwarm about \"%s\"",
        contexts: ["selection"],
      });
      chrome.contextMenus.create({
        id: MENU_SEARCH,
        title: "Search pinned pages for \"%s\"",
        contexts: ["selection"],
      });
      chrome.contextMenus.create({
        id: MENU_SUMMARIZE,
        title: "Summarize this page",
        contexts: ["page"],
      });
      chrome.contextMenus.create({
        id: MENU_READER,
        title: "Agent's view of this page",
        contexts: ["page"],
      });
      chrome.contextMenus.create({
        id: "jiuwen_sep_page",
        type: "separator",
        contexts: ["page"],
      });
      chrome.contextMenus.create({
        id: MENU_PIN,
        title: "Pin this page",
        contexts: ["page"],
      });
      log.info("context menus registered");
    });

    chrome.contextMenus.onClicked.addListener(this._onClick.bind(this));

    // Show "Pin this page" or "Unpin this page" (single toggle item), depending
    // on whether the current page is already pinned in the active session.
    // `onShown`/`refresh` are not available on all Chromium builds, so guard it:
    // if unsupported, the item just stays as the static "Pin this page" title
    // (pin/unpin still works via the click handler).
    if (dynMenu.onShown) {
      dynMenu.onShown.addListener(async (info) => {
        const pageUrl = await this._resolveUrl(info.pageUrl);
        let pinned = false;
        if (pageUrl) {
          try {
            pinned = await this._isUrlPinned(pageUrl);
          } catch {
            pinned = false;
          }
        }
        try {
          await dynMenu.update(MENU_PIN, {
            title: pinned ? "Unpin this page" : "Pin this page",
          });
        } catch {
          /* update may fail if the menu is closing — ignore */
        }
        dynMenu.refresh();
      });
    }
  }

  /** Fall back to the active tab's URL when onShown gives us no page URL. */
  private async _resolveUrl(hint?: string): Promise<string> {
    if (hint) return hint;
    try {
      const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      return tab?.url || "";
    } catch {
      return "";
    }
  }

  private _onClick(
    info: chrome.contextMenus.OnClickData,
    tab?: chrome.tabs.Tab
  ): void {
    switch (info.menuItemId) {
      case MENU_ASK:
        this._onAction("ask", info, tab);
        break;
      case MENU_PIN:
        this._onAction("toggle_pin", info, tab);
        break;
      case MENU_SUMMARIZE:
        this._onAction("summarize", info, tab);
        break;
      case MENU_READER:
        this._onAction("reader", info, tab);
        break;
      case MENU_SEARCH:
        this._onAction("search_selection", info, tab);
        break;
      default:
        log.warn("unknown menu item", info.menuItemId);
    }
  }
}
