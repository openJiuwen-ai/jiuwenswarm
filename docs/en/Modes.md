# Modes

JiuwenSwarm supports multiple runtime modes, each with its own tool set, permission policy, and memory behavior. Users can switch modes during a conversation using the `/mode` command.

---

## Mode Overview

| Mode | Code | Description |
|------|------|-------------|
| Agent (Plan) | `agent.plan` | Default mode. Full tools + proactive memory, focused on reasoning and planning |
| Agent (Fast) | `agent.fast` | Full tools + passive memory, focused on quick responses |
| Code (Normal) | `code.normal` | Code mode + coding memory, focused on code execution |
| Code (Team) | `code.team` | Team collaboration launched from the Code profile |
| Team | `team` | Multi-agent collaboration mode, based on the `team` definition in config |

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

> The TUI source contains a `/switch` implementation, but the default TUI command registry does not currently register it. In TUI, prefer `/mode ...` for direct switching or `/plan` to enter Agent plan mode.

---

## Configuration

Define mode tools and constraints in the `modes` section of `config/config.yaml`:

```yaml
modes:
  agent:
    fast:
      memory:
        enabled: true
        is_proactive: false     # Passive memory
      rails: []
      tools: []
    plan:
      memory:
        enabled: true
        is_proactive: true      # Proactive memory
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
| `modes.agent.fast` | Agent Fast mode: passive memory, no extra rails |
| `modes.agent.plan` | Agent Plan mode: proactive memory, no extra rails |
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
    default_mode: agent.plan    # This channel defaults to Agent Plan mode
```

---

## Mode Behavior Differences

Modes do more than rename the UI state: they decide which AgentServer runtime profile is used, which Rails are attached, and how memory or team coordination is injected.

| Mode | Runtime profile | Agent behavior focus | Main Rails / tool differences | Memory strategy |
|------|-----------------|----------------------|--------------------------------|-----------------|
| `agent.plan` | Deep Agent (`mode=agent`, `sub_mode=plan`) | Default planning-oriented chat. Best for complex task decomposition, long reasoning, skill evolution, and tasks that benefit from subagents. | Registers `TaskPlanningRail` and `SubagentRail`; enables `SkillEvolutionRail` / `SkillCreateRail` when configured; keeps normal Agent tools such as search, multimodal tools, and skills. | Uses `modes.agent.plan.memory`; defaults to proactive memory (`is_proactive: true`) so context is retrieved and consolidated more actively. |
| `agent.fast` | Deep Agent (`mode=agent`, `sub_mode=fast`) | Fast response mode. Less orchestration, more direct answering or tool use on demand. | Unregisters `TaskPlanningRail`, `SkillEvolutionRail`, `SkillCreateRail`, and `SubagentRail`; keeps common runtime prompt, safety, permission, search/multimodal/skill tooling. | Uses `modes.agent.fast.memory`; defaults to passive memory (`is_proactive: false`) and usually reads/writes memory on demand. |
| `code.normal` | Code Adapter (`mode=code`, `sub_mode=normal`) | Execution phase for coding work. Useful for editing files, running commands, verifying changes, and delivering results. | Uses the Code-specific English system prompt; fixed Rails include `LspRail`, `ProjectMemoryRail`, `CodingMemoryRail`, `AgentModeRail`, `StructuredAskUserRail`, `ConfirmInterruptRail`, filesystem/permission Rails; dynamic Rails/tools come from `modes.code.rails` / `modes.code.tools`. | Uses `CodingMemoryRail` and project memory files such as `JIUWENSWARM.md` / `CLAUDE.md`. |
| `code.team` | Code Adapter + Team sub-mode (`mode=code`, `sub_mode=team`) | Team collaboration launched from the Code profile. Useful when a coding project needs multiple members to split work while preserving code-workspace semantics. | The main agent stays on the Code profile; TeamManager starts team members and attempts to inherit the Code-side project directory, code tooling, and member skill toolkit. | Team members follow Team config; code/project context is influenced by both the Code profile and Team runtime. |
| `team` | Team runtime (`mode=team`) | Standard multi-agent collaboration. A leader decomposes, schedules, and summarizes work while role members execute subtasks. | Team members attach Rails such as `RuntimePromptRail`, `ResponsePromptRail`, `SysOperationRail`, `TaskPlanningRail`, `SecurityRail`, `HeartbeatRail`, and `AvatarPromptRail`; the leader additionally supports Team skill evolution/creation; tools come from the inheritable whitelist and team config. | Controlled by `modes.team.<name>.memory`, including shared `TEAM_MEMORY.md`, auto-extraction, and member memory prompt injection. |

### Quick Mental Model

- `agent.plan` and `agent.fast` use the same Deep Agent profile, but `agent.plan` keeps planning, subagent, and skill-evolution Rails while `agent.fast` removes those heavier orchestration pieces.
- `code.team` and `team` both enter team collaboration, but from different entry points: `code.team` starts from the Code profile and is better for code-project delegation; `team` is the standard Team runtime.

---

## See Also

- [Configuration](Configuration.md) — Full `modes` section field reference in `config.yaml`
- [CLI Commands](CLI.md) — Full command reference including `/mode` and `/switch`
- [Slash Command Architecture](SlashCommandArchitecture.md) — Internal command parsing flow
- [Distributed Team](DistributedTeam.md) — Distributed deployment for Team mode
