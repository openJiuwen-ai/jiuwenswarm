# JiuwenSwarm Electron desktop shell

This package is the Electron replacement for the current pywebview desktop
window. It keeps the existing Python backend and Web frontend server, and adds
an isolated browser `WebContentsView` on the right side of the application.

## Local files and downloads

Native file drops use the preload's `webUtils.getPathForFile` bridge and the
shared local-file consumer for chat attachments and connector package imports.
Directories and virtual files without disk paths remain unsupported.
Switching chat sessions discards pending file-drop results from the previous
session, including when switching back before the file read finishes.
Downloads through the desktop save API offer a save dialog; cancelling does not
start a request. Data streams into a unique temporary file beside the chosen
destination and replaces it only after success; failures remove the temporary
file and preserve any existing destination.
At startup, installer download fragments (`*.exe.part`, `*.dmg.part`, and
`*.tar.gz.part`) older than 24 hours are removed from the configured data
directory's `.updates` folder. Recent files, complete installers, directories,
and update helper scripts are retained.

Regression checks: `node --test tests/electron/file_transfer.test.cjs` from the
repository root and `npm run test:desktop-file-drop` from the frontend directory.

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
`--desktop-run-web` dispatch flags. The Electron build scripts are independent
from the current pywebview build scripts. On macOS,
`NOSIGN=1 bash scripts/build-electron-exe.sh` produces a local-only DMG, while
`NOTARIZE=1 bash scripts/build-electron-exe.sh` signs and notarizes both the app
and DMG, staples the tickets, and requires the final Gatekeeper assessment to
pass. Install Node >=22.12.0 through `NODE_DIR` or let the build download it;
the same runtime builds the frontend/Electron dependencies and is bundled for
backend skills. Electron DMGs are named
`<DisplayName>-<version>-macos.dmg`, matching the canonical `build_config.py`
name used by the Python desktop package. Test artifacts retain the distinct
`electron-test` or `electron-frontend-test` names.

Official macOS releases contain one ARM64 Electron DMG; x86_64 artifacts are
for local validation only and must not be uploaded to that release. The existing
backend installer selection is unchanged, so it does not block pywebview clients
from selecting the first Electron release. The Electron macOS install helper
verifies the native architecture and stages a signed, Gatekeeper-approved app before
quitting, waits for Electron and its backend children/ports, replaces the app,
and relaunches it. A failed swap restores the previous app. Logs are written to
`<data-dir>/logs/electron-update.log`. Updates from a mounted DMG or test build
are rejected; first copy the app into a writable installation directory.
The old pywebview macOS helper replaces the entire app bundle, independently
of its UI framework. Validate that migration separately from Electron-to-Electron
updates, using the supported old client and the signed release artifact.
Private information-collection agreements and daily-activity integration are
not included in this public packaging path.

## Backend identity and upgrades

The 0.2.8 backport retains IPC protocol 1 and its existing request handling.
Modern HELLO messages add build/deployment identity without importing develop's
event replay or readiness changes. Protocol 1 has no shutdown acknowledgement;
retirement must observe the validated process exit. Source identity uses full
content/dependency hashing rather than develop's filesystem identity cache.

Backend builds embed `jiuwenswarm/_worker_build_identity.json`. Rebuilding the
backend creates a new ID even at the same version; assembling an unchanged
backend into an installer preserves that ID. `-ReuseBackend` validates source,
lockfile and packaging inputs before accepting an existing backend.

Build entry points accept `WORKSWARM_SOURCE_ROOT`, `WORKSWARM_PACKAGING_ROOT`,
`WORKSWARM_BUILD_ROOT` and an optional `WORKSWARM_WORKER_IDENTITY` input file.
PowerShell also exposes `-SourceRoot`, `-BuildRoot` and `-WorkerIdentityFile`.
The defaults retain the current single-repository layout. Runtime never imports
release scripts. Metadata is generated in build staging, outside editable source.
An input manifest supplies provenance only: its ID becomes `input_build_id` and
each backend or wheel build allocates a fresh `backend_build_id`. Nested TUI
builds load scripts from the packaging root and inputs from the source root.

Supervisor validates each resident Worker's code and deployment before routing
requests. An owned incompatible Worker is stopped and observed exiting before
replacement under the startup lock. A different installation is never retired.
DMG startup and shutdown never retire a WHL or source-service deployment.
Legacy Python Workers without a deployment identity are never adopted or automatically
retired: a shared interpreter or working directory cannot prove the loaded
package's origin. Stop them through their original source or WHL service before
starting the new version. Legacy frozen desktop ownership still requires the
matching OS executable, user, endpoint and process identity.
Concurrent complete stop requires ownership validation of the current connection
before sending shutdown. An incomplete handshake is refused; validation of an
earlier connection cannot authorize a new peer. A child spawned by this Supervisor
can still be cleaned up through its retained process handle without unverified IPC.
Complete stop cancels unvalidated startup; spawn, connect and HELLO share owned-child
cleanup on failure or cancellation. The stored PID creation time comes from that
same ownership check, and subsequent reaping must still match it.
Source services need no identity file; installed wheels combine artifact/content
identity with the actual dependency environment. Worker pins its identity at
startup, so reconnecting cannot relabel old code after an update.

Stop services before changing source, wheels or dependencies. Use
`jiuwenswarm-stop` for debug services or `jiuwenswarm-start --stop <name>` for
instances. Desktop update helpers require resident Worker exit before replacing
the installed archive. Each update has a unique shutdown receipt written by the
parent after successful service shutdown and Worker persistence. Helpers require
this success before checking residual processes. Failed, pending or missing
receipts block replacement even if the Worker endpoint is already absent.

## Browser-agent boundary

Electron enables a loopback-only CDP endpoint on a launcher-selected ephemeral
port before `app.ready`. Python services receive the endpoint and authenticated
target resolver. When each MCP process starts, the resolver creates or reuses
its panel's `WebContentsView` and returns its exact DevTools TargetID.
BrowserAgent runs in `remote` mode against the persistent Electron session
used by the visible panes.

The target-aware Playwright MCP adapter routes normal page operations to one
TargetID. Every conversation/member pair gets an independent page and MCP
registration, including single-agent conversations. The browser pane has member
tabs; switching or hiding a tab does not stop background automation. Popups are
still redirected into their owning page; this is not a full Chrome tab manager.

Browser settings' `browser.headless` also controls embedded page presentation.
Headed mode keeps the normal browser tab and automatic single-agent expansion;
headless mode hides the browser tab and all native views, while MCP operations
continue on the same pages. Switching display modes does not itself close or
navigate those views, change their TargetIDs, or clear their login state. Electron
implements this as background WebContentsViews, not a separate headless Chrome
process. The default is headless, including while the saved setting is loading.
After a successful settings save the presentation updates without restarting
Electron. Failed saves leave the effective mode unchanged. HTML/Markdown file
links use the normal artifact preview in headless mode rather than navigating a
background task's page.

All browser panels use `persist:jiuwenswarm-browser`, separate from the trusted
UI's session. Cookies and origin-scoped local storage are shared across members
and conversations and survive restarts. Page navigation and sessionStorage are
independent. Signing out or switching accounts therefore affects other panels
on that site. Existing per-conversation profile directories are preserved but
not merged automatically: log in once in the new shared profile. External
Chrome profiles are not imported.

### Swarm external Chrome fallback

In Electron, a non-empty `browser.chrome_path` saved in Browser settings selects
the SDK's managed external Chrome for Swarm members only. It honors the same
`browser.headless` setting as CLI/pywebview. Each conversation/member owns its
MCP connection, debugging port and persistent profile under the user workspace's
`.browser-profiles/swarm_<identity>` directory. External tasks do not open an
empty embedded panel. An invalid path produces the managed Chrome launch error;
it does not silently switch to another browser.

Leave Chrome's path empty to use independent Electron member panels with shared
login state. Single-agent conversations always retain Electron panels regardless
of the saved Chrome path. CLI/pywebview and explicit launch-environment driver
overrides keep their previous behavior.

External Chrome profiles are separate from each other and from Electron's shared
cookie store. They are reused for the same conversation/member, not migrated or
merged. Apply browser settings before creating a team; existing running teams
are not migrated between backends mid-task. Recreate the team after a backend
change. The external fallback uses the normal bundled MCP/Node resolver, so
packaged builds must retain their backend Node runtime (or provide Node >=20).

`browser_run_code_unsafe` is available as in the upstream SDK, and
`browser_run_code` is a compatibility alias with the same unsafe semantics.
This restores SDK metadata/probe/batch RPCs. Both execute arbitrary JavaScript
in the MCP process and are RCE-equivalent: the page proxy is routing protection,
not a sandbox for malicious code. Do not use this mode for untrusted agents or
scripts. Shell-owned close/install/tab-list tools remain unavailable.

The MCP wrapper acquires a page lease on startup and releases it on exit.
Hidden pages with live MCP owners, and pages still being created, cannot be
evicted. Eight pages is a soft cache limit; concurrent owners may exceed it.
Dead-process leases are pruned during eviction. Reconnecting resolves a fresh
TargetID instead of reusing a stale ID cached in an agent spec.

Ordinary CLI/pywebview backends do not auto-attach to a discovered Electron.
For an externally started backend used with a frontend-only Electron build,
explicitly set `JIUWENSWARM_ELECTRON_BROWSER=1` and `BROWSER_DRIVER=remote` in
that backend's launch environment. An explicit managed/extension driver wins.
On the next browser configuration refresh, discovery restores only its own
overrides if the heartbeat expired or the shell exited, without overwriting
later user configuration.
Set `JIUWENSWARM_BROWSER_FORCE_MANAGED=1` in the Electron launch environment to
keep BrowserAgent on external managed Chrome instead of the built-in panels.

The CDP endpoint is loopback-only but unauthenticated: any local process could
connect to it and drive any exposed WebContents, including the trusted
renderer. This is an accepted risk of the current design — the Playwright
adapter needs an HTTP endpoint. The resolver requires a per-launch bearer token,
but neither it nor the page proxy isolates arbitrary unsafe code. Revisit if a
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
- Run simultaneous tasks from different members and conversations; verify
  independent pages, shared login, and member-tab switching without interference.
- Configure a Chrome path and test headed/headless Swarm tasks alongside a
  single-agent task: Swarm uses separate external Chrome instances while the
  single agent remains embedded. Clear the path and create a new team to verify
  embedded member panels are restored.
- Exercise `window.open`, target-blank links, downloads, and file save flows;
  no detached page may appear and downloads must complete through Electron.
- Crash the sideview renderer and the MCP subprocess independently; the view
  must reload/reconnect without exposing or navigating the trusted Jiuwen UI.
- Restart the whole app and verify authentication persistence plus a fresh CDP
  port/TargetID binding.
- Quit during an active task and confirm MCP, Python service trees, and the CDP
  listener all terminate while the Electron-owned view is never closed by MCP.

Automated browser checks: `npm run test:browser`. For the real Electron/MCP
integration test, set `SWARM_TEST_MODULES` to a `node_modules` directory containing
the pinned `@playwright/mcp@0.0.78`, then run `npm run test:browser:integration`.
Install frontend dependencies and the repository `.venv` first. The test uses a
temporary Electron profile, synthetic cookies and local pages only; it covers
the actual SDK probe scripts, IPC tabs, busy-page eviction and restart persistence.
Set `SWARM_TEST_CHROME` to a Chrome executable to additionally exercise two
external Swarm members in both headed and headless modes alongside Electron,
including managed port cleanup. This check uses temporary profiles only.
