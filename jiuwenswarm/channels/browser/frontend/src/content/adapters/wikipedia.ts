/** Wikipedia-specific page extractor. Keeps lead section + article body; drops navboxes, references, and external links. */

export function extractWikipedia(): { title: string; text: string } {
  const titleEl = document.querySelector<HTMLElement>("#firstHeading, .mw-first-heading");
  const title = titleEl?.innerText.trim() ?? document.title;

  const parts: string[] = [];

  // Lead section (before first h2)
  const content = document.querySelector<HTMLElement>("#mw-content-text .mw-parser-output");
  if (!content) {
    return { title, text: document.body.innerText.slice(0, 6000) };
  }

  // Clone so we can strip noise without mutating the live page
  const clone = content.cloneNode(true) as HTMLElement;

  // Remove navboxes, references section, external links, categories, infoboxes, hidden elements
  const noiseSelectors = [
    ".navbox", ".reflist", ".references", ".mw-references-wrap",
    ".external", "#mw-navigation", ".toc",
    ".hatnote", ".sidebar", ".infobox",
    "style", "script", ".mw-editsection",
  ];
  noiseSelectors.forEach((sel) => {
    clone.querySelectorAll(sel).forEach((el) => el.remove());
  });

  // Extract section by section, stopping at "See also" / "References" headings
  const stopHeadings = new Set(["see also", "references", "notes", "external links", "further reading"]);
  let stopped = false;

  for (const node of Array.from(clone.childNodes)) {
    if (stopped) break;
    if (node instanceof HTMLElement) {
      if (/^h[2-4]$/i.test(node.tagName)) {
        const heading = node.innerText.trim().toLowerCase();
        if (stopHeadings.has(heading)) {
          stopped = true;
          break;
        }
      }
      const text = node.innerText.trim();
      if (text) parts.push(text);
    }
  }

  return { title, text: parts.join("\n\n") };
}
