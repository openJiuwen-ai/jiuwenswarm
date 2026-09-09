/**
 * Twitter / X-specific page extractor.
 *
 * Extracts the main tweet, quoted tweet (if any), and visible thread replies.
 * Twitter is a heavily JS-rendered SPA; this adapter reads from the DOM
 * as rendered, not from the API.
 */

export function extractTwitter(): { title: string; text: string } {
  const parts: string[] = [];

  // Page title (usually "Name on X: '…'")
  const title = document.title.replace(/\s*\|\s*X$/, "").trim();

  // Primary tweet — first article on the page is the main post
  const articles = document.querySelectorAll<HTMLElement>("article[data-testid='tweet']");
  const processed = new Set<string>();

  articles.forEach((article, idx) => {
    // Tweet text
    const textEl = article.querySelector<HTMLElement>("[data-testid='tweetText']");
    const text = textEl?.innerText.trim() ?? "";
    if (!text || processed.has(text)) return;
    processed.add(text);

    // Author
    const authorEl = article.querySelector<HTMLElement>("[data-testid='User-Name']");
    const author = authorEl?.innerText.replace(/\n/g, " ").trim() ?? "";

    // Quoted tweet (nested article)
    const quotedEl = article.querySelector<HTMLElement>("[data-testid='tweet'] [data-testid='tweetText']");
    const quoted = quotedEl && quotedEl !== textEl ? quotedEl.innerText.trim() : "";

    const label = idx === 0 ? "Tweet" : `Reply ${idx}`;
    let block = author ? `${label} by ${author}:\n${text}` : `${label}:\n${text}`;
    if (quoted) block += `\n\n[Quoted tweet]: ${quoted}`;
    parts.push(block);
  });

  if (parts.length === 0) {
    // Fallback for timeline or profile pages
    const bodyText = document.body.innerText.slice(0, 4000);
    return { title, text: bodyText };
  }

  return { title, text: parts.join("\n\n---\n\n") };
}
