# User Guide

## Overview

JiuwenSwarm Browser is a Chrome extension that puts the JiuwenSwarm AI agent
alongside any page you are reading. Unlike a standalone chat UI, it reads the
content of your current tab, lets you pin multiple pages into a single
**research session**, and allows the agent to interact with what you see
on screen.

---

## Core Concepts

### Research Session
A named conversation scope. All pinned pages, chat history, and agent context
belong to a session. You can have multiple sessions and switch between them.
Sessions are stored on the JiuwenSwarm server — they appear in the web app
as well, so you can start research in the browser and continue it there.

### Pinned Page
A snapshot of a page's extracted text added to the active session's context.
When you send a message, the agent automatically receives the combined text
of all pinned pages as background context — no copy-pasting needed.

### Side Panel
The main UI. Opens beside the page (not as an overlay), so you can read and
chat simultaneously without losing your place.

---

## Getting Started

### Open the Side Panel

Three ways to open it:

| Method | Action |
|---|---|
| Keyboard shortcut | **Ctrl+Shift+J** (Mac: **Cmd+Shift+J**) |
| Toolbar icon | Click the JiuwenSwarm icon → **Open panel** |
| Right-click menu | Right-click any page → **Pin this page**, **Unpin this page**, **Summarize this page**, or **Agent's view of this page** |

> **First time?** A short 3-step tour explains the core loop the first time you open
> the panel. You can replay it anytime from the **⋯** menu → **Getting-started tour**.

### Create Your First Session

1. Open the side panel.
2. Click **+ New** (top right of the panel). A small form appears below the header.
3. Type a session name.
4. Choose a **mode** (Research, Chat, Summarize, or Compare).
5. Click **Create**. The session is active immediately.

Your last active session is restored automatically the next time you open the panel.
Switch sessions by clicking the session name in the header to open the session picker.

---

## Working with Pages

### Pin a Page

Pinning extracts the page's main content and adds it to the active session's
context. The agent will use it when answering your questions.

**Methods:**
- **Keyboard:** Press **Ctrl+Shift+P** (Mac: **Cmd+Shift+P**) on any tab.
- **Button:** Open the side panel → click **📌 Pin page** (hover it to see the shortcut).
- **Right-click:** Right-click the page → **Pin this page** (or **Unpin this page** to remove it from the session).

> If you haven't created a session yet, pinning opens the **+ New** form first — create
> a session, then pin. The same happens if you try to send a message with no active
> session; a **+ Create a session** button also appears in the empty state.

The pinned page appears as a chip in the context bar under the header.
When you pin, a confirmation toast appears and the toolbar badge shows the
number of pages pinned to the active session.

- **Reorder** — use the **◀ ▶** buttons on a chip to change context priority
  (earlier chips are sent to the agent first).
- **Preview** — click any chip to expand a preview of the extracted text so you
  can see exactly what the agent will read. Click again to collapse.
- **Undo unpin** — clicking **×** removes the page and shows an **Undo** toast.
  Click **Undo** within a few seconds to restore it.
- **Keyboard** — with a chip focused, press **← / →** to move to the previous/next chip.

Hover over a chip to see the full URL, page type, and how many characters
were extracted.

### Extraction Quality Signals

Each chip in the context bar shows information about the extraction:

- **Tooltip** — hover over any chip to see the full URL, page type, and the
  number of characters extracted.
- **⚠ warning** — a chip with a red border and warning icon means very little
  text was extracted (under 200 characters). This usually means the page is
  JS-rendered and needs a moment to load, or it is behind a login. Try unpinning
  and re-pinning after the page fully loads.
- **PDF badge** — the page is a PDF opened inline in Chrome. Content script
  extraction does not work on binary PDFs; the **↻** retry button will re-attempt
  extraction via the JiuwenSwarm server's `read_pdf` tool if that tool is
  available.
- **↻ retry button** — visible on warning chips and PDF chips. Click to unpin the
  old version and re-extract the current state of the page.

### Page Types

The extension automatically detects the page type and applies a specialized
extractor:

| Page type | What is extracted |
|---|---|
| **Article / news** | Main body text via Readability.js |
| **GitHub** | README, issue/PR body, comments, repo description |
| **arXiv** | Title, authors, abstract, full paper text |
| **SEC EDGAR** | Filing content, 10-K/10-Q/8-K document body |
| **PubMed** | Abstract, full text (PMC), authors, MeSH terms |
| **Wikipedia** | Lead section + article body; references and navboxes stripped |
| **YouTube** | Video title, channel, description, and auto-generated transcript (when available) |
| **Twitter / X** | Tweet thread text with quoted tweets; handles multi-reply threads |
| **Hacker News** | Submission title and URL, top-level comments; front page link lists |
| **Generic** | Readability.js fallback on any other page |

> **PDFs** opened directly in Chrome are detected and shown with a **PDF** badge.
> Text extraction from binary PDFs requires the server-side `read_pdf` tool.

### Research Across Multiple Tabs

1. Open several tabs — a paper, a company's investor page, a news article.
2. Pin each tab using **Ctrl+Shift+P**.
3. All pinned pages appear as chips in the context bar.
4. Ask the agent in the side panel — it receives all pinned content as context.

Example prompt after pinning three pages:
> "Compare the revenue growth claims in these three sources and flag any
> contradictions."

---

## Using the Chat

### Sending Messages

Type your message in the chat input at the bottom of the side panel and press
**Enter** (or click Send). While the agent is composing a reply, a small pulsing
indicator appears; it's replaced by the answer as soon as the first words arrive.

Before your first message, one-click **suggestion chips** appear to help you
start — "Summarize this page", and "Compare the pinned pages" (only when you've pinned
at least two pages).

The agent automatically receives:
- Your message
- Combined text of all pages pinned to the active session
- Session mode (Research / Chat / Summarize / Compare)

### Reading the Answers

Agent replies render as **Markdown** — headings, bullet lists, inline code and
code blocks, and links all display formatted instead of as raw text. Click any link
in an answer to open it in a new tab.

- **Copy** — grab the full answer with one click.
- **Stop** — while an answer is streaming, a red **■** button appears; click it to stop
  generation.
- **Sources** — each answer lists the pages it drew from as chips. Click a source chip
  to open that page in a new tab.
- **History** — messages are timestamped and turns are separated, so long research
  sessions stay readable.

### Ask About Selected Text

Highlight any text on the page, then press **Ctrl+Shift+A** (Mac: **Cmd+Shift+A**).
The side panel opens and pre-fills the chat input with your selection quoted —
ready for you to add a question.

Alternatively: select text → right-click → **Ask JiuwenSwarm about "..."**

To look for text across your pinned pages: select text → right-click → **Search pinned pages for "…"**. The panel's full-text search opens pre-filled with your selection and runs immediately.

### Summarize a Page

Right-click anywhere on the page → **Summarize this page**. The side panel
opens with a pre-filled summary request. The page is temporarily added to
context for that request.

### Session Modes

| Mode | Best for |
|---|---|
| **Research** | Multi-source analysis, cross-referencing, fact-checking |
| **Chat** | Open-ended conversation without pinned context |
| **Summarize** | Distilling a single page or set of pages |
| **Compare** | Side-by-side comparison of two or more sources |

Change the mode when creating a session or via the session picker dropdown.

---

## Agent Page Actions

The agent can interact with pages you have open, not just read them. These
actions are triggered automatically when the agent determines they are useful —
no separate button is needed.

While the agent acts on a page, the panel shows an inline status chip (e.g.
"⚙ Highlighting a passage on the page…") so you can see what it is doing.

| Action | What happens |
|---|---|
| **Highlight text** | The agent highlights passages it is citing in the active tab with a colored overlay. |
| **Clear highlights** | Highlights are removed when the conversation moves on or the session changes. |
| **Scroll to element** | The agent scrolls the tab to a specific section relevant to your question. |
| **Fill form** | The agent fills fields in a form by label or field name — useful for search pages, data-entry tools, or research portals. |
| **Take screenshot** | The agent captures the visible area of the active tab as a PNG for visual analysis. |
| **Read a URL** | The agent reads a specific page directly, without the user needing to open it and pin it manually. |
| **Open URL** | The agent opens a URL in a new tab — for example, to follow up a cited source. |
| **Pin a page** | The agent can pin the current tab to the active session programmatically when relevant. |

---

## Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| **Ctrl+Shift+J** | Open / close side panel |
| **Ctrl+Shift+P** | Pin current tab to active session |
| **Ctrl+Shift+A** | Ask about selected text |

Mac users: replace **Ctrl** with **Cmd**.

To customize shortcuts: `chrome://extensions/shortcuts`

---

## Managing Sessions

### Switch Sessions
Click the session name label in the panel header to open the session picker.
Click any session to switch to it. The context bar updates to show that
session's pinned pages.

The picker is **keyboard-friendly**: with it open, press **↑ / ↓** to move, **Enter**
to select, and **Esc** to close.

### Rename a Session

Click **⋯** → **Rename session…** to give the active session a name you choose (or clear
the field to reset it). The name is saved locally in your browser and shows in the
header, the session picker, and the popup.

### Create a New Session

Click **+ New** in the panel header. A form appears with:
- A name field
- A **mode** selector (Research / Chat / Summarize / Compare)

### Export a Session

Click **⋯** in the panel header, then choose an export format:

- **Export JSON** — downloads a `.json` file containing session metadata, all pinned pages, and the full conversation.
- **Export Markdown** — downloads a `.md` file with session info, the full conversation, and a text preview of each pinned page. Useful for sharing or filing in a notes app.

> The conversation is exported from the extension's local chat history. It covers what has happened in this panel for that session.

### Pin All Open Tabs

Click **⋯** → **Pin all open tabs** to pin every tab in the current window to the active session at once (up to the session's pinned-page limit).

### Search Pinned Pages

Click **⋯** → **🔍 Search pinned pages** to search across every pinned page (title, URL, and extracted text). Type to search; results appear instantly. Click a result to open that page.

### Re-read Offline

The last agent answer is cached locally. If the JiuwenSwarm server is unreachable when you open the panel, the cached answer is shown with a "(cached — server offline)" label so you can still re-read your last research.

### Agent's View of a Page

Click **⋯** → **👁 Agent's view**, or right-click any page → **Agent's view of this page**, to read the exact text the agent extracts from the active page — a clean, distraction-free view of what JiuwenSwarm actually reads. Use **← Back** to return to the chat.

### Session Persistence

Sessions are stored on the JiuwenSwarm server and survive browser restarts.
They are shared with the JiuwenSwarm web app — any session you create in the
extension appears there, and vice versa. Pinned page metadata (extracted text,
URLs) is stored locally in Chrome storage and is browser-specific.

---

## Settings

Open settings via the popup (**⚙ Settings**) or right-click the toolbar icon →
**Options**.

| Setting | Default | Description |
|---|---|---|
| Host | 127.0.0.1 | IP or hostname of the JiuwenSwarm server |
| Port | 19000 | WebSocket port |
| Default mode | Research | Mode for newly created sessions |
| Auto-extract | On | Extract page context automatically when panel opens |
| Auto-summarize on pin | Off | Ask for a short summary each time you pin a page |

The extension follows your operating system's **light/dark** color scheme
automatically — there is no separate toggle. The entire interface (side panel,
popup, and settings) is available in **English and Simplified Chinese**, matching
your browser language.

---

## Troubleshooting

### Red status dot / "Not connected"

1. Ensure the JiuwenSwarm server is running.
2. Verify the host and port in Settings match your server configuration.
3. If the server is on a different port, update Settings → Save → wait 5 seconds.

When the connection drops, the panel shows a **"Lost connection to JiuwenSwarm"**
banner with a **Retry** button — click it to reconnect without reloading the panel.

### Page context is empty or shows a warning chip

- Some pages block content scripts (bank portals, Chrome internal pages).
  These pages cannot be extracted.
- SPA pages (React, Next.js) may need a moment to render. Try unpinning and
  re-pinning after the page fully loads.
- Click the **↻** retry button on the chip to re-extract the current page state.

### PDF chip shows no text

PDF pages opened inline in Chrome cannot be extracted by the content script.
The chip shows a **PDF** badge. Use the **↻** retry button, which will route
extraction through the server-side `read_pdf` tool if it is available on your
JiuwenSwarm server.

### Side panel does not open

Chrome 114+ is required for the Side Panel API. If you are on an older Chrome
build, update Chrome.

On Chromium-based browsers that do not implement the Side Panel API (360 Safe
Browser, QQ Browser, Sogou Browser), the extension automatically opens the panel
as a standalone popup window instead. All features work identically in this mode.

### Context is cut off

Pages are capped at 120,000 characters. Very long documents are truncated using
a head + tail strategy: the first ~80% of the budget and the last ~20% are kept,
preserving both the opening and any summary or conclusion at the document end.
A `[...truncated...]` marker appears in the extracted text. The server also enforces
its own context window limit.

---

## Privacy

- The extension sends page content to the **JiuwenSwarm server you configure**, which forwards it to your chosen LLM provider. The extension contacts no other service.
- Sessions are stored on your server; uninstalling the extension deletes only locally stored data.

You can review this at any time from the side panel: **⋯** → **🔒 Privacy**.
