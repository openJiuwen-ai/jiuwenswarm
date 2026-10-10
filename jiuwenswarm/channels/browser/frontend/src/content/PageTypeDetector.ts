/**
 * Detects the semantic type of the current page from URL and DOM signals.
 * Used to select the right extraction adapter.
 */

export type PageType =
  | "article"
  | "github"
  | "arxiv"
  | "sec"
  | "pubmed"
  | "wikipedia"
  | "youtube"
  | "twitter"
  | "hackernews"
  | "pdf"
  | "generic";

export function detectPageType(url: string): PageType {
  try {
    const { hostname, pathname } = new URL(url);

    if (hostname === "github.com" || hostname.endsWith(".github.com")) {
      return "github";
    }
    if (hostname === "arxiv.org" || hostname === "ar5iv.org") {
      return "arxiv";
    }
    if (hostname.includes("sec.gov")) {
      return "sec";
    }
    if (hostname === "pubmed.ncbi.nlm.nih.gov" || hostname.includes("ncbi.nlm.nih.gov")) {
      return "pubmed";
    }
    if (hostname.includes("wikipedia.org")) {
      return "wikipedia";
    }
    if (hostname === "www.youtube.com" || hostname === "youtu.be" || hostname === "youtube.com") {
      return "youtube";
    }
    if (
      hostname === "twitter.com" ||
      hostname === "x.com" ||
      hostname === "mobile.twitter.com"
    ) {
      return "twitter";
    }
    if (hostname === "news.ycombinator.com") {
      return "hackernews";
    }
    if (
      pathname.endsWith(".pdf") ||
      document.contentType === "application/pdf" ||
      document.querySelector("embed[type='application/pdf']") !== null
    ) {
      return "pdf";
    }
    // Check for article-like pages via Open Graph
    const ogType = document
      .querySelector('meta[property="og:type"]')
      ?.getAttribute("content");
    if (ogType === "article") return "article";

    return "generic";
  } catch {
    return "generic";
  }
}
