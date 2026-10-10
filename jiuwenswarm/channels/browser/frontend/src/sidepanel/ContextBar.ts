/**
 * Context bar — shows pinned page chips with extraction quality signals.
 *
 * Each chip shows:
 * - Favicon + truncated page title
 * - Tooltip: full URL + extracted character count + page type
 * - Warning icon (⚠) if extraction returned < 200 characters
 * - PDF badge if page is a PDF (requires server-side extraction)
 * - Retry button for low-quality extractions
 * - Move (◀ ▶) buttons to reorder context priority
 * - Unpin (×) button
 * - Click the chip to expand a preview of the extracted text
 */

import { PinnedPage } from "@shared/types";
import { t } from "@shared/i18n";

const LOW_EXTRACTION_THRESHOLD = 200; // characters
const PREVIEW_CHARS = 1200;

export class ContextBar {
  private _chipsEl: HTMLElement;
  private _onUnpin: (page: PinnedPage) => void;
  private _onRetry: (page: PinnedPage) => void;
  private _onMove: (id: string, dir: -1 | 1) => void;
  private _pages: PinnedPage[] = [];

  constructor(
    chipsEl: HTMLElement,
    onUnpin: (page: PinnedPage) => void,
    onRetry: (page: PinnedPage) => void,
    onMove: (id: string, dir: -1 | 1) => void
  ) {
    this._chipsEl = chipsEl;
    this._onUnpin = onUnpin;
    this._onRetry = onRetry;
    this._onMove = onMove;
  }

  update(pinnedPages: PinnedPage[]): void {
    this._pages = pinnedPages;
    this._chipsEl.innerHTML = "";
    for (let i = 0; i < this._pages.length; i++) {
      this._chipsEl.appendChild(this._makeChip(this._pages[i], i));
    }
  }

  addPage(page: PinnedPage): void {
    this._pages = [...this._pages, page];
    this.update(this._pages);
    // Micro-interaction: flash the ring to draw attention to the new chip.
    const chip = this._chipsEl.querySelector(`.pin-chip[data-id="${page.id}"]`);
    if (chip) {
      chip.classList.add("flash");
      window.setTimeout(() => chip.classList.remove("flash"), 700);
    }
  }

  private _makeChip(page: PinnedPage, index: number): HTMLElement {
    const { context } = page;
    const charCount = context.text.length;
    const isLow = charCount < LOW_EXTRACTION_THRESHOLD;
    const isPdf = context.pageType === "pdf";

    const chip = document.createElement("div");
    chip.className = "pin-chip" + (isLow ? " pin-chip--warn" : "");
    chip.dataset.id = page.id;
    chip.tabIndex = 0;
    chip.title = `${context.url}\nType: ${context.pageType} · ${charCount} chars\nPinned: ${new Date(page.pinnedAt).toLocaleString()}\n${t("chip.previewHint")}`;

    // Favicon
    const favicon = document.createElement("img");
    favicon.src = context.faviconUrl ?? chrome.runtime.getURL("icons/icon-16.png");
    favicon.width = 12;
    favicon.height = 12;
    favicon.style.cssText = "border-radius:2px;flex-shrink:0;";
    favicon.onerror = () => { favicon.style.display = "none"; };
    chip.appendChild(favicon);

    // PDF badge
    if (isPdf) {
      const badge = document.createElement("span");
      badge.textContent = "PDF";
      badge.style.cssText = "font-size:9px;background:#7c6af7;color:#fff;border-radius:3px;padding:0 3px;flex-shrink:0;";
      chip.appendChild(badge);
    }

    // Warning icon for low extraction
    if (isLow && !isPdf) {
      const warn = document.createElement("span");
      warn.textContent = "⚠";
      warn.style.cssText = "color:#f38ba8;font-size:11px;flex-shrink:0;";
      warn.title = charCount === 0
        ? "Extraction failed — page may be blocked or JS-only"
        : `Low extraction: only ${charCount} characters`;
      chip.appendChild(warn);
    }

    // Title label
    const label = document.createElement("span");
    label.textContent = context.title || context.url;
    label.style.cssText = "max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
    chip.appendChild(label);

    // Action buttons
    const actions = document.createElement("span");
    actions.className = "chip-actions";

    // Move left / right (reorder context priority)
    if (index > 0) {
      actions.appendChild(this._makeAction("◀", t("chip.moveEarlier"), () => {
        chip.classList.remove("open");
        this._onMove(page.id, -1);
      }));
    }
    if (index < this._pages.length - 1) {
      actions.appendChild(this._makeAction("▶", t("chip.moveLater"), () => {
        chip.classList.remove("open");
        this._onMove(page.id, 1);
      }));
    }

    // Retry button (only for low-quality or PDF)
    if (isLow || isPdf) {
      actions.appendChild(this._makeAction("↻", isPdf ? t("chip.rePdf") : t("chip.retry"), () => {
        this._onRetry(page);
      }));
    }

    // Unpin button
    actions.appendChild(this._makeAction("×", t("chip.unpin"), () => {
      chip.classList.remove("open");
      this._onUnpin(page);
    }));

    chip.appendChild(actions);

    // Extracted-text preview (toggled by clicking the chip)
    const preview = document.createElement("div");
    preview.className = "pin-preview";
    const text = context.text.trim();
    preview.textContent = text.length > PREVIEW_CHARS
      ? `${text.slice(0, PREVIEW_CHARS)}…\n\n[...truncated preview…]`
      : (text || "No extractable text.");
    chip.appendChild(preview);

    const toggle = () => chip.classList.toggle("open");
    chip.addEventListener("click", (ev) => {
      if ((ev.target as HTMLElement).closest("button")) return;
      toggle();
    });
    chip.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        toggle();
      }
    });

    return chip;
  }

  private _makeAction(
    glyph: string,
    title: string,
    onClick: () => void
  ): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.textContent = glyph;
    btn.title = title;
    btn.style.cssText = "background:none;border:none;color:var(--text-dim,#7f849c);cursor:pointer;font-size:11px;padding:0 2px;";
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      onClick();
    });
    return btn;
  }
}
