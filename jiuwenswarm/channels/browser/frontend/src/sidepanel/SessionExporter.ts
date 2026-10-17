/**
 * SessionExporter — export, import, open-in-web-app, and session templates.
 *
 * Export formats:
 *   JSON (.json)     — re-importable; contains full pinned page data
 *   Markdown (.md)   — human-readable research package for sharing
 *
 * Import: reads a JSON export and re-adds pinned pages to the current session.
 * Open in web app: opens the active session in the JiuwenSwarm web UI.
 * Templates: predefined session starters that auto-fill name, mode, and
 *            inject a starting prompt into the chat after creation.
 */

import { getPinnedPagesBySession, loadChatHistory } from "@shared/storage";
import { PinnedPage, ResearchSession, ChatEntry } from "@shared/types";

// ---------------------------------------------------------------------------
// Export — JSON
// ---------------------------------------------------------------------------

export interface ExportPackage {
  version: "1";
  session: {
    id: string;
    title: string;
    mode: string;
    createdAt: string;
  };
  pinnedPages: PinnedPage[];
  chatHistory: ChatEntry[];
  exportedAt: string;
}

export async function exportSessionJson(session: ResearchSession): Promise<void> {
  const pinnedPages = await getPinnedPagesBySession(session.id);
  const chatHistory = await loadChatHistory(session.id);
  const pkg: ExportPackage = {
    version: "1",
    session: {
      id: session.id,
      title: session.title,
      mode: session.mode,
      createdAt: session.createdAt,
    },
    pinnedPages,
    chatHistory,
    exportedAt: new Date().toISOString(),
  };
  _download(
    JSON.stringify(pkg, null, 2),
    `jiuwen-${_safeName(session.title)}.json`,
    "application/json",
  );
}

// ---------------------------------------------------------------------------
// Export — Markdown
// ---------------------------------------------------------------------------

export async function exportSessionMarkdown(session: ResearchSession): Promise<void> {
  const pinnedPages = await getPinnedPagesBySession(session.id);
  const chatHistory = await loadChatHistory(session.id);
  const lines: string[] = [
    `# Research Session: ${session.title}`,
    ``,
    `**Mode:** ${session.mode}  `,
    `**Created:** ${new Date(session.createdAt).toLocaleString()}  `,
    `**Exported:** ${new Date().toLocaleString()}`,
    ``,
    `---`,
    ``,
    `## Conversation (${chatHistory.length} messages)`,
    ``,
  ];

  for (const entry of chatHistory) {
    const who = entry.role === "user" ? "**You**" : "**JiuwenSwarm**";
    const when = new Date(entry.ts).toLocaleString();
    lines.push(`### ${who} — ${when}`);
    lines.push(``);
    const quoted = entry.text.trim().replace(/\n/g, "\n\n");
    lines.push(quoted, ``);
  }

  lines.push(`---`, ``, `## Pinned Pages (${pinnedPages.length})`, ``);

  for (const page of pinnedPages) {
    lines.push(`### ${page.context.title || page.context.url}`);
    lines.push(`- **URL:** ${page.context.url}`);
    lines.push(`- **Type:** ${page.context.pageType}`);
    lines.push(`- **Pinned:** ${new Date(page.pinnedAt).toLocaleString()}`);
    if (page.note) lines.push(`- **Note:** ${page.note}`);
    const preview = page.context.text.slice(0, 800).trimEnd();
    if (preview) {
      const quoted = preview.replace(/\n/g, "\n> ");
      lines.push(``, `> ${quoted}${page.context.text.length > 800 ? " …" : ""}`);
    }
    lines.push(``, `---`, ``);
  }

  _download(
    lines.join("\n"),
    `jiuwen-${_safeName(session.title)}.md`,
    "text/markdown",
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _safeName(title: string): string {
  return title.replace(/[^a-z0-9]+/gi, "-").toLowerCase().slice(0, 60) || "session";
}

function _download(content: string, filename: string, mimeType: string): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
