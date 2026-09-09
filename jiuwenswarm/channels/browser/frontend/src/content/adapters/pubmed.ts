/** PubMed/NCBI-specific page extractor. */

export function extractPubmed(): { title: string; text: string } {
  const parts: string[] = [];

  const titleEl = document.querySelector<HTMLElement>(".heading-title, h1.heading-title");
  const title = titleEl?.innerText.trim() ?? document.title;

  // Authors
  const authors = document.querySelector<HTMLElement>(".authors-list");
  if (authors) parts.push(`Authors: ${authors.innerText.trim()}`);

  // Abstract
  const abstract = document.querySelector<HTMLElement>("#abstract, .abstract-content");
  if (abstract) parts.push(`Abstract:\n${abstract.innerText.trim()}`);

  // Full text (PMC articles)
  const fullText = document.querySelector<HTMLElement>("#full-view-heading, .article-page");
  if (fullText) parts.push(fullText.innerText.trim());

  // MeSH terms
  const mesh = document.querySelector<HTMLElement>(".keywords-list");
  if (mesh) parts.push(`Keywords: ${mesh.innerText.trim()}`);

  return { title, text: parts.filter(Boolean).join("\n\n") };
}
