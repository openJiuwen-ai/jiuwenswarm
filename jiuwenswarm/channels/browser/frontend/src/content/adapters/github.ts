/** GitHub-specific page extractor. */

export function extractGitHub(): { title: string; text: string } {
  const title = document.title;
  const parts: string[] = [];

  // README / file view
  const article = document.querySelector<HTMLElement>("article.markdown-body");
  if (article) {
    parts.push(article.innerText.trim());
  }

  // Issue / PR body
  const body = document.querySelector(".comment-body");
  if (body) {
    parts.push((body as HTMLElement).innerText.trim());
  }

  // Issue/PR comments
  const comments = document.querySelectorAll<HTMLElement>(
    ".timeline-comment .comment-body"
  );
  comments.forEach((c) => parts.push(c.innerText.trim()));

  // Repository description
  const desc = document.querySelector<HTMLElement>("p.f4.my-3");
  if (desc) {
    parts.unshift(`Description: ${desc.innerText.trim()}`);
  }

  // Topics
  const topics = Array.from(
    document.querySelectorAll<HTMLElement>("a.topic-tag")
  )
    .map((t) => t.innerText.trim())
    .join(", ");
  if (topics) parts.unshift(`Topics: ${topics}`);

  return { title, text: parts.filter(Boolean).join("\n\n") || document.body.innerText.slice(0, 4000) };
}
