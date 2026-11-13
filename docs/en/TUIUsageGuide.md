# JiuwenSwarm TUI Usage Guide

> Full walkthrough (CLI flags, tools, keyboard shortcuts, Code mode): Chinese [TUI 使用指南](../zh/TUI使用指南.md). Install and first run: [Quick start (TUI)](Quickstart_tui.md).

When the first message creates a draft session, Ctrl+C and supplemental input wait for creation; the TUI sends the message first and then the interrupt or supplement using the assigned session ID. Commands that need saved conversation history, such as `/rewind` and `/compact`, ask you to send a message first while the session is still a draft.

---

## Slash command reference

Top-level commands from `createBuiltinCommands()` in `jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/`. Gateway-side slash behavior: [Slash Commands Reference](SlashCommands.md).

### Command table

| Command | Aliases | Purpose | Example | Modes |
|------|------|------|------|----------|
| `/help` | - | List registered slash commands | `/help` | All |
| `/keybindings` | `/keybind` | View / edit / reset TUI keybindings | `/keybindings`, `/keybindings list` | All |
| `/hooks` | - | Browse configured hooks (read-only) | `/hooks` | All |
| `/exit` | `/quit` | Exit the TUI | `/exit` | All |
| `/clear` | `/reset` | Clear transcript and open a draft; the next message creates a session (rejected while busy) | `/clear` | All |
| `/new` | - | Open a draft; the next message creates a session. `--persist` creates a Persist Session immediately. | `/new` | All |
| `/copy` | - | Copy the Nth most recent assistant reply to clipboard | `/copy` or `/copy 2` | All |
| `/theme` | - | Switch dark / light theme | `/theme dark` | All |
| `/color` | - | Set prompt accent color | `/color blue` | All |
| `/compact` | - | Compact context, keep summary | `/compact` | All |
| `/config` | `/settings`, `/setting` | View / set backend config | `/config`, `/config get`, `/config set key value` | All |
| `/context` | - | Context window and token usage | `/context` | All |
| `/diff` | - | Interactive turn diffs + uncommitted working tree | `/diff` | All |
| `/evolve` | - | Start a Skill evolution review | `/evolve myskill fix error handling` | Agent / Team |
| `/evolve_list` | - | List evolution entries for a Skill | `/evolve_list myskill` | Agent / Team |
| `/evolve_rebuild` | - | Rebuild SKILL.md from archive and evolution records | `/evolve_rebuild myskill strengthen errors` | Agent / Team |
| `/evolve_simplify` | - | Organize / merge evolution notes for a Skill | `/evolve_simplify myskill merge duplicates` | Agent / Team |
| `/init` | - | Initialize `JIUWENSWARM.md` / `JIUWENSWARM.local.md` in **Code mode** | `/init` | **`code.*` only** |
| `/mcp` | - | Manage MCP servers | `/mcp list`, `/mcp add ...` | All |
| `/mode` | - | Switch or view mode | `/mode`, `/mode code`, `/mode team` | All |
| `/permissions` | - | Set allow / ask / deny for tools in `permissions.tools` | `/permissions ask write_file` | All |
| `/plan` | - | Enter agent planning mode or send a planning request | `/plan`, `/plan open`, `/plan migration steps` | Not `team` |
| `/rename` | - | View / rename / clear session title | `/rename`, `/rename title`, `/rename clear` | All |
| `/review` | - | Review a PR (TUI sends chat; Gateway injects prompt) | `/review`, `/review 123` | All |
| `/resume` | `/continue` | List or restore sessions; bare `/resume` / `/continue` opens the interactive picker | `/resume list`, `/resume <id>` | All |
| `/skills` | - | Skills and marketplace sources | `/skills`, `/skills install ...` | All |
| `/teamskills` | - | TeamSkills Hub (init, validate, pack, search, install, …) | `/teamskills list` | All |
| `/model` | - | View / add / switch models | `/model`, `/model add name k=v` | All |
| `/workspace` | `/workspace_dir`, `/workspace-dir` | Manage trusted directories for file ops | `/workspace add .` | All |
| `/export` | - | Export current session to file or clipboard | `/export`, `/export my-chat` | All |
| `/status` | - | Runtime overview, usage, config | `/status`, `/status usage` | All |
| `/agents` | - | Agent config (list, get, create, update, enable, disable, delete) | `/agents list`, `/agents get Explore` | All |
| `/branch` | `/fork` | Branch session from current conversation point | `/branch fix-login-bug` | All |
| `/btw` | - | Side-channel quick question without interrupting main chat | `/btw what does git status do?` | All |
| `/rewind` | `/checkpoint` | Rewind conversation before a given turn | `/rewind 2` | All |
| `/memory` | `/mem` | Memory management (status, files, toggle, dirs) | `/memory status` | All |
| `/sandbox` | - | Sandbox mode / excluded_commands / files | `/sandbox enable`, `/sandbox status`, `/sandbox files allow ./tmp/` | All |
| `/security-review` | - | Security review of pending branch changes | `/security-review`, `/security-review focus on auth` | All |
| `/simplify` | - | Code simplification review (reuse, quality, efficiency) with auto-fix | `/simplify`, `/simplify src/auth/` | **`code.*` only** |
| `/swarmflow` | - | SwarmFlow toggle / status / budget (`on` / `off` / `--budget`) | `/swarmflow on` | **Recommended `team`** |
| `/swarmflows` | `/swarmworkflows` | Full-screen SwarmFlow run tree | `/swarmflows` | **Recommended `team`** (requires `/swarmflow on`) |

Before the first message, `/mode` selects the mode of the in-memory draft without creating a saved session. This also applies after ordinary `/new` or `/clear`. The first message creates the session in the selected mode. If session creation is already in progress, the command waits for it and then uses the saved session ID.

For a saved session, `/mode` commits the mode through `mode.set` before updating the TUI. A response timeout or AgentServer connection loss triggers one retry; if the retry is inconclusive, the TUI reads the authoritative mode through `mode.get`. If another TUI has switched the same session, a mode conflict makes this TUI read and display the server's mode. If that read fails, the switch is marked unconfirmed. The next chat waits for mode synchronization without sending the message; its text and attachment references stay in the editor. Press Enter after confirmation. An active task, tool call, or pending interaction blocks the switch. Finish or cancel that work separately, then retry. The same Session ID and its history remain in the original project index when switching between work and code; only an explicit project rebind changes that index. Model context is not yet transferred between different executors.

If you open another session while a mode request is pending, its retries and status checks still target the original session. The pending result does not change the new session's displayed mode.

If an older session has a missing or invalid stored mode, `mode.get` reports `MODE_UNKNOWN`. Choose a mode explicitly, for example `/mode team.work` or `/mode agent.code`, to repair it after any active work finishes. The server preserves the original project binding. Legacy mode aliases are normalized in successful responses; a missing or invalid mode in a switch response leaves the TUI unconfirmed until you retry `/mode` or synchronize it.

After a session is restored, `/swarmflow on` uses its current mode. It keeps an existing Team mode such as `team.code`. If it switches to Team and the config write then fails, the TUI attempts to restore the actual previous mode. An unconfirmed rollback is reported explicitly.

> SwarmFlow walkthrough: **[TUI SwarmFlow Guide](TUISwarmFlowGuide.md)**.
