/**
 * Hacker News-specific page extractor.
 *
 * Handles three page types:
 * - Item page (/item?id=…) — extracts OP title + URL + text + top-level comments
 * - Front page (/news, /) — extracts the ranked story list with scores
 * - User page (/user?id=…) — extracts user info + recent submissions
 */

export function extractHackerNews(): { title: string; text: string } {
  const url = location.href;

  if (url.includes("/item?id=")) return extractItem();
  if (url.includes("/user?id=")) return extractUser();
  return extractFrontPage();
}

function extractItem(): { title: string; text: string } {
  const parts: string[] = [];

  // Title and linked URL
  const titleEl = document.querySelector<HTMLElement>(".titleline > a");
  const title = titleEl?.innerText.trim() ?? document.title;
  const linkedUrl = titleEl?.getAttribute("href");
  if (linkedUrl && !linkedUrl.startsWith("item?")) {
    parts.push(`Link: ${linkedUrl}`);
  }

  // Score, author, date
  const subtext = document.querySelector<HTMLElement>(".subtext");
  if (subtext) parts.push(subtext.innerText.trim());

  // OP text (Ask HN, Show HN posts have a body)
  const opText = document.querySelector<HTMLElement>(".toptext");
  if (opText) parts.push(`Post:\n${opText.innerText.trim()}`);

  // Top-level comments (first 30)
  const comments = document.querySelectorAll<HTMLElement>(".comtr");
  let count = 0;
  for (const comment of comments) {
    if (count >= 30) break;
    // Only top-level (indent = 0)
    const indent = comment.querySelector<HTMLElement>(".ind img");
    const width = indent?.getAttribute("width") ?? "0";
    if (parseInt(width, 10) !== 0) continue;

    const text = comment.querySelector<HTMLElement>(".commtext")?.innerText.trim() ?? "";
    const author = comment.querySelector<HTMLElement>(".hnuser")?.innerText.trim() ?? "unknown";
    if (text) {
      parts.push(`Comment by ${author}:\n${text}`);
      count++;
    }
  }

  return { title, text: parts.filter(Boolean).join("\n\n") };
}

function extractFrontPage(): { title: string; text: string } {
  const title = "Hacker News Front Page";
  const rows = document.querySelectorAll<HTMLElement>(".athing");
  const lines: string[] = [];

  rows.forEach((row, idx) => {
    const link = row.querySelector<HTMLElement>(".titleline > a");
    const storyTitle = link?.innerText.trim() ?? "";
    const subtext = row.nextElementSibling?.querySelector<HTMLElement>(".subtext");
    const score = subtext?.querySelector<HTMLElement>(".score")?.innerText ?? "";
    if (storyTitle) {
      lines.push(`${idx + 1}. ${storyTitle}${score ? ` (${score})` : ""}`);
    }
  });

  return { title, text: lines.join("\n") };
}

function extractUser(): { title: string; text: string } {
  const userId = document.querySelector<HTMLElement>("#hnmain tr td:nth-child(2)")?.innerText.trim()
    ?? document.title;
  const title = `HN User: ${userId}`;
  const body = document.querySelector<HTMLElement>("#hnmain");
  return { title, text: body?.innerText.trim() ?? "" };
}
