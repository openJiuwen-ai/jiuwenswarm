# JiuwenSwarm for JetBrains

The JiuwenSwarm plugin embeds an autonomous multi-agent AI coding assistant directly into PyCharm, IntelliJ IDEA, WebStorm, GoLand, and every other JetBrains IDE (2023.1+). The panel lives in a docked tool window, streams responses token-by-token, and integrates with the editor through diff review, terminal integration, file-link navigation, and the Alt+Enter quick-fix menu.

## Installation

See [JetBrainsInstall.md](JetBrainsInstall.md) for prerequisites and installation instructions.

## Configuration

Open **Settings → Tools → JiuwenSwarm**:

| Setting | Default | Description |
|---------|---------|-------------|
| Server host | `127.0.0.1` | Hostname or IP of the JiuwenSwarm server |
| Server port | `19000` | WebSocket port — connects to `ws://host:port/ws` |
| Channel ID | `ide` | Client identifier shown in server logs |
| Connect on startup | on | Open the WebSocket when the IDE starts |
| **Default mode** | `code.plan` | Mode applied to new sessions (Plan & Execute / Execute / Team Coding) |
| Auto-apply file edits | off | Apply agent edits immediately without opening the diff window |
| Require approval before edits | off | Show a confirmation prompt before applying any agent file edit |
| Run commands in IDE terminal | on | Show agent shell commands in a dedicated terminal tab |
| Keep-alive ping interval | 30 s | Seconds between WebSocket ping frames (5–300) |
| Include project tree | on | Prepend a directory listing of the project root to every message |
| Project tree max files | 200 | Max entries in the project tree listing (10–2000) |
| Load history on session switch | on | Fetch and display past messages when switching to an existing session |
| Enable checkpoint / rewind | on | Snapshot files before agent edits; show the rewind bar after each turn |
| **Git quick actions** | off | Show Commit / Push buttons below the message list |

## Keyboard Shortcuts

| Action | Win / Linux | Mac |
|--------|-------------|-----|
| New session | `Ctrl+Shift+J` | `⌘⇧J` |
| Send selection to chat | `Ctrl+Shift+E` | `⌘⇧E` |
| Fix with JiuwenSwarm | `Alt+Enter` | `⌥Enter` |

## What the Panel Contains

### Chat input

Type your message in the textarea at the bottom. Three typing shortcuts trigger inline pickers:

| Trigger | What appears |
|---------|-------------|
| `@` | File autocomplete — picks from workspace files and injects the full file content |
| `#` | Skill picker — lists registered skills and inserts the skill name |
| `!` | Preset prompts — eight built-in templates (Explain, Fix bug, Write tests, Refactor, Optimize, Document, Review, Implement) |

Use **Arrow keys** to navigate, **Enter** or **Tab** to select, **Escape** to dismiss. **Enter** sends the message; **Shift+Enter** inserts a newline.

Attach images with the **+** button (PNG, JPEG, WebP, GIF; up to 10 MB each).

### Mode selector

| Mode | Key | Behaviour |
|------|-----|-----------|
| Plan & Execute | `code.plan` | Agent explores and proposes a plan; waits for your confirmation before editing files |
| Execute | `code.normal` | Agent edits files and runs commands directly |
| Team Coding | `code.team` | Leader agent assigns sub-agents in parallel for large tasks |

### Message list

- Responses stream token-by-token with full Markdown rendering (code blocks, tables, lists, headings).
- **Thinking blocks** — extended model reasoning appears in a collapsible section above the response.
- **Tool call cards** — every tool call is shown inline with a live status indicator, collapsible inputs, and collapsible output.
- **File links** — file paths in agent responses are clickable and open the file at the referenced line.
- **Symbol links** — PascalCase and SCREAMING_SNAKE_CASE identifiers in backticks are purple links that jump to the symbol in the project.

### Stats and metrics

- **Context bar** — a thin bar below the input shows how full the model's context window is.
- **Token counter** — per-turn token count next to the send button; session total in the IDE status bar widget.
- **Session stats chips** — total turns, tokens, cost, tool calls, average latency, and a tools breakdown chip.
- **Mini charts** — collapsible bar charts showing tokens and duration per turn.
- **Server memory** — live server RAM usage shown in the stats bar (polled every 10 s).

### Context automatically sent with every message

| Field | Source |
|-------|--------|
| Active file path and language | `FileEditorManager` + `FileType` |
| Cursor line | `Editor.caretModel` |
| Selected code | `Editor.selectionModel` |
| Editor diagnostics (up to 10) | Document markup model |
| Other open tabs (up to 10) | `FileEditorManager.openFiles` |
| Project tree (2-level) | `LocalFileSystem` traversal |
| Git branch and change count | `git` subprocess |
| Project rules | `.jiuwenswarm/instructions.md`, `.jiuwenswarm/rules.md`, or `AGENTS.md` |
| @-mentioned files | Full file contents for each `@path` typed in the message |

### File edit workflow

When the agent calls a file-editing tool (`str_replace_editor`, `write_file`, `create_file`), the plugin intercepts it:

- **Default**: a side-by-side diff window opens (Current vs Proposed). Close the window to apply.
- **Auto-apply**: edits are written immediately via `WriteCommandAction` (undoable with `Ctrl+Z`).
- **Require approval**: a confirmation prompt appears before opening the diff or applying.

### Terminal integration

Agent shell commands (`bash`, `run_command`) run in a dedicated **JiuwenSwarm** terminal tab so output is visible and scrollable. The terminal is created on the first command and reused. Disable in settings to run commands silently.

### Checkpoint / rewind

After any turn that edits files, a rewind bar appears below the message list. Click **Undo Changes** to restore all modified files to their state before that turn. Snapshots are cleared when you send the next message.

### Git quick actions

Enable **Git quick actions** in settings to show **Commit** and **Push** buttons below the message list. **Commit** opens a dialog pre-filled with your last message as the commit message, runs `git add -u && git commit -m <message>`. **Push** runs `git push` immediately.

### Sessions

Click **⚙ → Sessions** to open the session list. Switch, create (New button or `Ctrl+Shift+J`), or delete sessions. With **Load history on session switch** on, past messages stream in automatically after switching.

### Skills

Click **⚙ → Skills** to view registered skills. Each skill shows its name, description, trigger, and an ON/OFF toggle. Type `#` in the chat input to pick a skill without opening the overlay.

### Swarm Map

Available when using **Team Coding** mode (`code.team`). Opens automatically at the bottom of the IDE when the first team agent spawns — no click required.

The panel has three views, switched from the **Map / List / Board** toggle in the header (Map is the default):

| Element | What it shows |
|---------|---------------|
| Map view | Interactive canvas: agents as coloured nodes, pulsing while working, ✓ when done; animated flow dots; drag to pan, scroll to zoom, click for details |
| List view | One lane card per worker — status chip, live elapsed timer, current action, scrollable activity feed |
| Board view | Three-column kanban (Backlog / In Progress / Done); one task card per task with agent colour dot and Blocked/Done/Cancelled badge |
| Progress chip / bar | `N/M tasks · K agents · M working` + completed-task percentage bar |
| Task pills (List) | One pill per task in List view; colour shifts from yellow (pending) → green (in_progress) → red (blocked) → grey (completed) |
| ↗ open file | Hover a lane card that has an active file to see the hint; click to open the file in the editor |
| Message log | Collapsible `▶ Messages (N)` toggle showing inter-agent messages, colour-coded by sender |
| Debug console | ☰ menu → **Debug log** (closed by default): live team-event and tool-attribution log with Clear/Copy |
| Summary card | Replaces lanes when all workers finish: agent count, tasks completed, messages, per-agent durations |

See [JetBrainsGuide.md — Swarm Map](JetBrainsGuide.md#swarm-map) for the full reference.

## Alt+Enter Quick Fix

Place the cursor on any error or warning and press **Alt+Enter**. **Fix with JiuwenSwarm** appears in the menu. It prefills the chat with the error message and ±7 lines of surrounding code.

## Connection Status Bar

The IDE status bar shows live WebSocket state:

| Symbol | Meaning |
|--------|---------|
| `⬤ JiuwenSwarm` (teal) | Connected |
| `◌ JiuwenSwarm` (teal) | Connecting |
| `↻ JiuwenSwarm` (yellow) | Reconnecting (exponential back-off: 1 s → 30 s max) |
| `○ JiuwenSwarm` (grey) | Disconnected — click to reconnect |

Token total appears next to the label once tokens are consumed: `⬤ JiuwenSwarm · 42.3k`.

## Project Rules

Create `.jiuwenswarm/instructions.md` (or `.jiuwenswarm/rules.md` or `AGENTS.md`) in the project root. The plugin reads the first file it finds and prepends its contents to every message under **Project rules:**. Use it for per-project coding standards, forbidden patterns, or preferred libraries.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Panel is blank | Enable `ide.browser.jcef.enabled` via **Help → Find Action → Registry**, then restart |
| Status bar shows `○` | Start JiuwenSwarm; verify host/port in settings; click widget to reconnect |
| No response streams | Enable Debug log (**⚙ → Debug log**) and check for error frames |
| Diff window opens but file unchanged | Close the diff window — the change applies on close, not immediately |
| Rewind bar missing | Check **Enable checkpoint / rewind** in settings; verify turn completed normally |
| Alt+Enter does not show the option | Cursor must be on a line with a red or yellow gutter marker |
