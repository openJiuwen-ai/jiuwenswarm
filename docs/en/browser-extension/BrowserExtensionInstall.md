# Installation Guide

## Requirements

| Requirement | Version |
|---|---|
| Chrome (or Chromium-based browser) | 114+ (Side Panel API) |
| JiuwenSwarm server | running on any port (default 19000) |
| Node.js | 18+ (build only) |
| npm | 9+ (build only) |

---

## 1. Build the Extension

Clone the repository and install dependencies:

```bash
cd jiuwenswarm-browser
npm install
```

Then build:

```bash
npm run build
```

This produces a `dist/` directory containing the fully compiled extension.

---

## 2. Load into Chrome (Developer Mode)

1. Open Chrome and navigate to `chrome://extensions`.
2. Enable **Developer mode** (toggle, top-right corner).
3. Click **Load unpacked**.
4. Select the `dist/` folder inside this project.
5. The JiuwenSwarm icon appears in the Chrome toolbar.

The extension auto-reconnects to the server whenever Chrome is open — no manual
reconnection is needed.

---

## 3. Verify the Connection

1. Start your JiuwenSwarm server (default: `ws://127.0.0.1:19000/ws`).
2. Click the JiuwenSwarm toolbar icon — the popup shows **"Connected to local server"**
   with a green dot.
3. If the dot is red, check:
   - Is the server running? (`curl http://127.0.0.1:19000/health`)
   - Is the port correct? Open **Settings** (⚙ in the popup) and verify Host/Port.

---

## 4. Configure the Server Address (Optional)

The extension defaults to `ws://127.0.0.1:19000`. To change this:

1. Click the JiuwenSwarm icon → **⚙ Settings**.
2. Update **Host** and **Port**.
3. Click **Save settings**.
4. The background worker reconnects automatically within a few seconds.

---

## 5. Pin the Side Panel (Recommended)

The side panel is the primary interface. To keep it always accessible:

1. Right-click the JiuwenSwarm toolbar icon.
2. Select **Pin to toolbar** (if not already pinned).
3. Press **Ctrl+Shift+J** (Mac: **⌘+Shift+J**) to open/close the panel from any tab.

> **First run:** when you open the panel for the first time, a short getting-started
> tour walks you through the pin → ask → act loop. It appears only once; replay it
> anytime from the **⋯** menu.

---

## 6. Pack for Distribution (Optional)

To create a `.zip` suitable for the Chrome Web Store or manual sharing:

```bash
npm run pack
```

Output: `jiuwenswarm-browser-<version>.zip` in the project root.

---

## Updating

After pulling new source code:

```bash
npm install       # pick up any new dependencies
npm run build     # rebuild dist/
```

Then go to `chrome://extensions` and click the **↻ Reload** button on the
JiuwenSwarm card. No need to re-add the extension.

---

## Uninstalling

Go to `chrome://extensions`, find JiuwenSwarm, and click **Remove**.
Locally stored data (pinned page metadata, settings) is deleted. Sessions are stored
on the JiuwenSwarm server and are unaffected — they remain accessible via the web app.
