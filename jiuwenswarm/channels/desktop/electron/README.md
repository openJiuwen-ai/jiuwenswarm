# JiuwenSwarm Electron desktop shell

This package is the Electron replacement for the current pywebview desktop
window. It keeps the existing Python backend and Web frontend server, and adds
an isolated browser `WebContentsView` on the right side of the application.

## Development

From this directory:

```bash
npm install
npm run dev
```

`npm run dev` starts the Vite dev server first and navigates as soon as it
answers (HMR included). Electron then starts the Python services the same way
the pywebview desktop shell does: workspace preparation runs once in the
launcher, and AgentServer plus Gateway are spawned directly in parallel —
there is no `jiuwenswarm.app` supervisor process on this path. The frontend
reconnects its API/WebSocket traffic once the gateway is ready.

The following loopback ports must be available:

- `127.0.0.1:19000` — JiuwenSwarm backend
- `127.0.0.1:5173` — built frontend and API/WebSocket proxy

## Process model

- The trusted Electron renderer loads `http://127.0.0.1:5173`.
- The remote browser uses a separate persistent Electron session named
  `persist:jiuwenswarm-browser`.
- Remote browser pages have Node integration disabled, context isolation and
  Chromium sandboxing enabled, and no preload script.
- The preload exposes a narrow `window.jiuwenDesktop` API to the trusted
  renderer. It also provides the existing `window.pywebview.api` names while
  the two desktop shells coexist.

## Packaged backend contract

In development, services are launched through `uv`. In a packaged Electron
application, `main.cjs` expects the existing PyInstaller directory to be copied
to Electron's resources as:

```text
resources/backend/jiuwenswarm       # macOS/Linux
resources/backend/jiuwenswarm.exe   # Windows
```

The executable must retain the existing `--desktop-run-app` and
`--desktop-run-web` dispatch flags. Electron packaging, signing, notarization,
and release-updater migration are intentionally a subsequent release phase;
the current pywebview build scripts remain available until that phase is
validated.

## Browser-agent boundary

Electron enables a loopback-only CDP endpoint on a launcher-selected ephemeral
port before `app.ready`. After constructing the right-hand `WebContentsView`,
it passes that view's exact DevTools TargetID and the endpoint to the Python
services. BrowserAgent then runs in `remote` mode against the same persistent
Electron session used by the visible pane.

The target-aware Playwright MCP adapter exposes only that TargetID. The trusted
Jiuwen renderer remains present on Electron's raw CDP endpoint, so bypassing
the adapter or selecting a page by list order/URL is forbidden. The adapter
also turns page/context close and new-tab operations into no-ops. Electron
redirects page popups into the owned sideview, and BrowserAgent serializes all
logical sessions because they share one visible pane. Non-Electron processes
continue to use the existing managed Chrome behavior.

The CDP endpoint is loopback-only but unauthenticated: any local process could
connect to it and drive any exposed WebContents, including the trusted
renderer. This is an accepted risk of the current design — the Playwright
adapter needs an HTTP endpoint, and the exact-target wrapper is the
enforcement boundary for the agent path, not the endpoint itself. Revisit if a
per-session authenticated transport becomes available.

Packaged builds bundle the pinned `@playwright/mcp` runtime (and its
`playwright` dependencies) under `resources/app/node_modules`. The backend
launches the wrapper with the app executable itself in Node mode
(`ELECTRON_RUN_AS_NODE=1`, forwarded through the openjiuwen env allowlist), so
the browser agent does not require a user-installed Node.js, an `npx` shim, or
a first-run npm download. Development mode still resolves `@playwright/mcp`
through `npx` on demand.

## Browser-agent validation matrix

Before release, validate these flows against a development build:

- Navigate manually, then continue from BrowserAgent; repeat in the opposite
  direction and verify the toolbar state follows agent navigation.
- Sign in manually, restart BrowserAgent tasks, and confirm cookies/storage
  remain in `persist:jiuwenswarm-browser` without appearing in managed Chrome.
- Run simultaneous tasks from two logical sessions and verify that their
  actions execute serially in the one visible pane.
- Exercise `window.open`, target-blank links, downloads, and file save flows;
  no detached page may appear and downloads must complete through Electron.
- Crash the sideview renderer and the MCP subprocess independently; the view
  must reload/reconnect without exposing or navigating the trusted Jiuwen UI.
- Restart the whole app and verify authentication persistence plus a fresh CDP
  port/TargetID binding.
- Quit during an active task and confirm MCP, Python service trees, and the CDP
  listener all terminate while the Electron-owned view is never closed by MCP.
