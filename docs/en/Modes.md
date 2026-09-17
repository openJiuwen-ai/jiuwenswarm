# Modes

JiuwenSwarm supports multiple runtime modes, each with its own tool set, permission policy, and memory behavior.

> **Note**: In the Web frontend, users can switch between **Agent mode** and **Cluster mode** using the **mode selector** in the chat input area. The `/mode` command is primarily for IM controlled channels and TUI.

---

## Web Frontend Modes

The Web frontend provides two execution modes:

| Mode | Description | Use Cases |
|------|-------------|-----------|
| **Agent mode** | Single agent handles tasks independently, supports task planning and dynamic adjustment | Most daily tasks, Q&A, code generation, etc. |
| **Cluster mode** | Multi-agent collaboration mode, with a Leader orchestrating multiple specialized agents | Large complex tasks, scenarios requiring multi-role collaboration |

![Mode Selector](../assets/images/current-ui-en/02-Mode-Selector.png)

---

## Command-Line Modes (IM/TUI)

Users can switch to more granular modes using the `/mode` command during a conversation.

### Mode Overview

| Mode | Code | Description |
|------|------|-------------|
| Agent | `agent` | Unified single-agent mode (former `agent.plan` / `agent.fast` modes merged). Full tools + passive memory |
| Code (Normal) | `code.normal` | Code mode + coding memory, focused on code execution |
| Code (Team) | `code.team` | Team collaboration launched from the Code profile |
| Team | `team` | Multi-agent collaboration mode, based on the `team` definition in config |

> **Compatibility**: Former `agent.plan` / `agent.fast` modes normalize to `agent` on non-Web composition paths. In Web mode, `agent.plan` + `work_mode` enables hard Plan mode (read-only planning; execution requires `exit_plan_mode` approval), which differs from the old “planning sub-mode” semantics. See **Work mode (`work_mode`)** below.

---

## Switching Modes

Use the following commands during a channel conversation:

```
/mode agent          # Switch to Agent mode (defaults to agent.plan)
/mode plan           # TUI local shorthand, equivalent to agent.plan
/mode code           # Switch to Code mode (defaults to code.normal)
/mode team           # Switch to Team mode
/mode agent.plan     # Switch directly to Agent Plan sub-mode
/mode agent.fast     # Switch directly to Agent Fast sub-mode
/mode code.normal    # Switch directly to Code Normal sub-mode
/mode code.team      # Switch directly to Code Team sub-mode
/mode team.normal    # TUI local form, equivalent to team
```

> Compatibility: `/mode plan` and `/mode team.normal` are TUI-local command forms. Gateway controlled channels accept `agent`, `code`, `team`, `agent.plan`, `agent.fast`, `code.normal`, and `code.team`.

You can also use `/switch` to change sub-modes within the same category:

```
/switch plan         # Under Agent → plan; under Code → plan
/switch fast         # Under Agent → fast
/switch normal       # Under Code → normal
/switch team         # Under Code → code.team
```

> The examples above describe the Gateway-controlled command used by IM channels. TUI has a different command with the same name only when launched under `agentos-tui` supervision (`AGENTOS_TUI_SUPERVISED=1`): `/switch claude` hands off to the Claude TUI and `/switch list` shows handoff targets. A standalone TUI does not register that command; use `/mode ...` or `/plan` for mode switching in TUI.

---

## Configuration

Define mode tools and constraints in the `modes` section of `config/config.yaml`:

```yaml
modes:
  agent:
    # plan / fast merged into a single agent mode: memory is always passive
    # (the is_proactive switch is retired).
    memory:
      enabled: true
    rails: []
    tools: []

  code:
    rails:
      - FileSystemRail           # File system safety rails
      - SkillUseRail             # Skill invocation rails
      - LspRail                  # LSP assistance rails
    tools:
      - web_free_search
      - web_fetch_webpage
      - web_paid_search
      - user_todos
    embedding_config:
      model_name: null
      base_url: null
      api_key: null

  team:
    jiuwen_team:
      team_name: jiuwen_team
      lifecycle: persistent
      teammate_mode: build_mode
      spawn_mode: inprocess
      leader:
        member_name: team_leader
        display_name: Team Leader
        persona: "Expert project manager, skilled at task decomposition and team coordination"
      agents:
        leader:
          workspace:
            stable_base: true
          max_iterations: 200
          completion_timeout: 600.0
      workspace:
        enabled: true
      transport:
        type: inprocess
      storage:
        type: sqlite
```

### Section Reference

| Path | Description |
|------|-------------|
| `modes.agent` | Unified Agent mode: passive memory; planning / subagent / evolution capabilities are assembled at runtime and no longer forked by plan/fast |
| `modes.code.rails` | Dynamic safety rails for Code mode (fixed rails are hardcoded) |
| `modes.code.tools` | Dynamic tool whitelist for Code mode (`coding_memory_*` and `send_file_to_user` are registered at runtime) |
| `modes.code.embedding_config` | Code-mode-specific embedding config (empty = use global) |
| `modes.team.<name>` | Team mode definition: team name, lifecycle, leader/agents config |

### Channel Default Mode

Each channel can specify a default mode via `channels.<channel>.default_mode` in `config.yaml`:

```yaml
channels:
  web:
    enabled: true
    default_mode: agent         # This channel defaults to unified Agent mode
```

---

## Work mode (`work_mode`)

`work_mode` is orthogonal to the execution `mode`. Values:

| Value | Meaning |
|-------|---------|
| `work` | General office / collaboration profile (Deep Agent); Git capabilities are not exposed by default |
| `code` | Code-engineering profile (Code Adapter); binds a project directory and shows Git status / diff |

When using the E2A protocol, send both `mode` and `work_mode` in `chat.send` `params`. The backend `mode_matrix` composes the final runtime shape (for example `mode=agent` + `work_mode=work` → executable Agent; `mode=agent.plan` + `work_mode=work` → hard Plan mode).

---

## Mode Behavior Differences

Modes do more than rename the UI state: they decide which AgentServer runtime profile is used, which Rails are attached, and how memory or team coordination is injected.

| Mode | Runtime profile | Agent behavior focus | Main Rails / tool differences | Memory strategy |
|------|-----------------|----------------------|--------------------------------|-----------------|
| `agent` | Deep Agent (`mode=agent`) | Unified single-agent chat. Suitable for daily tasks, multi-step reasoning, skill use, and work that benefits from subagents. | Mounts the former plan-tier capabilities (such as `TaskPlanningRail` and `SubagentRail`; enables `SkillEvolutionRail` / `SkillCreateRail` when configured); keeps search, multimodal, skill, and other common Agent tools. | Uses `modes.agent.memory`; fixed passive memory, read/write on demand. |
| `code.normal` | Code Adapter (`mode=code`, `sub_mode=normal`) | Execution phase for coding work. Useful for editing files, running commands, verifying changes, and delivering results. Skill self-evolution is currently not supported. | Uses the Code-specific English system prompt; fixed Rails include `LspRail`, `ProjectMemoryRail`, `CodingMemoryRail`, `AgentModeRail`, `StructuredAskUserRail`, `ConfirmInterruptRail`, filesystem/permission Rails; dynamic Rails/tools come from `modes.code.rails` / `modes.code.tools`. | Uses `CodingMemoryRail` and project memory files such as `JIUWENSWARM.md` / `CLAUDE.md`. |
| `code.team` | Code Adapter + Team sub-mode (`mode=code`, `sub_mode=team`) | Team collaboration launched from the Code profile. Useful when a coding project needs multiple members to split work while preserving code-workspace semantics. | The main agent stays on the Code profile; TeamManager starts team members and attempts to inherit the Code-side project directory, code tooling, and member skill toolkit. | Team members follow Team config; code/project context is influenced by both the Code profile and Team runtime. |
| `team` | Team runtime (`mode=team`) | Standard multi-agent collaboration. A leader decomposes, schedules, and summarizes work while role members execute subtasks. | Team members attach Rails such as `RuntimePromptRail`, `ResponsePromptRail`, `SysOperationRail`, `TaskPlanningRail`, `SecurityRail`, `HeartbeatRail`, and `AvatarPromptRail`; the leader additionally supports Team skill evolution/creation; tools come from the inheritable whitelist and team config. | Controlled by `modes.team.<name>.memory`, including shared `TEAM_MEMORY.md`, auto-extraction, and member memory prompt injection. |

### Quick Mental Model

- Former `agent.plan` / `agent.fast` modes are merged into unified `agent`: one Deep Agent profile, shared planning / subagent / skill-evolution capabilities, and fixed passive memory.
- In Web mode, `agent.plan` + `work_mode` enables hard Plan mode, which differs from executable unified `agent` chat.
- `code.team` and `team` both enter team collaboration, but from different entry points: `code.team` starts from the Code profile and is better for code-project delegation; `team` is the standard Team runtime.

---

## See Also

- [Configuration](Configuration.md) — Full `modes` section field reference in `config.yaml`
- [CLI Commands](CLI.md) — Full command reference including `/mode` and `/switch`
- [Slash Command Architecture](SlashCommandArchitecture.md) — Internal command parsing flow
- [Distributed Team](DistributedTeam.md) — Distributed deployment for Team mode

## Changelog

- v0.2.4b3: Merged `agent.fast` and `agent.plan` into unified `agent` mode; added `work_mode`. In Web mode, `agent.plan` + `work_mode` enables hard Plan mode.
