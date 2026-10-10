/**
 * In-memory cache of PageContext objects per tab.
 *
 * Content scripts push context here whenever a page finishes loading or
 * when the user triggers an extraction. The cache is consulted when:
 * - a page is pinned (adds context to PinnedPage)
 * - the user asks about the current tab
 * - context is aggregated for a research session
 *
 * Note: service workers can be suspended; the cache is intentionally
 * ephemeral. Pinned contexts are persisted separately via storage.ts.
 */

import { PageContext } from "@shared/types";

export class ContextCache {
  private _cache: Map<number, PageContext> = new Map();

  set(tabId: number, ctx: PageContext): void {
    this._cache.set(tabId, ctx);
  }

  get(tabId: number): PageContext | undefined {
    return this._cache.get(tabId);
  }

  delete(tabId: number): void {
    this._cache.delete(tabId);
  }

  /** Aggregate contexts for a list of tab IDs into a single text block. */
  aggregate(tabIds: number[], maxChars: number): string {
    const parts: string[] = [];
    let total = 0;
    for (const id of tabIds) {
      const ctx = this._cache.get(id);
      if (!ctx) continue;
      const header = `### ${ctx.title}\nURL: ${ctx.url}\nType: ${ctx.pageType}\nCaptured: ${ctx.capturedAt}\n\n`;
      const block = header + ctx.text;
      if (total + block.length > maxChars) {
        const remaining = maxChars - total;
        if (remaining > 200) {
          parts.push(block.slice(0, remaining) + "\n[...truncated]");
          total = maxChars;
        }
        break;
      }
      parts.push(block);
      total += block.length;
    }
    return parts.join("\n\n---\n\n");
  }

  get size(): number {
    return this._cache.size;
  }
}
