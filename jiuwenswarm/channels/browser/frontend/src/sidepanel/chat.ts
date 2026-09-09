/**
 * Pure chat-rendering helpers. These take their DOM/context as parameters so they
 * can be unit-tested and reused without reaching into the side panel's module
 * state. The stateful orchestration (connection, streaming, history persistence)
 * lives in `index.ts`.
 */

import { t } from "@shared/i18n";
import { getPinnedPagesBySession } from "@shared/storage";

export function formatTime(ts: number): string {
  return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function addTurnDivider(chatMessages: HTMLElement, ts: number): void {
  const last = chatMessages.lastElementChild;
  // Only add a divider when there was a prior completed turn in this session.
  if (last && last.className !== "msg-turn-divider") {
    const d = document.createElement("div");
    d.className = "msg-turn-divider";
    d.textContent = formatTime(ts);
    chatMessages.appendChild(d);
  }
}

/** Make a small copy icon button (copies the message text on click). */
export function makeCopyIcon(text: string): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.className = "msg-copy-icon";
  btn.title = t("msg.copy");
  btn.textContent = "⧉";
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    await navigator.clipboard.writeText(text);
    btn.textContent = "✓";
    window.setTimeout(() => {
      btn.textContent = "⧉";
    }, 1200);
  });
  return btn;
}

/** Append a bottom row with the timestamp (left) and the copy icon (right). */
export function addMessageFooter(el: HTMLElement, text: string, tsValue: number): void {
  const footer = document.createElement("div");
  footer.className = "msg-footer";
  const time = document.createElement("span");
  time.className = "msg-ts";
  time.textContent = formatTime(tsValue);
  footer.appendChild(time);
  footer.appendChild(makeCopyIcon(text));
  el.appendChild(footer);
}

/** Append the "Sources" chips (pinned pages) to an assistant message. */
export function appendSources(el: HTMLElement, sessionId: string | null): void {
  if (!sessionId) return;
  getPinnedPagesBySession(sessionId).then((pages) => {
    if (pages.length === 0) return;
    const wrap = document.createElement("div");
    wrap.className = "msg-sources";
    const label = document.createElement("span");
    label.style.cssText = "font-size:10px;color:var(--text-dim);align-self:center;";
    label.textContent = t("msg.sources");
    wrap.appendChild(label);
    for (const page of pages.slice(0, 8)) {
      const chip = document.createElement("button");
      chip.className = "src-chip";
      chip.textContent = page.context.title || page.context.url;
      chip.title = page.context.url;
      chip.addEventListener("click", () => {
        chrome.tabs.create({ url: page.context.url });
      });
      wrap.appendChild(chip);
    }
    el.appendChild(wrap);
  }).catch(() => {});
}

/** Render a small inline chip when the agent acts on the page (tool visibility). */
export function renderToolStatus(chatMessages: HTMLElement, tool: string): void {
  const labels: Record<string, string> = {
    highlight_text: t("tool.highlight"),
    scroll_to: t("tool.scroll"),
    fill_form: t("tool.fill"),
    take_screenshot: t("tool.screenshot"),
    open_url: t("tool.open"),
    read_page: t("tool.read"),
    pin_page: t("tool.pin"),
    get_selection: t("tool.selection"),
  };
  const text = labels[tool] ?? t("tool.default");
  const chip = document.createElement("div");
  chip.className = "tool-chip";
  chip.textContent = `⚙ ${text}`;
  chatMessages.appendChild(chip);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  // Remove after a short delay so it doesn't accumulate.
  window.setTimeout(() => chip.remove(), 2500);
}

/** Turn raw server errors into plain-language messages with a next step. */
export function humanizeError(raw: string): string {
  const lower = raw.toLowerCase();
  if (lower.includes("websocket") || lower.includes("socket") || lower.includes("handshake")) {
    return t("err.websocket");
  }
  if (lower.includes("extract page context") || lower.includes("extraction")) {
    return t("err.extraction");
  }
  if (lower.includes("maximum") || lower.includes("limit")) {
    return raw;
  }
  if (lower.includes("timeout") || lower.includes("timed out")) {
    return t("err.timeout");
  }
  return raw;
}
