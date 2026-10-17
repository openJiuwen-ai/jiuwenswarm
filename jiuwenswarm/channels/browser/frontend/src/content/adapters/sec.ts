/** SEC EDGAR-specific page extractor. Handles filings, forms, and search results. */

export function extractSec(): { title: string; text: string } {
  const title = document.title;
  const parts: string[] = [];

  // Filing viewer — primary content
  const formContent = document.querySelector<HTMLElement>("#formContent");
  if (formContent) {
    parts.push(formContent.innerText.trim());
    return { title, text: parts.join("\n\n") };
  }

  // EDGAR filing index
  const filingHeader = document.querySelector<HTMLElement>(".formGrouping");
  if (filingHeader) parts.push(filingHeader.innerText.trim());

  // Generic document body (10-K, 10-Q, 8-K as HTML)
  const docBody = document.querySelector<HTMLElement>("body");
  if (docBody) {
    // Filter out nav/header boilerplate
    const cloned = docBody.cloneNode(true) as HTMLElement;
    cloned.querySelectorAll("nav, header, footer, script, style").forEach((el) => el.remove());
    parts.push(cloned.innerText.slice(0, 80_000).trim());
  }

  return { title, text: parts.filter(Boolean).join("\n\n") };
}
