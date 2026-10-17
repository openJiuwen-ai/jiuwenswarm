/**
 * Agent's view (reader) — opens the active page's extracted text in a clean view,
 * showing exactly what the agent reads.
 */

import { MSG } from "@shared/constants";
import { t } from "@shared/i18n";

const readerEl = document.getElementById("reader")!;
const readerBack = document.getElementById("reader-back")!;
const readerContent = document.getElementById("reader-content")!;

export async function openReader(): Promise<void> {
  readerEl.classList.add("open");
  readerContent.innerHTML = `<div id="reader-loading">${t("reader.loading")}</div>`;
  try {
    const resp = await chrome.runtime.sendMessage({ action: MSG.GET_ACTIVE_CONTEXT });
    const ctx = resp?.context;
    if (!ctx) {
      readerContent.innerHTML = `<div id="reader-error">${t("reader.error")}</div>`;
      return;
    }
    const article = document.createElement("article");
    article.id = "reader-article";
    const h1 = document.createElement("h1");
    h1.textContent = ctx.title || ctx.url;
    const meta = document.createElement("div");
    meta.className = "reader-meta";
    meta.textContent = `${ctx.url} · ${ctx.pageType}`;
    const note = document.createElement("div");
    note.className = "reader-note";
    note.textContent = t("reader.note");
    const body = document.createElement("div");
    body.className = "reader-body";
    body.textContent = ctx.text || "—";
    article.appendChild(h1);
    article.appendChild(meta);
    article.appendChild(note);
    article.appendChild(body);
    readerContent.innerHTML = "";
    readerContent.appendChild(article);
  } catch {
    readerContent.innerHTML = `<div id="reader-error">${t("reader.error")}</div>`;
  }
}

export function closeReader(): void {
  readerEl.classList.remove("open");
}

readerBack.addEventListener("click", closeReader);
