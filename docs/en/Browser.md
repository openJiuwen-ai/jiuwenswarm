# Browser tools

## 1. Overview

JiuwenSwarm browser tools drive a real Chrome instance for navigation, form
filling, clicks, uploads, and other web tasks.

Chrome is managed by the browser agent. The user configures the Chrome
executable and display mode in the web UI, and the agent starts the browser on
the first browser task. There is no separate browser service to start from the
frontend.

The managed browser can:

- Open pages and wait for loading
- Click elements, enter text, and upload files
- Execute multi-step web tasks
- Reuse a session and its login state
- Read page titles, URLs, and page content

## 2. Quick start

### 2.1 Install Chrome

Install Google Chrome on the machine that runs JiuwenSwarm. The managed driver
normally detects a standard Chrome installation automatically.

If Chrome is installed in a custom location, open `chrome://version`, copy the
**Executable Path**, and use it in the next step.

### 2.2 Configure the browser

1. Open the JiuwenSwarm web UI.
2. Go to **Settings** > **Browser**.
3. Optionally enter the full Chrome executable path. Leave it empty to use
   automatic detection.
4. Enable **Show browser** when you need to see or manually authenticate in the
   managed browser. Disable it for headless execution.
5. Save the settings.

Changing the display mode restarts the browser runtime when necessary so the
next task uses the selected mode.

### 2.3 Run a browser task

Ask the agent to open a page, extract information, fill a form, or continue an
authenticated workflow. The browser agent starts and owns Chrome automatically.

In visible mode, the Chrome window appears when the first browser task starts.
Complete login, MFA, or other required manual authorization in that window, then
continue the task in the conversation.

## 3. Usage guidance

- Keep the same agent session for long workflows that depend on login state.
- Use visible mode for manual login, MFA, QR codes, and authorization prompts.
- Use headless mode for tasks that do not require manual interaction.
- Do not start a separate Chrome with a fixed debugging port for normal
  JiuwenSwarm usage. The managed driver selects and injects the correct CDP
  endpoint.
- Keep `BROWSER_ALLOW_SHORT_TIMEOUT_OVERRIDE=0` unless short browser task
  timeouts are explicitly required.

## 4. Examples

### 4.1 Extract web information

1. Ask: "Extract today's headlines and summaries from
   https://example.com/news."
2. The agent starts the managed browser, visits the page, and returns the
   requested information.

### 4.2 Send email with an attachment

1. Enable **Show browser** and save the setting.
2. Ask the agent to open Gmail.
3. Sign in in the managed Chrome window if required.
4. Ask the agent to compose the message and attach the file.

## 5. Configuration

### 5.1 `config.yaml`

| Configuration | Type | Default | Description |
|---|---|---|---|
| `browser.chrome_path` | string/map | `""` | Chrome executable. Empty uses automatic detection. A map may provide OS-specific paths. |
| `browser.headless` | boolean | `true` | `false` shows the managed browser window. |

Example:

```yaml
browser:
  chrome_path:
    windows: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
    macos: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    linux: "/usr/bin/google-chrome"
  headless: true
```

An empty `chrome_path` clears any previously configured binary and lets
openJiuwen detect Chrome from the system. A non-empty path is authoritative: if
it does not identify a Chrome executable, browser startup reports an error
instead of silently using another installation.

### 5.2 Advanced environment overrides

Most installations do not need these variables.

| Environment variable | Default | Description |
|---|---|---|
| `BROWSER_DRIVER` | `managed` in JiuwenSwarm | Browser driver mode. |
| `BROWSER_MANAGED_BINARY` | auto-detected | Direct openJiuwen runtime override. JiuwenSwarm derives it from `browser.chrome_path`. |
| `BROWSER_MANAGED_PORT` | `9333` | Port for an unkeyed managed instance. Keyed instances allocate free ports automatically. |
| `BROWSER_MANAGED_USER_DATA_DIR` | managed profile directory | Overrides the managed profile directory. |
| `BROWSER_MANAGED_ARGS` | derived from display mode | Additional Chrome startup arguments. |
| `BROWSER_MANAGED_KILL_EXISTING` | `false` | Allows the driver to terminate a matching existing Chrome before launch. Use only when profile ownership is understood. |

`PLAYWRIGHT_CDP_URL` is intended for explicit remote-driver setups. It is not
required for the normal managed-browser flow.

## 6. Architecture

The browser lifecycle is:

`frontend settings -> agent browser task -> managed Chrome start -> task execution -> session reuse`

- The frontend `BrowserPanel` only reads and saves Chrome path and display mode.
- JiuwenSwarm maps those settings to the browser-agent runtime.
- openJiuwen's `BrowserService` and `ManagedBrowserDriver` allocate the endpoint,
  start Chrome on demand, monitor it, reuse the profile, and stop it with the
  agent lifecycle.
- The Playwright MCP endpoint is injected from the managed browser instance; it
  is not supplied by a frontend-launched browser.

This keeps browser startup, profile ownership, endpoint allocation, and cleanup
under one lifecycle owner: the browser agent.
