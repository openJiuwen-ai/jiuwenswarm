/**
 * ToolDispatcher — executes browser-native agent tools.
 *
 * When the server-side agent needs to interact with the browser (read a page,
 * highlight text, take a screenshot, etc.) it sends a `tool_call` envelope.
 * ToolDispatcher receives that envelope, runs the appropriate browser API or
 * content-script command, and sends a `tool_result` envelope back.
 *
 * Supported tools:
 *   get_selection    — current text selection in the active tab
 *   highlight_text   — highlight a passage in the active tab
 *   fill_form        — fill form fields by label or id
 *   scroll_to        — scroll the active tab to a CSS selector
 *   take_screenshot  — capture the visible tab area as base64 PNG
 *   open_url         — open a URL in a new tab
 *   read_page        — return extracted text of a tab (by URL or active tab)
 *   pin_page         — pin a tab to the active research session
 */

import { createLogger } from "@shared/logger";
import { InboundEnvelope, GW_METHOD } from "@shared/protocol";
import { MSG } from "@shared/constants";
import { addPinnedPage } from "@shared/storage";
import { PinnedPage } from "@shared/types";
import { nanoid } from "nanoid";

import { WsClient } from "./WsClient";
import { ContextCache } from "./ContextCache";
import { TabWatcher } from "./TabWatcher";
import { SessionManager } from "./SessionManager";

const log = createLogger("bg/tools");

export class ToolDispatcher {
  /**
   * Optional callback fired when a tool starts executing. Used by the background
   * to surface agent activity to the side panel (tool-action visibility).
   */
  onTool: ((tool: string) => void) | null = null;

  constructor(
    private readonly _client: WsClient,
    private readonly _cache: ContextCache,
    private readonly _tabWatcher: TabWatcher,
    private readonly _sessionMgr: SessionManager,
  ) {}

  async dispatch(envelope: InboundEnvelope, sessionId?: string): Promise<void> {
    const { tool, args, call_id } = envelope.payload as {
      tool: string;
      args: Record<string, unknown>;
      call_id: string;
    };
    log.debug("tool_call", tool, call_id);
    this.onTool?.(tool);

    let result: unknown;
    try {
      switch (tool) {
        case "get_selection":
          result = await this._getSelection();
          break;
        case "highlight_text":
          result = await this._highlightText(
            args.text as string,
            args.color as string | undefined,
          );
          break;
        case "fill_form":
          result = await this._fillForm(args.fields as Record<string, string>);
          break;
        case "scroll_to":
          result = await this._scrollTo(args.selector as string);
          break;
        case "take_screenshot":
          result = await this._takeScreenshot();
          break;
        case "open_url":
          result = await this._openUrl(args.url as string);
          break;
        case "read_page":
          result = await this._readPage(args.url as string | undefined);
          break;
        case "pin_page":
          result = await this._pinPage(
            args.tabId as number | undefined,
            sessionId,
          );
          break;
        default:
          result = { error: `Unknown browser tool: ${tool}` };
      }
    } catch (e) {
      log.warn("tool error", tool, e);
      result = { error: String(e) };
    }

    this._client.send(GW_METHOD.CHAT_TOOL_RESULT, {
      call_id,
      result,
      ...(sessionId ? { session_id: sessionId } : {}),
    });
    log.debug("tool_result sent", tool, call_id);
  }

  // ---------------------------------------------------------------------------
  // Tool implementations
  // ---------------------------------------------------------------------------

  private async _getActiveTab(): Promise<chrome.tabs.Tab | null> {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    return tab ?? null;
  }

  /** Returns the current text selection in the active tab, or an empty string. */
  private async _getSelection(): Promise<string> {
    const tab = await this._getActiveTab();
    if (!tab?.id) return "";
    try {
      const resp = await chrome.tabs.sendMessage(tab.id, {
        action: MSG.SELECTION_TEXT,
      });
      return (resp as { text: string })?.text ?? "";
    } catch {
      return "";
    }
  }

  /** Highlights a text passage in the active tab via the Annotator content script. */
  private async _highlightText(
    text: string,
    color?: string,
  ): Promise<{ ok: boolean }> {
    const tab = await this._getActiveTab();
    if (!tab?.id) return { ok: false };
    try {
      await chrome.tabs.sendMessage(tab.id, {
        action: MSG.HIGHLIGHT_TEXT,
        text,
        color,
      });
      return { ok: true };
    } catch {
      return { ok: false };
    }
  }

  /** Fills form fields in the active tab via the FormAssist content script. */
  private async _fillForm(
    fields: Record<string, string>,
  ): Promise<{ ok: boolean }> {
    const tab = await this._getActiveTab();
    if (!tab?.id) return { ok: false };
    try {
      await chrome.tabs.sendMessage(tab.id, { action: MSG.FILL_FORM, fields });
      return { ok: true };
    } catch {
      return { ok: false };
    }
  }

  /** Scrolls the active tab to the first element matching a CSS selector. */
  private async _scrollTo(selector: string): Promise<{ ok: boolean }> {
    const tab = await this._getActiveTab();
    if (!tab?.id) return { ok: false };
    try {
      await chrome.tabs.sendMessage(tab.id, {
        action: MSG.SCROLL_TO,
        selector,
      });
      return { ok: true };
    } catch {
      return { ok: false };
    }
  }

  /** Captures the visible area of the active tab as a base64 PNG data URL. */
  private async _takeScreenshot(): Promise<
    { dataUrl: string } | { error: string }
  > {
    const tab = await this._getActiveTab();
    if (!tab?.windowId) return { error: "No active tab" };
    try {
      const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
        format: "png",
      });
      return { dataUrl };
    } catch (e) {
      return { error: String(e) };
    }
  }

  /** Opens a URL in a new tab and returns the new tab's id. */
  private async _openUrl(
    url: string,
  ): Promise<{ tabId: number; url: string }> {
    const tab = await chrome.tabs.create({ url });
    return { tabId: tab.id!, url: tab.url ?? url };
  }

  /**
   * Returns the extracted text of a tab.
   * If `url` is provided, finds the first open tab matching that URL.
   * If omitted, uses the currently active tab.
   * Returns an error if no matching tab is open — call open_url first.
   */
  private async _readPage(url?: string): Promise<
    | { title: string; url: string; text: string; pageType: string }
    | { error: string }
  > {
    let tabId: number | undefined;

    if (url) {
      const tabs = await chrome.tabs.query({ url });
      if (tabs.length === 0) {
        return { error: `No open tab matches "${url}". Call open_url first.` };
      }
      tabId = tabs[0].id!;
    } else {
      const tab = await this._getActiveTab();
      tabId = tab?.id;
    }

    if (!tabId) return { error: "No tab found" };

    let ctx = this._cache.get(tabId);
    if (!ctx) {
      ctx = (await this._tabWatcher.extractFromTab(tabId)) ?? undefined;
      if (ctx) this._cache.set(tabId, ctx);
    }
    if (!ctx) return { error: "Could not extract page context from tab" };

    return {
      title: ctx.title,
      url: ctx.url,
      text: ctx.text,
      pageType: ctx.pageType,
    };
  }

  /**
   * Pins a tab to the active research session.
   * Uses the active tab if tabId is not specified.
   */
  private async _pinPage(
    tabId: number | undefined,
    sessionId: string | undefined,
  ): Promise<{ ok: boolean; pageId: string } | { error: string }> {
    const activeSessionId = sessionId ?? this._sessionMgr.activeSessionId;
    if (!activeSessionId) return { error: "No active session" };

    let resolvedTabId = tabId;
    if (!resolvedTabId) {
      const tab = await this._getActiveTab();
      resolvedTabId = tab?.id;
    }
    if (!resolvedTabId) return { error: "No tab found" };

    let ctx = this._cache.get(resolvedTabId);
    if (!ctx) {
      ctx = (await this._tabWatcher.extractFromTab(resolvedTabId)) ?? undefined;
      if (ctx) this._cache.set(resolvedTabId, ctx);
    }
    if (!ctx) return { error: "Could not extract page context" };

    const pinned: PinnedPage = {
      id: nanoid(),
      tabId: resolvedTabId,
      sessionId: activeSessionId,
      context: ctx,
      note: "",
      pinnedAt: new Date().toISOString(),
    };
    await addPinnedPage(pinned);
    return { ok: true, pageId: pinned.id };
  }
}
