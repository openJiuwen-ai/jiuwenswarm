/**
 * Highlights text passages in the page on behalf of the agent.
 *
 * Transient (.jiuwen-highlight) — applied by the agent during a session and
 * cleared on CLEAR_HIGHLIGHTS or when the agent moves on.
 *
 * Uses <mark> element injection (Chrome 105 Custom Highlight API not needed).
 */

import { MSG } from "@shared/constants";
import { HighlightMsg } from "@shared/types";

const TRANSIENT_CLASS = "jiuwen-highlight";
const STYLE_ID        = "jiuwen-highlight-style";

export function startAnnotator(): void {
  _injectStyles();

  chrome.runtime.onMessage.addListener((msg: HighlightMsg | { action: string }) => {
    const action = (msg as { action: string }).action;

    if (action === MSG.HIGHLIGHT_TEXT) {
      const m = msg as HighlightMsg;
      _applyTransient(m.text, m.color);
    } else if (action === MSG.CLEAR_HIGHLIGHTS) {
      _clearTransient();
    }

    return false;
  });
}

function _applyTransient(searchText: string, _color?: string): void {
  if (!searchText) return;
  _clearTransient();

  const walker = document.createTreeWalker(
    document.body,
    NodeFilter.SHOW_TEXT,
    null
  );

  let node: Node | null;
  while ((node = walker.nextNode())) {
    const text = (node as Text).textContent ?? "";
    const idx = text.indexOf(searchText);
    if (idx === -1) continue;
    const mark = document.createElement("mark");
    mark.className = TRANSIENT_CLASS;
    mark.textContent = searchText;
    const parent = node.parentNode!;
    parent.insertBefore(document.createTextNode(text.slice(0, idx)), node);
    parent.insertBefore(mark, node);
    parent.insertBefore(document.createTextNode(text.slice(idx + searchText.length)), node);
    parent.removeChild(node);
    break; // first occurrence only
  }
}

function _clearTransient(): void {
  document.querySelectorAll(`.${TRANSIENT_CLASS}`).forEach((el) => {
    el.replaceWith(el.textContent ?? "");
  });
}

function _injectStyles(): void {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
    .${TRANSIENT_CLASS} {
      background-color: #ffe08a;
      border-radius: 2px;
      padding: 0 1px;
    }
  `;
  document.head.appendChild(style);
}
