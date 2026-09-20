/**
 * Page content extractor.
 *
 * Uses Readability.js as the baseline for article-like pages, with
 * specialised adapters for 9 structured source types.
 *
 * Truncation strategy: paragraph-boundary chunking with head+tail preservation
 * — keeps the beginning and ending of long documents (endings often contain
 * summaries, conclusions, and key figures that head-only cuts would drop).
 */

import { Readability } from "@mozilla/readability";
import { PageContext } from "@shared/types";
import { MAX_CONTEXT_CHARS } from "@shared/constants";
import { detectPageType } from "./PageTypeDetector";
import { extractGitHub } from "./adapters/github";
import { extractArxiv } from "./adapters/arxiv";
import { extractSec } from "./adapters/sec";
import { extractPubmed } from "./adapters/pubmed";
import { extractWikipedia } from "./adapters/wikipedia";
import { extractYouTube } from "./adapters/youtube";
import { extractTwitter } from "./adapters/twitter";
import { extractHackerNews } from "./adapters/hackernews";

export function extractPageContext(): PageContext {
  const url = location.href;
  const pageType = detectPageType(url);
  const capturedAt = new Date().toISOString();

  let title = document.title;
  let text = "";

  // PDF pages: flag for server-side extraction; return a stub context
  if (pageType === "pdf") {
    return {
      url,
      title,
      pageType,
      capturedAt,
      text: "[PDF — text extraction requires the JiuwenSwarm server-side read_pdf tool]",
      originalLength: 0,
      faviconUrl: getFaviconUrl(),
    };
  }

  try {
    switch (pageType) {
      case "github":       ({ title, text } = extractGitHub());      break;
      case "arxiv":        ({ title, text } = extractArxiv());       break;
      case "sec":          ({ title, text } = extractSec());         break;
      case "pubmed":       ({ title, text } = extractPubmed());      break;
      case "wikipedia":    ({ title, text } = extractWikipedia());   break;
      case "youtube":      ({ title, text } = extractYouTube());     break;
      case "twitter":      ({ title, text } = extractTwitter());     break;
      case "hackernews":   ({ title, text } = extractHackerNews());  break;
      default:             ({ title, text } = extractReadability(title));
    }
  } catch {
    try {
      ({ title, text } = extractReadability(title));
    } catch {
      text = document.body?.innerText?.slice(0, 5000) ?? "";
    }
  }

  const originalLength = text.length;
  // Fall back to the visible page text when no adapter/Readability content was
  // found (e.g. dashboards, JS-heavy apps, or pages with no article body), so
  // the agent still receives something rather than an empty context.
  if (!text.trim()) {
    text = document.body?.innerText ?? "";
  }
  if (text.length > MAX_CONTEXT_CHARS) {
    text = truncateAtParagraphBoundary(text, MAX_CONTEXT_CHARS);
  }

  return {
    url,
    title,
    pageType,
    capturedAt,
    text,
    originalLength,
    faviconUrl: getFaviconUrl(),
  };
}

/**
 * Truncate text at a paragraph boundary rather than a hard character cut.
 *
 * Strategy: keep the first ~80% of the budget from the head, then append the
 * last ~20% from the tail (conclusions, summaries, key figures often appear at
 * document end). Cut at the nearest double-newline to avoid breaking mid-sentence.
 */
function truncateAtParagraphBoundary(text: string, limit: number): string {
  const HEAD_SHARE = 0.80;
  const headBudget = Math.floor(limit * HEAD_SHARE);
  const tailBudget = limit - headBudget - 40; // 40 chars for the separator

  // Find nearest paragraph break at or before headBudget
  let headEnd = headBudget;
  const headBreak = text.lastIndexOf("\n\n", headBudget);
  if (headBreak > headBudget * 0.5) headEnd = headBreak;

  const head = text.slice(0, headEnd).trimEnd();

  // Find nearest paragraph break at or after (length - tailBudget)
  const tailStart = text.length - tailBudget;
  const tailBreak = text.indexOf("\n\n", tailStart);
  const tail = text.slice(tailBreak > tailStart ? tailBreak : tailStart).trimStart();

  // Avoid tail overlap with head
  if (tailStart <= headEnd) {
    return head + "\n\n[...truncated]";
  }

  return `${head}\n\n[...truncated...]\n\n${tail}`;
}

function extractReadability(fallbackTitle: string): { title: string; text: string } {
  const docClone = document.cloneNode(true) as Document;
  const reader = new Readability(docClone);
  const article = reader.parse();
  return {
    title: article?.title || fallbackTitle,
    text: article?.textContent?.trim() ?? "",
  };
}

function getFaviconUrl(): string | undefined {
  const link =
    document.querySelector<HTMLLinkElement>('link[rel="icon"]') ??
    document.querySelector<HTMLLinkElement>('link[rel="shortcut icon"]');
  if (!link?.href) return undefined;
  try {
    return new URL(link.href, location.href).href;
  } catch {
    return undefined;
  }
}
