# Chrome Web Store Listing — Draft

> Ready-to-paste listing copy for the Chrome Web Store. Final descriptions and
> screenshot assets still need to be produced from this copy (see "Screenshots").

---

## Store fields

**Name:** JiuwenSwarm — AI research assistant for the pages you read

**Category:** Productivity

**Language:** English / 简体中文 (auto-detected)

**Website:** (JiuwenSwarm product URL — TBD)

---

## Short description (up to 132 chars)

```
Ask an AI agent about any page you're reading. Pin tabs into a research session,
ask across them, and let the agent highlight, scroll, and fill forms on the page.
```

---

## Detailed description

JiuwenSwarm is an AI research assistant that lives beside every page you read. It
reads the current tab, lets you pin multiple pages into a single research session,
and answers questions across everything you pinned — no copy-pasting.

**Research the way you already work**
- Pin any page (button, shortcut, or right-click) — its content becomes context.
- Ask across all pinned pages at once: *"Compare the revenue claims in these three
  filings and flag contradictions."*
- See exactly what you're feeding the agent: click a pinned chip to preview its text.
- Batch-pin all open tabs, reorder sources, and undo a pin with one click.

**Read rich, cite-able answers**
- Answers render as Markdown — headings, lists, code, and links.
- Every answer lists its **Sources**; click one to open it.
- Copy any reply, or stop generation while it streams.
- When the agent acts on the page (highlight, scroll, fill a form, screenshot), you
  see a status chip telling you what it's doing.

**Built for real research**
- 9 page-type extractors (GitHub, arXiv, SEC EDGAR, PubMed, Wikipedia, YouTube,
  Twitter/X, Hacker News) with a smart fallback for everything else.
- Sessions are shared with the JiuwenSwarm web app — start in the browser, finish
  anywhere.
- Session history and pinned pages persist across visits and reloads.
- Full-text search across every pinned page.
- Dark mode, full keyboard navigation, and English + Simplified Chinese.

**Note:** This extension works with a **JiuwenSwarm server**. Content goes to that server. See the in-app **🔒 Privacy** disclosure for details.

---

## Privacy

See the in-app disclosure (**⋯ → Privacy**) and the extension's privacy policy.

---

## Screenshots (to produce)

Capture from the built extension (light and dark mode) at 1280×800.

### Setup for capturing

1. `npm run build`, then load `dist/` unpacked at `chrome://extensions` (Developer mode).
2. Start the JiuwenSwarm server locally.
3. Open the side panel with **Ctrl+Shift+J**.
4. Create a session (**+ New**), then pin 2–3 real pages (e.g. an arXiv paper and a
   news article) so the context bar shows chips.
5. Send a question that returns a Markdown answer (with a list/code) so the **Sources**
   row and formatted answer are visible.

### Shots

1. **Side panel beside a pinned research page** — context bar with 2–3 chips; a chat
   answer with bold/headings and the **Sources** row.
2. **Pinning multiple tabs into one session** — several chips in the context bar.
3. **Agent highlighting a cited passage** on the page, with the `⚙` tool-status chip.
4. **Full-text search** modal (**⋯ → 🔍 Search pinned pages**) with results.
5. **Batch pin / reorder / undo** — context-bar interactions.
6. **Settings** page showing the **auto-summarize** toggle.
7. **Agent's view** (**⋯ → 👁 Agent's view**) — the clean view of the text the agent reads.
8. Same as #1 in **dark mode** (toggle OS color scheme).

### Delivery

- 1280×800 PNG, one per shot, in both light and dark where relevant.
- Name: `screenshot-01.png` … `screenshot-08.png`.

---

## Small promotional tiles (to produce)

- 440×280 tile showing the pin → ask → sources loop.
- 440×280 tile for the Chinese-language storefront (same copy translated).
