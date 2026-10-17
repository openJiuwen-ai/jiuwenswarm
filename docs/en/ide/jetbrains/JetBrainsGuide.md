# JiuwenSwarm JetBrains Plugin — Usage Guide

Complete reference for every setting, panel element, and workflow. For installation see [JetBrains.md](JetBrains.md).

---

## Configuration

Open **Settings → Tools → JiuwenSwarm**.

| Setting | Default | Description |
|---------|---------|-------------|
| Server host | `127.0.0.1` | Hostname or IP of the JiuwenSwarm WebSocket server |
| Server port | `19000` | Port — connects to `ws://host:port/ws` |
| Channel ID | `ide` | Client identifier shown in server logs and traces |
| Connect on startup | on | Open the WebSocket when the IDE starts |
| **Default mode** | `code.plan` | Mode applied when a new session is created (Plan & Execute / Execute / Team Coding). Applied to the UI on every connection and can be overridden per-session by the mode selector. |
| Auto-apply file edits | off | Apply agent file edits immediately without opening the diff review window |
| Require approval before edits | off | Show a confirmation prompt before applying any agent file edit |
| Run commands in IDE terminal | on | Route agent shell commands to a dedicated JiuwenSwarm terminal tab |
| Keep-alive ping | on | Send periodic WebSocket ping frames to prevent server-side timeout |
| Keep-alive interval | 30 s | Seconds between pings (5–300) |
| Include project tree | on | Prepend a 2-level directory listing of the project root to every message |
| Project tree max files | 200 | Maximum file entries in the tree listing (10–2000) |
| Load history on session switch | on | Fetch and display past messages after switching to an existing session |
| **Enable checkpoint / rewind** | on | Snapshot files before agent edits; show the rewind bar at the end of each turn |
| **Git quick actions** | off | Show Commit and Push buttons below the message list |

Settings are stored in `jiuwenswarm.xml` in the IDE config directory and persist across restarts.

---

## Opening the Panel

Click **JiuwenSwarm** in the right sidebar tool window. The panel opens as a docked JCEF (embedded Chromium) tool window and can be resized, moved, or floated like any other tool window.

A session is created automatically on first connect. The header shows the session title and live connection state.

---

## Header Bar

```
● Session title                    [New] [⚙]
```

| Element | Description |
|---------|-------------|
| Status dot | Grey until the first connection, then green = connected, yellow (pulsing) = reconnecting, red = disconnected. To reconnect after a disconnect, click the status-bar widget (`○ JiuwenSwarm`) or use **New**. |
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

Click the mode pill to open the dropdown. If the current session already has messages, switching mode asks for confirmation and starts a new session. The default mode applied at startup comes from **Settings → Default mode**.

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

Type `@` followed by part of a filename to search workspace files. Selecting a file inserts `@relative/path/to/file` into the message. When sent, the plugin reads the file and includes its full contents in the context under a fenced code block.

**`#` — skill picker**

Type `#` to see all registered skills. Continue typing to filter by name. Selecting a skill inserts `#skill-name` into the message. Skills listed here are the same ones shown in the Skills overlay.

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

Each completed turn's footer shows that turn's token counts, tool calls, error categories, and duration.

The bar chart icon (right side of the stats bar, shown after two or more turns) toggles **mini charts** — bar graphs of tokens and duration per turn. Hover a bar to see that turn's details.

At the bottom of the panel, the input area contains a **context bar** showing how full the active model's context window is (0–100%). It turns orange above 60% and red above 80%; a warning chip appears as the context approaches the server's auto-compaction threshold.

The **server memory** chip shows live JiuwenSwarm server RAM usage (RSS of total, plus available), polled every 10 seconds.

---

## IDE Context Injection

Every message has a structured context block prepended before being sent. The agent sees it as part of your message — no manual copy-pasting required.

### What is injected

| Field | Source |
|-------|--------|
| Active file path and language | `FileEditorManager` + `FileType` |
| Cursor line | `Editor.caretModel` |
| Selected code | `Editor.selectionModel` (if non-empty) |
| Diagnostics (up to 10) | Document markup model (error-stripe highlighters) |
| Other open tabs (up to 10) | `FileEditorManager.openFiles` |
| Project tree (2-level) | `LocalFileSystem` traversal; skips `.git`, `build`, `node_modules`, `target`, `.venv`, etc. |
| Git branch + change count | `git rev-parse` + `git status --porcelain` subprocess |
| Project rules | First non-empty file found: `.jiuwenswarm/instructions.md`, `.jiuwenswarm/rules.md`, `AGENTS.md` |
| @-mentioned files | Full file content for each `@path` typed in the message |

### Project rules

Create a file at the project root to inject standing instructions into every message:

```
.jiuwenswarm/instructions.md   ← checked first
.jiuwenswarm/rules.md          ← checked second
AGENTS.md                      ← checked third
```

Use it to define coding style, forbidden patterns, preferred libraries, or any project-specific context the agent should always know.

### Controlling what is injected

| Setting | Effect |
|---------|--------|
| **Include project tree** | Toggle the directory listing on or off |
| **Project tree max files** | Limit entries (10–2000) for large mono-repos |

### Example context block

````
<!-- IDE Context -->
Active file: /Users/mishka/project/src/api/handler.py  (Python)
Cursor line: 87

Selected code:
```
def handle_request(req):
    result = blocking_call(req)
    return result
```

Diagnostics (2):
  • Variable 'result' is not used before return (line 87)
  • blocking_call is deprecated

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

File paths in agent responses become clickable links. Clicking opens the file in the editor at the referenced line.

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
| New session | `Ctrl+Shift+J` | `⌘⇧J` |
| Send selection | `Ctrl+Shift+E` | `⌘⇧E` |
| Fix with JiuwenSwarm | `Alt+Enter` | `⌥Enter` |

**New session** — opens the panel if not already visible, then reconnects the WebSocket to start a fresh session. The message list clears.

**Send selection** — opens the panel and pre-fills the input with the selected code, labelled with the file name:

````
[File: handler.py]
```
def handle_request(req):
    ...
```
````

Add your question after the code and press Enter.

**Right-click → Send Selection to JiuwenSwarm** — identical to `Ctrl+Shift+E`.

---

## Alt+Enter Quick Fix

Place the cursor on any error or warning gutter marker and press **Alt+Enter**. **Fix with JiuwenSwarm** appears in the lightbulb menu.

What happens:

1. The error tooltip text is read (HTML stripped).
2. ±7 lines of surrounding code are captured.
3. The panel opens and the input is pre-filled:

````
Fix this error in handler.py:

Error:
Variable 'result' is not used before return

```py
def handle_request(req):
    result = blocking_call(req)
    return result
```
````

Works for any language PyCharm supports — Python, Kotlin, Java, TypeScript, Go, Rust, and so on.

---

## File Edit Workflow

When the agent calls `str_replace_editor`, `write_file`, or `create_file`, the plugin intercepts it:

### Default: diff review

A side-by-side diff window opens — **Current** (left) vs **Proposed** (right) — so you can review the change. This dialog is preview-only: closing it does not write the change to the file. To have agent edits applied to your files, enable **Auto-apply file edits** in settings, or apply the proposed change manually.

If the target text for a `str_replace` cannot be located, a balloon notification explains the problem.

### Auto-apply

Enable **Auto-apply file edits** to skip the diff dialog. Changes are applied via `WriteCommandAction` and are undoable with `Ctrl+Z`. A balloon notification confirms each edit.

### Require approval

Enable **Require approval before edits** to show a confirmation prompt before any edit is applied or the diff dialog opens. Useful when you want to see every proposed change before committing.

---

## Terminal Integration

Agent shell commands (`bash`, `run_command`) are routed to a **JiuwenSwarm** terminal tab in the Terminal tool window. The tab is created on the first command and reused for all subsequent ones. The Terminal tool window comes to the front automatically.

Disable **Run commands in IDE terminal** in settings to skip mirroring agent commands into the IDE. The agent still runs its commands on the server — the IDE only reflects them when this setting is on.

---

## Checkpoint / Rewind

After any agent turn that edits files, the rewind bar appears below the message list:

```
⟲ Agent edited files this turn    [⟲ Undo changes]
```

### How it works

Before the agent's first edit to a file in a given turn, the plugin snapshots that file's current content. At the end of the turn (`chat.final`) the snapshots are locked in. If no edits were made, the bar does not appear.

### Using rewind

Click **⟲ Undo changes**. The plugin restores every snapshotted file using `WriteCommandAction`. Files that did not exist before the turn are deleted. The restore is itself undoable with `Ctrl+Z`.

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

Disable rewind entirely via **Enable checkpoint / rewind** in settings (reduces memory usage during long agent runs).

---

## Git Quick Actions

Enable **Git quick actions** in settings to show a toolbar below the message list with two buttons:

**Commit** — opens a dialog pre-filled with your last sent message as the commit message (prefixed with "AI: "). Clicking OK runs `git add -u && git commit -m <message>`. The git bar updates after commit.

**Push** — runs `git push` in the background. Status updates on completion.

The git bar shows the current branch and the number of uncommitted files. It updates automatically after each agent turn.

---

## Sessions

### Opening the overlay

Click **⚙ → Sessions** in the header. The overlay slides over the message list.

### What the list shows

Each row shows:
- Session title (or raw session ID if untitled)
- Time of last message (relative: "just now", "3m ago", "2h ago", "5d ago")
- Message count

### Switching

Click a row to switch. With **Load history on session switch** on, past messages stream in automatically. A **"Loading history…"** indicator appears until the stream is complete.

### Creating

Click **New** in the header or press `Ctrl+Shift+J` / `⌘⇧J`.

### Deleting

Click **✕** on a non-active session row. The button turns red and the tooltip changes to "Click again to confirm." Click a second time within 2 seconds to confirm deletion. The active session cannot be deleted — switch to another session first.

### Refreshing

Click **↺** in the overlay header to reload the list. Up to 20 sessions are shown.

---

## Skills

### Opening the overlay

Click **⚙ → Skills** in the header.

### What the list shows

Each skill row shows:
- Name and description
- Trigger (the slash command used in-session, e.g. `/commit`)
- ON/OFF toggle

### Toggling

Click ON or OFF. The change is sent immediately to the server via `skills.toggle`. Teal = enabled; muted grey = disabled.

### Picking a skill from the input

Type `#` in the chat textarea. A popup lists all loaded skills. Continue typing to filter. Select with Enter or Tab — the skill name is inserted at the cursor. Skills are loaded the first time `#` is typed; the list is cached for the session.

---

## Connection Status Bar Widget

| Widget | Meaning |
|--------|---------|
| `⬤ JiuwenSwarm` (teal) | Connected |
| `◌ JiuwenSwarm` (teal) | Connecting |
| `↻ JiuwenSwarm` (yellow) | Reconnecting — exponential backoff: 1 s → 2 s → 4 s → … → 30 s max |
| `○ JiuwenSwarm` (grey) | Disconnected — click to reconnect |

Token total appears next to the label once tokens are consumed:

```
⬤ JiuwenSwarm · 42.3k
```

Hover for a tooltip with session ID and full token count.

---

## Theme

| Option | Description |
|--------|-------------|
| **⚙ → ◐ Auto** | Follows the IDE's light or dark theme (default) |
| **⚙ → 🌙 Dark** | Forces dark regardless of IDE theme |
| **⚙ → ☀ Light** | Forces light regardless of IDE theme |

Stored in browser local storage inside JCEF; survives panel restarts.

---

## Model Selector

When connected to a server that has multiple models configured, a model dropdown appears in the input bar. Click to open it and switch models. The active model is shown in the mini model chip. Model availability depends on the server configuration.

---

## Debug Log

Click **⚙ → Debug log** to open a scrollable log panel below the message list. It records:

- Every WebSocket frame received (raw JSON with timestamp)
- Every message sent (content, context size, media item count)
- Session switches, reconnects, connection state transitions
- Action dispatches (list_sessions, list_skills, toggle_skill, etc.)
- File edit tool calls (tool name and parameters)
- Snapshot events (`SNAP →`) and rewind operations

The panel keeps the most recent 500 lines. Toggle off to hide the panel; use **Clear** to empty the log (its content persists across toggles).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Panel shows blank | JCEF not enabled | Enable `ide.browser.jcef.enabled` in **Help → Find Action → Registry**; restart |
| Status bar shows `○` | Server not running or wrong host/port | Start JiuwenSwarm; verify settings; click status widget to reconnect |
| Messages send, no response | Server unreachable after handshake | Enable Debug log; look for error frames; check server logs |
| Diff window opens but file unchanged | The diff dialog is preview-only | The proposed change is not applied automatically — enable **Auto-apply file edits** or apply it manually |
| Rewind bar missing | Rewind disabled, or turn ended without file edits | Check **Enable checkpoint / rewind** in settings; enable Debug log and look for `SNAP →` lines |
| Rewind restores 0 files | Snapshots cleared by a subsequent message | Click Undo immediately after the turn; snapshots survive until the next send |
| Alt+Enter does not show the option | No error at cursor | Move cursor onto a line with a red or yellow gutter marker |
| Skills popup shows nothing | Server has no skills, or `list_skills` failed | Check server logs; the popup is empty if the server returns an empty list |
| Session history does not load | **Load history on session switch** is off, or server does not support history | Enable the setting; check server logs for `history.get` errors |
| "Loading history…" never disappears | Server did not send `history.done` | Reconnect via the status bar; check server logs |
| Session ✕ button missing | Session is active | Switch to another session before deleting |
| Project structure not in context | No project root detected | Open a project folder; `project.basePath` must be non-null |
| Git quick actions not visible | **Git quick actions** is off | Enable in Settings → Tools → JiuwenSwarm |

### Reading the IDE log

**Help → Show Log in Explorer** (Windows/Linux) or **Help → Show Log in Finder** (macOS). Search for `JiuwenSwarm`.

---

## Swarm Map

The Swarm Map is a dedicated tool window that provides a real-time visual overview of an
active `code.team` session — showing every worker agent, their current task, live file
activity, inter-agent messages, and overall progress. It opens automatically when the
first team agent spawns. To open manually: *View → Tool Windows → JiuwenSwarm Swarm*.

### Three views — Map (default), List, and Board

Use the **Map / List / Board** toggle in the tool window header to switch between them:

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
hint. Click the card to open that file in the editor. The IDE moves focus immediately.

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
| Swarm Map never opens | Server not emitting `team.member.spawned` | Check **Help → Show Log** for `team.event:` lines; verify server sends team events |
| Lane cards appear but no file activity | `member_name` missing from `chat.tool_call` payload | Confirm server includes `member_name` in tool call events |
| Only workers appear (no leader lane) | Expected — the orchestrator is filtered out | This is by design; workers are the lanes |
| Messages toggle never appears | `team.message.*` events missing or have no `content` field | Check server event schema |
| ↗ open file hint not shown | Agent has not called any file tool yet | Wait for first `read_file`, `write_file`, or `str_replace_editor` call |
| Click navigates to wrong file | File path in event is server-absolute but doesn't match local mount | Ensure server sends paths matching the local project root |
