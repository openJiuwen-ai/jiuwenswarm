/**
 * Full-text search across all pinned pages.
 */

import { t } from "@shared/i18n";
import { loadPinnedPages } from "@shared/storage";

const searchEl = document.getElementById("search")!;
const searchInput = document.getElementById("search-input") as HTMLInputElement;
const searchResults = document.getElementById("search-results")!;
const searchClose = document.getElementById("search-close")!;

export function openSearch(initialQuery?: string): void {
  searchInput.value = initialQuery ?? "";
  searchResults.innerHTML = "";
  searchEl.classList.add("open");
  searchInput.focus();
  if (initialQuery) void runSearch();
}

export function closeSearch(): void {
  searchEl.classList.remove("open");
}

searchClose.addEventListener("click", closeSearch);

async function runSearch(): Promise<void> {
  const q = searchInput.value.trim().toLowerCase();
  if (!q) {
    searchResults.innerHTML = "";
    return;
  }
  const results: { title: string; url: string; snippet: string }[] = [];

  const pages = await loadPinnedPages();
  for (const p of pages) {
    const hay = `${p.context.title} ${p.context.url} ${p.context.text}`.toLowerCase();
    if (hay.includes(q)) {
      const idx = p.context.text.toLowerCase().indexOf(q);
      const start = Math.max(0, idx - 40);
      const snippet = p.context.text.slice(start, start + 120) + (idx < 0 ? "" : "…");
      results.push({ title: p.context.title || p.context.url, url: p.context.url, snippet });
    }
  }

  searchResults.innerHTML = "";
  if (results.length === 0) {
    const empty = document.createElement("div");
    empty.className = "sr-item";
    empty.textContent = t("search.noResults");
    empty.style.cssText = "color:var(--text-dim);";
    searchResults.appendChild(empty);
    return;
  }
  for (const r of results.slice(0, 20)) {
    const item = document.createElement("div");
    item.className = "sr-item";
    const title = document.createElement("div");
    title.className = "sr-title";
    title.textContent = r.title;
    const snippet = document.createElement("div");
    snippet.className = "sr-snippet";
    snippet.textContent = r.snippet;
    item.appendChild(title);
    item.appendChild(snippet);
    item.addEventListener("click", () => {
      if (r.url) chrome.tabs.create({ url: r.url });
    });
    searchResults.appendChild(item);
  }
}

let _searchTimer: number | null = null;
searchInput.addEventListener("input", () => {
  if (_searchTimer != null) window.clearTimeout(_searchTimer);
  _searchTimer = window.setTimeout(() => void runSearch(), 250);
});
