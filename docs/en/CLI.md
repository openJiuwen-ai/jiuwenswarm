## CLI / Channel Control Commands

JiuwenSwarm supports **special prefix commands** to control sessions and modes. These commands are parsed by the Gateway's `MessageHandler` and **are not sent to the Agent**.

---

### Supported Channels

The following IM channels support control commands:

- `feishu`
- `xiaoyi`
- `dingtalk`
- `whatsapp`
- `wechat`

---

### 1. `/new_session` — New Session ID

**Behavior**

- Generates a new `session_id` for the current channel, formatted as `{channel_type}_{ms_timestamp_hex}_{random_hex}`
- All subsequent chat messages from this channel will be forced to use the new `session_id`

**Usage**

Send in a supported channel:

```text
/new_session
```

![](../assets/images/命令行解析.png)

The Gateway will:
1. Intercept this message (not forwarded to the Agent)
2. Cancel any tasks currently running in the session
3. Generate a new `session_id` for that `channel_id`
4. Reply with a system message, e.g.: `[Received CLI command], session_id changed to feishu_17f2b4b32e0_ab12cd`

---

### 2. `/mode` — Switch Channel Mode

**Behavior**

Sets the working mode for the current channel. The Agent uses this when constructing prompts and behavior strategies.

**Primary Modes (mapped to secondary modes):**

| Command | Maps to | Description |
|---------|---------|-------------|
| `/mode agent` | `agent.plan` | Agent mode, planning/explanation/decomposition |
| `/mode auto` | `auto` | MACRO Auto: classify Agent vs Cluster on each message |
| `/mode code` | `code.normal` | Code mode, Agent interacts via code execution tools |
| `/mode team` | `team` | Team mode |

**Direct Secondary Modes:**

| Command | Description |
|---------|-------------|
| `/mode agent.plan` | Agent mode + planning style (default) |
| `/mode agent.fast` | Agent mode + auto-execution style |
| `/mode code.normal` | Code mode + direct execution style (default) |
| `/mode code.team` | Code mode + team style |

> Note: this table is the Gateway controlled-channel whitelist. The TUI local `/mode` command also supports `/mode plan` (equivalent to `agent.plan`) and `/mode team.normal` (equivalent to `team`); those forms are not recognized by Gateway controlled channels.

**Usage**

```text
/mode agent
```

The Gateway will:
1. Intercept this message
2. Cancel tasks in the current session (if mode changes)
3. Update `ChannelControlState.mode`
4. Reply with a system message, e.g.: `[Received CLI command], mode changed to code.normal`

---

### 3. `/switch` — Switch Secondary Mode

**Behavior**

Switches secondary style within the current primary mode, more concise than `/mode`.

| Command | When in agent mode | When in code mode |
|---------|--------------------|--------------------|
| `/switch plan` | → `agent.plan` | Not supported |
| `/switch fast` | → `agent.fast` | Not supported |
| `/switch normal` | Not supported | → `code.normal` |
| `/switch team` | Not supported | → `code.team` |

> This section describes the Gateway-controlled `/switch` command used by IM channels. The TUI has a different, conditionally registered command with the same name: when launched under `agentos-tui` supervision (`AGENTOS_TUI_SUPERVISED=1`), `/switch claude` hands the current task off to the Claude TUI and `/switch list` shows available handoff targets. A standalone TUI does not register that handoff command. In TUI, use `/mode ...` or `/plan` for mode switching.

**Usage**

```text
/switch plan
```

---

### 4. `/skills list` — List Available Skills

**Behavior**

Queries the currently available skill list.

**Usage**

```text
/skills list
```

The Gateway will call `skills.list` and reply with the skill list as a notification.

---

### 5. `/branch` — Fork Session

**Behavior**

Forks a new session from the current one, preserving the original conversation history. Useful for exploring new directions without affecting the original session.

**Usage**

```text
/branch
```

Or with a custom name:

```text
/branch fix login issue
```

The Gateway will:
1. Call `session.fork` to create a new session
2. Switch to the new session
3. Reply with a message, e.g.: `[Received /branch command] Session "fix login issue" forked, now switched to new session.`

---

### 6. `/rewind` — Rewind Conversation

**Behavior**

Rewinds the current session to a specified turn, deleting that turn and all subsequent conversation records.

**Usage**

First send the rewind request:

```text
/rewind 3
```

The Gateway will reply with a confirmation prompt:

```
[Received /rewind 3 command] Confirm rewind to turn 3?
This operation is irreversible and will delete turn 3 and all subsequent conversations.
Please reply /rewind confirm 3 to confirm, or /rewind cancel to cancel.
Note: Rewind does not affect manually edited files or commands executed via bash.
```

Confirm execution:

```text
/rewind confirm 3
```

Cancel operation:

```text
/rewind cancel
```

---

### 7. TUI: `/workspace` — Project Scope and Trusted Directories

**Scope:** Terminal UI (`jiuwenswarm-tui`) only; parsed locally, not by the Gateway control pipeline.

**Behavior**

- **`/workspace`** or **`/workspace get`**: show the system workspace, current project scope, and trusted directories for that project.
- **`/workspace add [path]`**: add a trusted directory; defaults to the current directory.
- **`/workspace set <path>`**: switch the in-process project scope to `<path>` and add it to that project's trusted directories. Example: `/workspace set C:\Projects\my-app`.
- **`/workspace remove <path>`**: remove a trusted directory from the current project.
- **`/workspace clear`**: clear trusted directories for the current project and fall back to the system workspace.
- Aliases: **`/workspace_dir`**, **`/workspace-dir`**.

**Persistence**

- Trusted directories are stored per project in **`~/.jiuwenswarm-tui/config.json`** under `trustedDirs`.
- The project-scope override selected by `/workspace set` lasts only for the current TUI process; after restart, the scope is derived from the launch directory again.

**Gateway / Agent**

- TUI includes the current **`trusted_dirs`**, **`project_dir`**, and **`cwd`** in WebSocket request parameters. Gateway and AgentServer use them for project identity, runtime context, and file-access policy.

---

### 8. `/compact` — Context Compression

**Scope:** TUI only; triggers context compression via AgentServer.

**Behavior**

- Actively triggers context compression to clean up conversation history while keeping summary information in context.
- TUI sends `command.compact` request to AgentServer.

**Usage**

```text
/compact
```

**Return Values**

- `busy`: Compression is already in progress, please try again later.
- `compressed`: Compression successful, displays token count before/after compression and savings percentage.
- `noop`: No compression needed, context is already optimized.

---

### Configuration Notes

- Mode is stored **per channel** (`channel_id` → `mode`). All subsequent messages on that channel will automatically include the current mode.
- `default_mode` can be set in `config.yaml` as the initial value; `MessageHandler` reads it on startup.
- `/new_session` and `/mode` changes will automatically cancel tasks currently running in the session.

---

## Terminal CLI: `jiuwenswarm chat`

Starting from v0.2.3, JiuwenSwarm provides a first-party command-line chat entry point to interact with JiuwenSwarm directly from the terminal.

### Quick Start

```bash
# Start Gateway and AgentServer (if not running)
jiuwenswarm-start app

# Send a message
jiuwenswarm chat "Hello, introduce yourself"
```

`jiuwenswarm chat` calls JiuwenSwarm's runtime through the Gateway's `/tui` WebSocket route (`channel_id="tui"`), sharing the same MessageHandler and AgentServer path as the TUI.

### Basic Usage

| Command | Description |
|---|---|
| `jiuwenswarm chat "hello"` | Send a single message |
| `jiuwenswarm chat check the repo and suggest` | Multi-word args joined into one prompt |
| `echo "analyze main.py" \| jiuwenswarm chat` | Pipe stdin |
| `jiuwenswarm chat` | No args + interactive TTY → enter REPL mode |

Single-message, piped, and JSON/JSONL invocations are non-interactive, so the
runtime does not expose the `ask_user` tool to the Agent. Use REPL mode without
a prompt argument when the Agent needs to ask follow-up questions or present options.

### Options

| Option | Default | Description |
|---|---|---|
| `--mode <mode>` | `code.normal` | Execution mode, see [Modes](Modes.md) |
| `--session <id>` | Auto-generated | Reuse or create a named session id |
| `--cwd <path>` | Current dir | Working directory for file mentions and agent context |
| `--project-dir <path>` | `--cwd` | Project identity for session and agent cache |
| `--trusted-dir <path>` | `--project-dir` | Trusted directory (repeatable) |
| `--gateway-url <url>` | `ws://127.0.0.1:19001/tui` | Explicit Gateway WebSocket URL |
| `--name <instance>` | — | Named instance for env isolation |
| `--dotenv <path>` | — | Path to .env file |
| `--json` | — | Print one final JSON object |
| `--jsonl` | — | Print each Gateway event frame as JSON Lines |
| `--show-reasoning` | — | Include reasoning output (to stderr) |
| `--show-tools` | — | Include compact tool call/result status (to stderr) |
| `--timeout <seconds>` | — | Total response timeout in seconds |

### Modes (`--mode`)

Supported mode values match those in [Modes](Modes.md) and TUI `/mode` command:

| Mode | Alias | Description |
|---|---|---|
| `code.normal` | `code` | Default, code normal mode |
| `code.plan` | — | Code planning mode |
| `code.team` | — | Code team mode |
| `agent.plan` | `agent` | Agent planning mode |
| `agent.fast` | — | Agent fast mode |
| `auto` | `agent.auto`, `macro.auto` | MACRO Auto: Agent vs Cluster per query |
| `team` | — | Team mode |

```bash
# Using aliases
jiuwenswarm chat --mode agent "help me plan"
jiuwenswarm chat --mode auto "Spawn a team of 6 agents to summarize this repo"
jiuwenswarm chat --mode code "help me analyze the code"

# Using canonical values
jiuwenswarm chat --mode code.plan "design a user system"
```

### Session Reuse

```bash
# First: specify a session name
jiuwenswarm chat --session project-a "analyze the project architecture"

# Follow-up: reuse the same session, preserving context
jiuwenswarm chat --session project-a "any improvement suggestions?"

# Without --session, auto-generates cli-YYYYMMDD-HHMMSS-xxxxxxxx
```

### REPL Mode

Run without a prompt argument to enter multi-turn conversation:

```bash
jiuwenswarm chat
# Session: cli-20260616-120500-abc12345
# > show me the files in the current directory
# > analyze main.py
# > /exit
```

All messages in REPL mode share the same session, maintaining continuous context.

**Exit the REPL** with any of:

- `/exit`, `/quit`, or `/q`
- `Ctrl+D` (Unix) / `Ctrl+Z`+Enter (Windows)
- `Ctrl+C` at the input prompt

### Loading Spinner

While waiting for the first response, a dynamic loading indicator displays on stderr:

```
✢ analyzing (3s)
```

- **12-frame ping-pong animation**: `␣ · ✢ ✳ ✶ ✻ ✽` forward then reverse
- **Random verb per turn**: analyzing, thinking, planning, exploring, searching, reading, computing, processing, generating, understanding, writing, compiling, checking, optimizing, learning
- **Elapsed timer**: shows after 1 second
- **Stall detection**: glyph turns red after 3s of no new content
- Auto-clears on first delta

### Output Modes

| Mode | Flag | Description |
|---|---|---|
| Human-readable (default) | — | Stream deltas to stdout |
| JSON | `--json` | Buffer all events, output one final JSON object |
| JSONL | `--jsonl` | Output each event frame as a JSON Line |

```bash
# JSON output
jiuwenswarm chat --json "analyze README"
# → {"ok": true, "content": "..."}

# JSONL output (pipe-friendly)
jiuwenswarm chat --jsonl "analyze README" | jq
```

### Interrupts

| Action | Behavior |
|---|---|
| First Ctrl+C | Sends `chat.interrupt` to Agent, graceful cancel. In REPL, stays in the loop for the next prompt |
| Second Ctrl+C | Force exit (exit code 130) |

> **Windows note**: `loop.add_signal_handler` is not supported on Windows. The CLI falls back to `signal.signal(SIGINT, ...)` so Ctrl+C still triggers graceful cancel (sends `chat.interrupt`) instead of raising an unhandled `KeyboardInterrupt`. Two rapid Ctrl+C presses force-exit on Windows as well.

### Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Agent returned an error |
| 2 | CLI usage or invalid mode |
| 3 | Gateway unavailable |
| 4 | The current invocation does not support interactive input |
| 130 | Interrupted by user |

### Relationship with TUI

`jiuwenswarm chat` reuses the TUI's `/tui` route (`channel_id="tui"`), sharing the same MessageHandler and AgentServer pipeline. Only one TUI/CLI connection per `channel_id="tui"` is active at a time — opening the TUI while `jiuwenswarm chat` is running (or vice versa) will replace the previous WebSocket client on that channel. The first version of the terminal CLI does not aim to replicate all TUI slash commands.
