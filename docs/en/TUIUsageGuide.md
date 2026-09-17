# JiuwenSwarm TUI Usage Guide

> Full walkthrough (CLI flags, tools, keyboard shortcuts, Code mode): Chinese [TUI 使用指南](../zh/TUI使用指南.md). Install and first run: [Quick start (TUI)](Quickstart_tui.md).

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
| `/clear` | `/reset`, `/new` | New session ID, clear transcript (rejected while busy) | `/clear` | All |
| `/copy` | - | Copy the Nth most recent assistant reply to clipboard | `/copy` or `/copy 2` | All |
| `/theme` | - | Switch dark / light theme | `/theme dark` | All |
| `/color` | - | Set prompt accent color | `/color blue` | All |
| `/compact` | - | Compact context, keep summary | `/compact` | All |
| `/config` | `/settings`, `/setting` | View / set backend config | `/config`, `/config get`, `/config set key value` | All |
| `/context` | - | Context window and token usage | `/context` | All |
| `/diff` | - | Interactive turn diffs + uncommitted working tree | `/diff` | All |
| `/evolve` | - | Trigger skill evolution | `/evolve myskill fix error handling` | `agent.plan` / `team` (see below) |
| `/evolve_list` | - | List evolution entries for a skill | `/evolve_list myskill --sort score` | `agent.plan` / `team` |
| `/evolve_rebuild` | - | Rebuild SKILL.md from archive and evolution records | `/evolve_rebuild myskill strengthen errors` | `agent.plan` / `team` |
| `/evolve_simplify` | - | Organize / merge evolution notes for a skill | `/evolve_simplify myskill merge duplicates` | `agent.plan` / `team` |
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

> SwarmFlow walkthrough: **[TUI SwarmFlow Guide](TUISwarmFlowGuide.md)**.
