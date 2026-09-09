/** arXiv-specific page extractor. Handles both arxiv.org and ar5iv.org. */

export function extractArxiv(): { title: string; text: string } {
  const parts: string[] = [];

  // Title
  const titleEl =
    document.querySelector<HTMLElement>(".title.mathjax") ??
    document.querySelector<HTMLElement>("h1.title");
  const title = titleEl?.innerText.replace(/^Title:\s*/i, "").trim() ?? document.title;

  // Authors
  const authors = document.querySelector<HTMLElement>(".authors");
  if (authors) parts.push(`Authors: ${authors.innerText.trim()}`);

  // Abstract
  const abstract =
    document.querySelector<HTMLElement>(".abstract.mathjax") ??
    document.querySelector<HTMLElement>("#abstract");
  if (abstract) {
    parts.push(`Abstract:\n${abstract.innerText.replace(/^Abstract:\s*/i, "").trim()}`);
  }

  // Full paper body (ar5iv.org has structured HTML)
  const body =
    document.querySelector<HTMLElement>("#content") ??
    document.querySelector<HTMLElement>("article");
  if (body) {
    parts.push(body.innerText.trim());
  }

  return { title, text: parts.filter(Boolean).join("\n\n") };
}
