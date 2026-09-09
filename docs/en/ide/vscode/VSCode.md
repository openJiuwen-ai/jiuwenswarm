# JiuwenSwarm for VS Code

The JiuwenSwarm extension embeds an autonomous multi-agent AI coding assistant directly into Visual Studio Code. The panel runs in the Activity Bar sidebar, streams responses token-by-token, and integrates with the editor through file edits, terminal integration, file-link navigation, and the lightbulb quick-fix menu.

## Installation

See [VSCodeInstall.md](VSCodeInstall.md) for prerequisites and installation instructions.

## Configuration

Open **Settings → Extensions → JiuwenSwarm**:

| Setting | Default | Description |
|---------|---------|-------------|
| `jiuwenswarm.host` | `localhost` | Hostname or IP of the JiuwenSwarm server |
| `jiuwenswarm.port` | `19000` | WebSocket port — connects to `ws://host:port/ws` |
| `jiuwenswarm.channelId` | `ide` | Client identifier shown in server logs |
| `jiuwenswarm.autoConnect` | `true` | Open the WebSocket when VS Code starts |
| `jiuwenswarm.defaultMode` | `code.plan` | Mode applied to new sessions (`code.plan` / `code.normal` / `code.team`) |
| `jiuwenswarm.approveEdits` | `false` | Require explicit approval before applying any agent file edit |
| `jiuwenswarm.loadHistoryOnSwitch` | `true` | Fetch and display past messages when switching to an existing session |
| `jiuwenswarm.rewindEnabled` | `true` | Snapshot files before agent edits; show the rewind bar after each turn |
| `jiuwenswarm.projectTree.enabled` | `true` | Prepend a directory listing of the workspace root to every message |
| `jiuwenswarm.projectTree.maxFiles` | `200` | Max entries in the project tree listing (10–2000) |

## Keyboard Shortcuts

| Action | Win / Linux | Mac |
|--------|-------------|-----|
| Open chat panel | `Ctrl+Shift+J` | `⌘⇧J` |
| Send selection to chat | `Ctrl+Shift+E` | `⌘⇧E` |
| Fix with JiuwenSwarm | `Ctrl+.` | `⌘.` |

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
- **Symbol links** — PascalCase and SCREAMING_SNAKE_CASE identifiers in backticks are purple links that jump to the symbol in the workspace.

### Stats and metrics

- **Context bar** — a thin bar below the input shows how full the model's context window is.
- **Token counter** — per-turn token count next to the send button; session total in the VS Code status bar.
- **Session stats chips** — total turns, tokens, cost, tool calls, average latency, and a tools breakdown chip.
- **Mini charts** — collapsible bar charts showing tokens and duration per turn.
- **Server memory** — live server RAM usage shown in the stats bar (polled every 10 s).

### Context automatically sent with every message

| Field | Source |
|-------|--------|
| Active file path and language | `vscode.window.activeTextEditor` |
| Cursor line | `editor.selection.active.line` |
| Selected code | `editor.document.getText(editor.selection)` |
| Editor diagnostics (up to 10) | `vscode.languages.getDiagnostics()` |
| Other open tabs (up to 10) | `vscode.window.tabGroups.all` |
| Project tree (2-level) | Workspace folder traversal |
| Git branch and change count | `git` subprocess |
| Project rules | `.jiuwenswarm/instructions.md`, `.jiuwenswarm/rules.md`, or `AGENTS.md` |
| @-mentioned files | Full file contents for each `@path` typed in the message |

### File edit workflow

When the agent calls a file-editing tool (`str_replace_editor`, `write_file`, `create_file`), the extension applies the edit to your workspace via Node.js `fs`. A notification toast confirms each applied change.

Enable **Approve edits** in settings to show an **Approve** / **Reject** prompt before every file change.

### Terminal integration

Agent shell commands (`bash`, `run_command`) run in a dedicated **JiuwenSwarm** terminal so output is visible and scrollable. The terminal is created on the first command and reused.

### Checkpoint / rewind

After any turn that edits files, a rewind bar appears below the message list. Click **Undo Changes** to restore all modified files to their state before that turn. Files created during the turn are deleted on rewind. Snapshots clear when you send the next message.

### Sessions

Click **⚙ → Sessions** to open the session list. Switch, create (New button / `Ctrl+Shift+J`), or delete sessions. With **Load history on session switch** on, past messages stream in automatically after switching.

### Skills

Click **⚙ → Skills** to view and toggle registered skills. Each skill shows its name, description, trigger, and ON/OFF toggle. Type `#` in the chat input to pick a skill without opening the overlay.

### Swarm Map

Available when using **Team Coding** mode (`code.team`). Opens automatically beside the chat panel when the first team agent spawns — no click required.

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

See [VSCodeGuide.md — Swarm Map](VSCodeGuide.md#swarm-map) for the full reference.

## Code Action Quick Fix

VS Code shows a lightbulb 💡 next to lines with errors or warnings. Click it (or press `Ctrl+.` / `⌘.`) and select **Fix with JiuwenSwarm**. The chat panel opens with the error message and ±7 lines of surrounding code pre-filled.

## Send Selection

Select code in any editor and press `Ctrl+Shift+E` / `⌘⇧E` (or right-click → **Send Selection to JiuwenSwarm**). The chat panel opens with the code pre-filled, labelled with the file name.

## Connection Status Bar

The VS Code status bar (bottom-right) shows live WebSocket state:

| Icon | Meaning |
|------|---------|
| `$(check)` | Connected |
| `$(loading~spin)` | Connecting |
| `$(sync~spin)` (yellow) | Reconnecting (exponential back-off: 1 s → 30 s max) |
| `$(circle-slash)` (red) | Disconnected — click to reconnect |

Token total appears next to the label: `$(check) JiuwenSwarm · 42.3k`.

## Project Rules

Create `.jiuwenswarm/instructions.md` (or `.jiuwenswarm/rules.md` or `AGENTS.md`) in the workspace root. The extension reads the first file it finds and prepends its contents to every message under **Project rules:**. Use it for per-project coding standards, forbidden patterns, or preferred libraries.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Panel is blank | Check that JiuwenSwarm is running on the configured host/port; click the status bar widget to reconnect |
| No response streams | Enable Debug log (**⚙ → Debug log**) and check for error frames; open Webview Developer Tools from the command palette |
| Send Selection does nothing | Ensure text is actually selected in the editor before pressing the shortcut |
| File links don't open | Verify the file path exists in the workspace |
| Rewind bar missing | Check `jiuwenswarm.rewindEnabled` in settings |
| Settings change did not take effect | Reload the VS Code window when prompted |
