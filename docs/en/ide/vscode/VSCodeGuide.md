# JiuwenSwarm VS Code Extension — Usage Guide

Complete reference for every setting, panel element, and workflow. For installation see [VSCode.md](VSCode.md).

---

## Configuration

Open **Settings → Extensions → JiuwenSwarm** (or search `jiuwenswarm` in the Settings editor):

| Setting | Default | Description |
|---------|---------|-------------|
| `jiuwenswarm.host` | `127.0.0.1` | Hostname or IP of the JiuwenSwarm WebSocket server |
| `jiuwenswarm.port` | `19000` | Port — connects to `ws://host:port/ws` |
| `jiuwenswarm.channelId` | `ide` | Client identifier shown in server logs and traces |
| `jiuwenswarm.autoConnect` | `true` | Open the WebSocket when VS Code starts |
| **`jiuwenswarm.defaultMode`** | `code.plan` | Declared default mode for the mode selector (`code.plan` / `code.normal` / `code.team`). The chat panel always starts in **Plan & Execute**; switch per-session with the mode pill. |
| `jiuwenswarm.approveEdits` | `false` | Require explicit approval before applying any agent file edit |
| `jiuwenswarm.runCommandsInTerminal` | `true` | Run `bash` / `run_command` tool calls in a JiuwenSwarm terminal so you can watch live output |
| `jiuwenswarm.useDiffViewer` | `false` | Show VS Code's built-in diff viewer and ask **Accept / Reject** before applying each file edit |
| `jiuwenswarm.loadHistoryOnSwitch` | `true` | Fetch and display past messages when switching to an existing session |
| `jiuwenswarm.keepAlive.enabled` | `true` | Send periodic WebSocket ping frames to keep the connection alive and detect drops early |
| `jiuwenswarm.keepAlive.interval` | `30` | Seconds between keep-alive pings (5–300) |
| `jiuwenswarm.rewindEnabled` | `true` | Snapshot files before agent edits; show the rewind bar after each turn |
| `jiuwenswarm.projectTree.enabled` | `true` | Prepend a 2-level directory listing of the workspace root to every message |
| `jiuwenswarm.projectTree.maxFiles` | `200` | Max entries in the project tree listing (10–2000) |
| `jiuwenswarm.gitEnabled` | `false` | Show **Commit** / **Push** buttons below the message list (requires a git repository) |

Connection settings (host, port, channel ID, auto-connect, keep-alive) are read when the extension activates; changing them prompts you to reload the window. Behaviour toggles (approval, diff viewer, terminal, rewind, project tree, git) are applied live as you use them.

---

## Opening the Panel

Open **JiuwenSwarm: Open Chat** from the command palette or press **Ctrl+Shift+J** / **⌘⇧J**. The panel opens as a webview panel beside the current editor and can be dragged into any editor group.

A session is created automatically on first connect. The header shows the session title and live connection state.

---

## Header Bar

```
● Session title                    [New] [⚙]
```

| Element | Description |
|---------|-------------|
| Status dot | Grey until the first connection, then green = connected, yellow (pulsing) = reconnecting, red = disconnected. To reconnect after a disconnect, click the status-bar item (`$(circle-slash) JiuwenSwarm`) or use **New**. |
| Session title | Name of the active session |
| **New** button | Start a fresh session: reconnects the WebSocket and clears the message list |
| **⚙** menu | Sessions, Skills, Theme (Auto/Dark/Light), Debug log |

---

## Mode Selector

The mode pill in the bottom input bar controls how the agent works:

| Mode | Key | Description |
|------|-----|-------------|
| **Plan & Execute** | `code.plan` | Agent reads files and designs a plan, then waits for you to approve before making any edits. Best for non-trivial or risky changes. |
| **Execute** | `code.normal` | Agent edits files and runs commands without a planning phase. Best for clear, contained tasks. |
| **Team Coding** | `code.team` | A leader agent breaks the task into parallel sub-tasks and assigns them to specialist agents simultaneously. Best for large decomposable work. |

Click the mode pill to open the dropdown. If the current session already has messages, switching mode asks for confirmation and starts a new session. The mode pill starts in **Plan & Execute**.

---

## Chat Input

```
[+]  [mode ▾]  @ files · # skills · ! prompts — Enter to send · Shift+Enter for new line     [↑]
```

| Element | Description |
|---------|-------------|
| **+** | Opens a file picker to attach images (PNG, JPEG, WebP, GIF; up to 10 MB each). Previews appear above the input; click **✕** to remove. Images are base64-encoded and sent with the message. |
| Mode pill | Quick mode switcher |
| Textarea | Grows vertically as you type. **Enter** sends; **Shift+Enter** inserts a newline. |
| Send / Stop button | Submits the message while idle. Becomes a Stop button while the agent is streaming — click to interrupt. |

### Inline pickers

Three characters trigger autocomplete dropdowns that appear above the input:

**`@` — file mention**

Type `@` followed by part of a filename to search workspace files. Selecting a file inserts `@relative/path/to/file` into the message. When sent, the extension reads the file and includes its full contents in the context under a fenced code block.

**`#` — skill picker**

Type `#` to see all registered skills. Continue typing to filter by name. Selecting a skill inserts `#skill-name` into the message.

**`!` — preset prompts**

Type `!` to see eight built-in prompt templates:

| Label | Template |
|-------|---------|
| Explain | Explain what this code does and how it works. |
| Fix bug | Find and fix the bug in this code. Explain what caused it. |
| Write tests | Write unit tests for this code. Cover edge cases. |
| Refactor | Refactor this code to be cleaner and more maintainable. |
| Optimize | Optimize this code for performance. Explain the changes. |
| Document | Add clear documentation and comments to this code. |
| Review | Review this code for bugs, security issues, and improvements. |
| Implement | Implement the following feature: |

Continue typing to filter the list. Selecting a template replaces `!query` with the full prompt text.

For all three pickers: **Arrow keys** navigate, **Enter** or **Tab** selects, **Escape** dismisses.

---

## Message List

Each completed turn consists of:

- **Your message** — right-aligned bubble.
- **Reasoning block** — when the model uses extended reasoning, a collapsible **Reasoning…** section appears before the response. Click the arrow to expand or collapse.
- **Thinking indicator** — while the model is prefilling a response (before the first token arrives), a **Thinking…** pill appears. It flips to **Generating…** the moment reasoning or text starts streaming, then clears when the model call ends.
- **Agent response** — text streams in as it is generated. When a session's history is reloaded, assistant messages are rendered with bold/italic, fenced code blocks, and clickable file links.
- **Tool call cards** — every tool the agent invokes appears as an inline card with:
  - Tool icon and friendly name (a gear icon plus a label like `Edit`, `Bash`, `WebSearch`, `TodoWrite`; the raw tool id such as `str_replace_editor` is shown in the card's tooltip)
  - Live spinner → checkmark or ✕ on completion
  - Collapsible **Inputs** section (parameters sent to the tool)
  - Collapsible **Output** section (result returned by the tool)

---

## Stats Bar and Metrics

A stats bar between the header and the message list shows session-level metrics that update after each turn (it appears once the first turn completes):

- **Turns** — total completed turns in the session
- **Errors** — turns that ended in an error
- **Tokens** — cumulative token count (input + output)
- **LLM calls** — cumulative model invocations
- **Avg latency** — mean response time across turns
- **TTFT** — mean time-to-first-token
- **Cost** — estimated USD cost (shown when the server reports pricing)
- **TODO** — live agent todo progress (✓ completed / ◐ in progress / ☐ pending), when the agent reports one

The bar chart icon (right side of the stats bar, shown after two or more turns) toggles **mini charts** — bar graphs of tokens and duration per turn. Hover a bar to see that turn's details.

The **server memory** chip shows live JiuwenSwarm server RAM usage (RSS of total, plus available), polled every 10 seconds.

In the input area at the bottom, a **context bar** shows how full the active model's context window is (0–100%). It turns orange above 60% and red above 80%; a warning chip appears as the context approaches the server's auto-compaction threshold.

---

## IDE Context Injection

Every message has a structured context block prepended. The agent sees it as part of your message.

### What is injected

| Field | Source |
|-------|--------|
| Active file path and language | `vscode.window.activeTextEditor` + `document.languageId` |
| Cursor line | `editor.selection.active.line` |
| Selected code | `editor.document.getText(editor.selection)` (if non-empty) |
| Diagnostics (up to 10) | `vscode.languages.getDiagnostics(doc.uri)` |
| Other open tabs (up to 10) | `vscode.window.tabGroups.all` |
| Project tree (2-level) | Workspace folder traversal; skips `.git`, `build`, `node_modules`, `dist`, `target`, etc. |
| Git branch + change count | `git rev-parse` + `git status --porcelain` subprocess |
| Project rules | First non-empty file found: `.jiuwenswarm/instructions.md`, `.jiuwenswarm/rules.md`, `AGENTS.md` |
| @-mentioned files | Full file content for each `@path` typed in the message |

### Project rules

Create a file at the workspace root to inject standing instructions into every message:

```
.jiuwenswarm/instructions.md   ← checked first
.jiuwenswarm/rules.md          ← checked second
AGENTS.md                      ← checked third
```

Use it to define coding style, forbidden patterns, preferred libraries, or any project-specific context the agent should always know.

### Controlling what is injected

| Setting | Effect |
|---------|--------|
| `jiuwenswarm.projectTree.enabled` | Toggle the directory listing on or off |
| `jiuwenswarm.projectTree.maxFiles` | Limit entries (10–2000) for large mono-repos |

### Example context block

````
<!-- IDE Context -->
Active file: /Users/mishka/project/src/api/handler.py  (python)
Cursor line: 87

Selected code:
```
def handle_request(req):
    result = blocking_call(req)
    return result
```

Diagnostics (2):
  • Line 87: Variable 'result' is not used before return
  • Line 88: blocking_call is deprecated

Other open files (2):
  /Users/mishka/project/src/api/router.py
  /Users/mishka/project/tests/test_handler.py

Project structure:
  src/
    api/
    models/
  tests/
  pyproject.toml

Git: branch=feature/async-refactor, 3 uncommitted changes

Project rules:
Always use async/await. No blocking calls. Follow PEP 8.
<!-- End IDE Context -->
````

---

## Clickable File Links

File paths in agent responses become clickable links that open the file at the referenced line.

| Pattern | Example | Effect |
|---------|---------|--------|
| Backtick path with directory | `` `src/api/handler.py` `` | Opens file at line 1 |
| Backtick path with line | `` `src/api/handler.py:42` `` | Opens file at line 42 |
| Bare `path/to/file.ext:N` | `src/auth/router.py:87` | Opens file at line 87 |

Plain identifiers in backticks (no `/` and no `:N`) are not linkified as files. Paths inside fenced code blocks are rendered verbatim.

---

## Actions and Keyboard Shortcuts

| Action | Win / Linux | Mac |
|--------|-------------|-----|
| Open / focus chat panel | `Ctrl+Shift+J` | `⌘⇧J` |
| Send selection | `Ctrl+Shift+E` | `⌘⇧E` |
| New session (command palette) | — | — |
| Fix with JiuwenSwarm (lightbulb) | `Ctrl+.` | `⌘.` |

**Open / focus** — opens the JiuwenSwarm panel. If already open, brings it to the front.

**Send selection** (`Ctrl+Shift+E` / `⌘⇧E` or right-click → **Send Selection to JiuwenSwarm**) — opens the panel and pre-fills the input with the selected code:

````
[File: handler.py]
```
def handle_request(req):
    ...
```
````

Add your question and press Enter.

**New session** (command palette: `JiuwenSwarm: New Session`) — reconnects the WebSocket to start a fresh session.

---

## Code Action Quick Fix

VS Code shows a lightbulb 💡 next to any line that has an error or warning. JiuwenSwarm registers a **Fix with JiuwenSwarm** code action:

1. Place the cursor on a line with an error (red squiggly).
2. Click the lightbulb or press `Ctrl+.` / `⌘.`.
3. Select **Fix with JiuwenSwarm**.
4. The chat panel opens with the error message and ±7 lines of surrounding code pre-filled:

````
Fix this error in handler.py:

Error:
Variable 'result' is not used before return

```python
def handle_request(req):
    result = blocking_call(req)
    return result
```
````

5. Press Enter to send.

Works for any language VS Code has diagnostics for — TypeScript, Python, Java, Go, Rust, C#, and more.

---

## File Edit Workflow

When the agent calls `str_replace_editor`, `write_file`, or `create_file`, the extension applies the edit to the workspace and a notification toast confirms each applied change.

### With approval

Enable `jiuwenswarm.approveEdits` in settings to see an **Approve / Reject** prompt before every file change. Clicking **Reject** discards the edit; clicking **Approve** writes it to disk.

### With the diff viewer

Enable `jiuwenswarm.useDiffViewer` to review every proposed edit in VS Code's built-in diff viewer before it is applied, then choose **Accept** or **Reject**.

---

## Terminal Integration

Agent shell commands (`bash`, `run_command`) run in a **JiuwenSwarm** terminal created by `vscode.window.createTerminal()`. The terminal is created on the first command and reused for subsequent commands; it is disposed when the extension deactivates. Disable `jiuwenswarm.runCommandsInTerminal` to skip running commands locally (the agent still runs them on the server).

---

## Checkpoint / Rewind

After any agent turn that edits files, the rewind bar appears below the message list:

```
⟲ Agent edited files this turn    [⟲ Undo changes]
```

### How it works

Before the agent's first edit to a file in a given turn, the extension snapshots that file's current content (read via the VS Code filesystem API). At the end of the turn (`chat.final`) the snapshots are locked in.

### Using rewind

Click **⟲ Undo changes**. The extension restores every snapshotted file. Files that did not exist before the turn are deleted.

A status line confirms the result:

```
⟲ Rewound 3 file(s)
```

### Limits

| Scenario | Behaviour |
|----------|-----------|
| Agent created a file | File is deleted on rewind |
| Agent edited a file | File is restored to pre-turn state |
| You send another message | Bar disappears; snapshots are discarded |
| New session | Bar cleared |

Disable via `jiuwenswarm.rewindEnabled` in settings.

---

## Git Quick Actions

Enable `jiuwenswarm.gitEnabled` in settings to show a toolbar below the message list with two buttons:

**Commit** — opens an input box pre-filled with your last sent message as the commit message (prefixed with "AI: "). Confirming runs `git add -u && git commit -m <message>`. The git bar updates after commit.

**Push** — runs `git push` in the background. Status updates on completion.

The git bar shows the current branch and the number of uncommitted files. It updates after each agent turn and only appears inside a git repository.

---

## Sessions

### Opening the overlay

Click **⚙ → Sessions** in the header.

### What the list shows

Each row shows session title, time of last message (relative), and message count.

### Switching

Click a row to switch. With `jiuwenswarm.loadHistoryOnSwitch` on, past messages stream in automatically.

### Creating

Click **New** in the header or run `JiuwenSwarm: New Session` from the command palette.

### Deleting

Click **✕** on a non-active session row. Click once (turns red) then again within 2 seconds to confirm. The active session cannot be deleted — start a new session first.

### Refreshing

Click **↺** in the overlay header. Up to 20 sessions are shown.

---

## Skills

### Opening the overlay

Click **⚙ → Skills** in the header.

### What the list shows

Each skill row shows name, description, trigger, and ON/OFF toggle. Click ON or OFF to enable or disable. The change is sent to the server via `skills.toggle`.

### Picking a skill from the input

Type `#` in the textarea. A popup lists all loaded skills. Filter by continuing to type. Select with Enter or Tab.

---

## Connection Status Bar

The status bar (bottom-right) shows live WebSocket state:

| Icon | Meaning |
|------|---------|
| `$(check) JiuwenSwarm` | Connected |
| `$(loading~spin) JiuwenSwarm` | Connecting |
| `$(sync~spin) JiuwenSwarm` (yellow) | Reconnecting — exponential backoff: 1 s → 30 s max |
| `$(circle-slash) JiuwenSwarm` (red) | Disconnected — click to reconnect |

Token total appears next to the label: `$(check) JiuwenSwarm · 42.3k`.

---

## Theme

| Option | Description |
|--------|-------------|
| **⚙ → ◐ Auto** | Follows VS Code's light or dark theme (default) |
| **⚙ → 🌙 Dark** | Forces dark regardless of VS Code theme |
| **⚙ → ☀ Light** | Forces light regardless of VS Code theme |

Stored in webview local storage; survives panel restarts.

---

## Model Selector

When connected to a server that has multiple models configured, a model dropdown appears in the input bar. Click to open it and switch models. The active model is shown in the mini model chip.

---

## Debug Log

Click **⚙ → Debug log** to open a scrollable log panel below the message list. It records:

- Every WebSocket frame received (raw JSON with timestamp)
- Every message sent (content, context size, media item count)
- Session switches, reconnects, connection state transitions
- Action dispatches (list_sessions, list_skills, toggle_skill, etc.)
- File edit tool calls (tool name and parameters)

The panel keeps the most recent 500 lines. Toggle off to hide the panel; use **Clear** to empty the log (its content persists across toggles).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Panel is blank | `chat.html` missing or CSP issue | Reinstall from the latest VSIX |
| Status bar shows `$(circle-slash)` | Server not running or wrong host/port | Start JiuwenSwarm; verify settings; click widget to reconnect |
| Messages send, no response | Server unreachable after handshake | Enable Debug log; check for error frames; open Webview Developer Tools from the command palette |
| Send Selection does nothing | No text selected | Ensure text is selected in the editor before pressing the shortcut |
| File links don't open | File path not in workspace | Check that the referenced file exists |
| Rewind bar missing | `jiuwenswarm.rewindEnabled` is false, or edit was rejected | Enable rewind in settings |
| Rewind restores 0 files | Snapshots cleared by a subsequent message | Click Undo immediately after the turn ends |
| "Loading history…" never disappears | Server did not send `history.done` | Reconnect via the status bar; check server logs |
| Session list stays on "Loading…" | Server timeout or `session.list` not supported | Click ↺ Retry; check server logs |
| Skills list shows error | Server does not support `skills.list` | Expected on older server versions; upgrade the server |

### Reading extension logs

1. Open **View → Output** (`Ctrl+Shift+U` / `⌘⇧U`).
2. Select **JiuwenSwarm** from the dropdown.

For webview JavaScript errors:

1. Run **Developer: Open Webview Developer Tools** from the command palette.
2. Check the **Console** tab.

---

## Swarm Map

The Swarm Map is a dedicated webview panel that provides a real-time visual overview of an
active `code.team` session — showing every worker agent, their current task, live file
activity, inter-agent messages, and overall progress. It opens automatically beside the
chat panel when the first team agent spawns.

### Three views — Map (default), List, and Board

Use the **Map / List / Board** toggle in the panel header to switch between them:

- **Map view** — an interactive "agent map". Every worker is a coloured node on a canvas
  arranged along a pipeline arc. Nodes **pulse and glow** while working, show a **✓** when
  done and **⏸** when paused, and animated dots flow along curved lines from one agent to
  the next as work moves down the pipeline. **Drag to pan, scroll to zoom, double-click
  to auto-fit**, and **click an agent** to open a detail card (name, role, status, live
  elapsed time, current action, recent steps).
- **List view** — the technical per-agent feed: one card per worker with a status chip,
  live elapsed timer, current action, and a scrollable activity feed.
- **Board view** — a three-column kanban board (Backlog / In Progress / Done). Each task
  is a card showing the title, assigned agent name with a colour dot, and a status badge
  (Blocked, Cancelled, Done). Cards update live as agents claim and complete tasks.

### Friendly status wording

The map and list describe agents in plain language rather than tool names: **Planning**,
**Writing**, **Editing**, **Exploring**, **Building**, **Coordinating**, **Thinking**,
**Generating**, **Standing by**, **Done** — always alongside a live elapsed timer (`0:42`).

### Layout (List view)

```
┌────────────────────────────────────────────────────────────┐
│ JIUWENSWARM · SWARM MAP  [Map|List]  2/4 tasks · 3 agents · 1 working │
├────────────────────────────────────────────────────────────┤
│ [⚙ Write module → coder] [✓ Plan → planner]                │
├────────────────────────────────────────────────────────────┤
│ ● planner  TEAMMATE  WORKING  0:42                         │
│   editing · plan.md                                        │
│   Task: Decompose the work                                 │
│   12:01:44  reading · plan.md                              │
│   12:01:53  editing · plan.md                              │
│ ● coder    TEAMMATE  IDLE                                  │
│   standing by                                              │
├────────────────────────────────────────────────────────────┤
│ ▶ Messages (5)                                             │
└────────────────────────────────────────────────────────────┘
```

### Progress chip and bar

The header chip (`N/M tasks · K agents · M working`) shows how many tasks have completed,
how many workers there are, and how many are actively working. A thin progress bar under
the header shows the completed-task percentage.

### Task pills (List view)

A row of pills at the top of the List view shows every task at a glance:

| Appearance | Status |
|------------|--------|
| Green border | in_progress |
| Yellow border | pending |
| Red border | blocked (waiting on a predecessor) |
| Grey, dim | completed or cancelled |

### Board view (kanban)

Switch to **Board** to see tasks as a proper kanban board. Three columns fill the panel:

```
┌──────────────────┬──────────────────┬──────────────────┐
│    Backlog  (2)  │ In Progress (3)  │    Done     (1)  │
├──────────────────┼──────────────────┼──────────────────┤
│                  │                  │                  │
│  Add auth API    │ ● coder          │ ✓ Plan tasks     │
│                  │  Write module    │                  │
│  Write README    │ ● tester         │                  │
│                  │  ⚑ Add tests     │                  │
│                  │    Blocked       │                  │
└──────────────────┴──────────────────┴──────────────────┘
```

Each card shows:

| Element | Description |
|---------|-------------|
| Task title | Full task description (wraps if long) |
| Colour dot | Matches the assigned agent's lane colour; absent if unassigned |
| Agent name | The worker the task is assigned to |
| **Blocked** badge | Red — task is waiting on another task or resource |
| **Done** badge | Green — task completed |
| **Cancelled** badge | Dim — task was abandoned |
| Strikethrough title | Applied to completed and cancelled tasks |

The board updates on every snapshot push — no manual refresh needed.

### Worker lanes (List view)

The orchestrator (`team-leader`) is the session owner, not a worker, so it is not shown.
Lanes are the actual workers:

| Element | Description |
|---------|-------------|
| Pulsing green dot | Agent is WORKING — actively running tools |
| Grey dot | Agent is IDLE — standing by |
| Amber dot | Agent is PAUSED |
| Faded card | Agent is DONE (SHUTDOWN) |
| Status chip | WORKING / IDLE / PAUSED / DONE word next to the name |
| Elapsed timer | Live `0:42` time since the agent became active |
| Activity line | Current operation: `writing · tasks.py`, `running · npm run build`, … |
| Activity feed | Last ~8 distinct steps, each as `HH:MM:SS text` (consecutive duplicates are collapsed) |
| ⚠ idle Ns | Agent has been WORKING but silent for more than 30 seconds. During model calls the lane instead shows **Thinking…** / **Generating…**, so this warning only appears when work is genuinely stalled |

### Lane click → jump to file

When an agent has recently touched a file, hovering over its card shows an **↗ open file**
hint. Click the card to open that file in the VS Code editor. Focus moves immediately.

### Inter-agent message log

When agents send messages to each other (`team.message.*` events), a **▶ Messages (N)**
toggle appears at the bottom of the panel. Click to expand a scrollable log:

```
planner  →  coder    implement add_task(title), write to tasks.json…
planner  →  tester   write unit tests for all four operations…
coder    →  tester   tasks.py is done, file is at /…/tasks.py
```

Sender names are colour-coded to match their lane card. The log holds the last 50 messages
and auto-scrolls to the newest entry.

### Summary card

When every worker reaches DONE, the live lane cards are replaced by a session summary,
including each agent's total duration:

```
✓ plan-code-review · Session complete
Agents              3
Tasks completed     3
Messages exchanged  9
plan-agent    · 0:58
code-agent    · 1:24
review-agent  · 0:49
```

### Debug console

The ☰ menu in the header contains **Debug log** (closed by default). Enable it to see a
live, timestamped log of every event driving the map — raw team events (`team.event: …`)
and tool attribution (`tool: … · member`) — with **Clear** and **Copy** buttons.

### Swarm Map troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Swarm Map never opens | Server not emitting `team.member.spawned` | Check Output → JiuwenSwarm for `team.event:` lines; verify server sends team events |
| Lane cards appear but no file activity | `member_name` missing from `chat.tool_call` payload | Confirm server includes `member_name` in tool call events |
| Only workers appear (no leader lane) | Expected — the orchestrator is filtered out | This is by design; workers are the lanes |
| Messages toggle never appears | `team.message.*` events missing or have no `content` field | Check server event schema |
| ↗ open file hint not shown | Agent has not called any file tool yet | Wait for first `read_file`, `write_file`, or `str_replace_editor` call |
| Click navigates to wrong file | File path in event is server-absolute but doesn't match workspace | Ensure server sends absolute paths matching the local project root |
