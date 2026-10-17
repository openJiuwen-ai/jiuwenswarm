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

The strings below are legacy aliases for the new three-segment canonical
modes (see [New Three-Segment Canonical Modes](#new-three-segment-canonical-modes)
below). The runtime writes the canonical form back into `params["mode"]`, but
legacy clients may still send these alias strings.

| Mode | Legacy alias | Canonical | Description |
|------|--------------|-----------|-------------|
| Agent | `agent` | `agent.work.normal` | Unified single-agent mode (former `agent.plan` / `agent.fast` modes merged). Full tools + passive memory |
| Code (Normal) | `code.normal` | `agent.code.normal` | Code mode + coding memory, focused on code execution |
| Code (Team) | `code.team` | `team.code.normal` | Team collaboration launched from the Code profile |
| Team | `team` | `team.work.normal` | Multi-agent collaboration mode, based on the `team` definition in config |

> **Compatibility**: Legacy strings are silently mapped to the new canonical
> via `deprecate_mode()` (no error, no warning). In particular `agent.plan`
> maps to `agent.work.plan` (plan state preserved), **not** to `agent`. In
> Web mode, `agent.plan` + `work_mode` enables hard Plan mode (read-only
> planning; execution requires `exit_plan_mode` approval), which differs from
> the old “planning sub-mode” semantics. See **Work mode (`work_mode`)** below.

---

## New Three-Segment Canonical Modes

The backend hub layer (`jiuwenswarm/common/mode_matrix.py`) introduces 8
three-segment canonical mode strings as the unified external identifier. The
three segments are `<category>.<work profile>.<plan?>`:

- **Category**: `agent` (single agent) / `team` (cluster)
- **Work profile**: `work` (Deep Agent profile) / `code` (Code Adapter profile)
- **Plan**: `normal` (executable) / `plan` (read-only planning; execution
  requires approval)

| New canonical | Meaning | Legacy alias |
|---------------|---------|--------------|
| `agent.work.normal` | single agent + work profile + executable | `agent` / `agent.fast` |
| `agent.work.plan`   | single agent + work profile + planning | `agent.plan` |
| `agent.code.normal` | single agent + code profile + executable | `code.normal` |
| `agent.code.plan`   | single agent + code profile + planning | `code.plan` |
| `team.work.normal`  | cluster + work profile + executable | `team` |
| `team.work.plan`    | cluster + work profile + planning | `team.plan.normal` |
| `team.code.normal`  | cluster + code profile + executable | `code.team` |
| `team.code.plan`    | cluster + code profile + planning | `team.plan.code` |

> **Migration strategy**: legacy strings are silently mapped to the new
> canonical via `deprecate_mode()` (no error, no warning); the canonical
> written back into `params["mode"]` is always the new string. New code should
> send the new strings directly; legacy clients sending legacy strings still
> work, but the runtime canonical mode is always the new string.

### Plan entry contract (`plan_entry_source`)

“Explicit plan entry” is a one-shot `plan_entry_source` field shared between
frontend and backend via a string-literal contract:

| Value | Source |
|-------|--------|
| `slash_command` | Entry source produced by the TUI `/plan` command |
| `plan_toggle` | Entry source from the first Plan message after toggling the Web Plan switch |

The backend `AgentWebSocketServer._is_explicit_plan_entry_request` anti-reentry
gate only accepts these two literals; frontends (TUI
`core/plan-entry-source.ts` / Web `features/planMode/planEntrySource.ts`) must
use the same-named literal. The literals are defined in
`jiuwenswarm/common/schema/chat_send.py` in the `PLAN_ENTRY_SOURCES` constant
set; the cross-layer contract test is
`tests/unit_tests/test_plan_entry_source_contract.py`.

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

# New three-segment canonical forms (recommended for new code):
/mode agent.work.normal  # Single agent + work profile + executable
/mode agent.work.plan    # Single agent + work profile + planning
/mode team.code.normal   # Cluster + code profile + executable
```

> Compatibility: `/mode plan` and `/mode team.normal` are TUI-local command forms. Gateway controlled channels accept the union of:
> - 8 new three-segment canonicals: `agent.work.normal`, `agent.work.plan`, `agent.code.normal`, `agent.code.plan`, `team.work.normal`, `team.work.plan`, `team.code.normal`, `team.code.plan`
> - 10 legacy canonicals (`DEPRECATION_MAP` keys): `agent`, `agent.plan`, `agent.fast`, `code`, `code.normal`, `code.plan`, `code.team`, `team`, `team.plan.normal`, `team.plan.code`
> - 2 formal aliases (`MODE_ALIASES` keys): `team.plan` (→ `team.work.plan`), `team.code` (→ `team.code.normal`)
>
> Any string outside this set is rejected as an illegal command by the gateway pre-check (`_VALID_MODE_INPUTS` in `jiuwenswarm/gateway/message_handler/message_handler.py`). Legacy strings are silently mapped to the new canonical via `deprecate_mode()`.

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

When using the E2A protocol, send both `mode` and `work_mode` in `chat.send` `params`.
The backend `mode_matrix` composes the final runtime shape (for example
`mode=agent` + `work_mode=work` → executable Agent;
`mode=agent.plan` + `work_mode=work` → hard Plan mode).

> **Transition status**: The new three-segment canonical modes already
> encode the work profile in the middle segment (`agent.work.*` /
> `agent.code.*`), so on the new canonicals `work_mode` is redundant for mode
> resolution. Per the project's refactor decision, `work_mode` is **not
> dropped yet** — it is being phased out ("暂不丢, 渐进退场"). It remains in
> use as the default project bucketing key (`work` → `default` project,
> `code` → `default_code` project) for sessions without an explicit
> `project_id`. New code should prefer sending the new canonical `mode`
> string directly; legacy Web clients may still send `mode` + `work_mode`
> and the backend continues to accept both during the transition. See
> `PLAN_drop_work_mode.md` for the full retirement plan.

---

## Mode Behavior Differences

Modes do more than rename the UI state: they decide which AgentServer runtime
profile is used, which Rails are attached, and how memory or team coordination
is injected.

| Mode | Runtime profile | Agent behavior focus | Main Rails / tool differences | Memory strategy |
|------|-----------------|----------------------|--------------------------------|-----------------|
| `agent` | Deep Agent (`mode=agent`) | Unified single-agent chat. Suitable for daily tasks, multi-step reasoning, skill use, and work that benefits from subagents. | Mounts the former plan-tier capabilities (such as `TaskPlanningRail` and `SubagentRail`; enables `SkillEvolutionRail` / `SkillCreateRail` when configured); keeps search, multimodal, skill, and other common Agent tools. | Uses `modes.agent.memory`; fixed passive memory, read/write on demand. |
| `code.normal` | Code Adapter (`mode=code`, `sub_mode=normal`) | Execution phase for coding work. Useful for editing files, running commands, verifying changes, and delivering results. Skill self-evolution is currently not supported. | Uses the Code-specific English system prompt; fixed Rails include `LspRail`, `ProjectMemoryRail`, `CodingMemoryRail`, `AgentModeRail`, `StructuredAskUserRail`, `ConfirmInterruptRail`, filesystem/permission Rails; dynamic Rails/tools come from `modes.code.rails` / `modes.code.tools`. | Uses `CodingMemoryRail` and project memory files such as `JIUWENSWARM.md` / `CLAUDE.md`. |
| `code.team` | Code Adapter + Team sub-mode (`mode=code`, `sub_mode=team`) | Team collaboration launched from the Code profile. Useful when a coding project needs multiple members to split work while preserving code-workspace semantics. | The main agent stays on the Code profile; TeamManager starts team members and attempts to inherit the Code-side project directory, code tooling, and member skill toolkit. | Team members follow Team config; code/project context is influenced by both the Code profile and Team runtime. |
| `team` | Team runtime (`mode=team`) | Standard multi-agent collaboration. A leader decomposes, schedules, and summarizes work while role members execute subtasks. | Team members attach Rails such as `RuntimePromptRail`, `ResponsePromptRail`, `SysOperationRail`, `TaskPlanningRail`, `SecurityRail`, and `AvatarPromptRail`; the leader additionally supports Team skill evolution/creation; tools come from the inheritable whitelist and team config. | Controlled by `modes.team.<name>.memory`, including shared `TEAM_MEMORY.md`, auto-extraction, and member memory prompt injection. |

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
