# JiuwenSwarm TUI SwarmFlow Guide

> This document is for **JiuwenSwarm TUI users**. It covers the complete workflow of using SwarmFlow (Swarm Workflows) in the terminal interface, including configuration, triggering runs, real-time monitoring, and interactive inspection.
>
> **Note**: The TUI interface examples in this document (code blocks, status icons, banner text, etc.) are illustrative only and may differ from what is actually displayed; refer to the terminal's real output as the source of truth.

---

## Overview

**SwarmFlow** uses **Python workflow scripts** (`scripts/workflow.py`) for deterministic multi-agent orchestration: phases, model calls, parallel/pipeline steps, and Human-in-the-loop turns. In **Team mode**, the Leader runs scripts via **`SwarmflowTool`** (`openjiuwen/agent_teams/workflow/tool_swarmflow.py`) in the background; the TUI subscribes to run events and renders the Phase / Node tree.

Main entry points: **`/swarmflow`** toggle, **`/swarmflows`** run tree, **`h`** to reply to `human` / `human_session` nodes.

### Where scripts come from

| Method | Description |
|--------|-------------|
| Leader on-the-fly | Leader calls the `swarmflow()` tool; the tool description (`openjiuwen/agent_teams/tools/locales/descs/en/workflow/swarmflow.md`) guides the model to write `scripts/workflow.py` and execute it |
| Swarm Skill asset | Use the built-in **`swarmskill-creator`** Skill to produce a reusable Swarm Skill with a workflow script; install locally or publish to Skills Hub |
| Offline-generated script | Prepare `scripts/workflow.py` outside the live Team session (or e.g. `swarmflow/<name>.py` under the team workspace); in session, have the Leader run it via **`swarmflow(script_path=...)`** or inline **`script`**; edit and re-run with the same path |

A valid script has top-level **`META={...}`** and **`async def run(args)`**; operators are imported via **`from swarmflow import ...`** (resolved at runtime to `openjiuwen/agent_teams/workflow/engine/facade.py`).

Most users do not need offline-authored scripts — toggle SwarmFlow, watch progress, reply to HITL. For precise orchestration, use **offline generation** or **Skill** paths; the operator table below is the authoring reference.

### Script operators

Scripts import these via `from swarmflow import ...`; signatures are defined in **`openjiuwen/agent_teams/workflow/engine/facade.py`**.

**Orchestration**

| Operator | Signature | Role | When to use |
|----------|-----------|------|-------------|
| `agent` | `async def agent(prompt, *, label=None, phase=None, schema=None, options=None)` | Spawn a one-shot worker; `schema` for structured output; `options` — see below | Single AI step: retrieve, analyze, generate a chunk; pass `schema` for JSON/model output |
| `parallel` | `async def parallel(thunks)` | Fork-join over lazy thunks; **waits for all**; failures become `None`, never raises | Need the **full** result set before the next step: parallel search then dedupe/merge, skip downstream when count is 0 |
| `pipeline` | `async def pipeline(items, *stages)` | No-barrier pipeline; each item flows through stages independently | **Default choice**: many items × many stages (e.g. per-URL fetch→analyze→summarize) without cross-item barriers |
| `map_parallel` / `pmap` | `async def map_parallel(items, fn)`; `pmap` alias | Fan-out per item via `async fn(item)`; avoids closure traps | List fan-out shorthand; replaces `parallel([lambda x=x: ...])` |
| `phase` | `def phase(title)` | Mark the current phase; emit a phase event | Split major steps (research / analysis / draft); align titles with `META.phases` |
| `log` | `def log(message)` | Emit one progress line | Milestones and narration (e.g. "merging results") |
| `compact` | `def compact(xs)` | Drop falsy values | After `parallel` / `pipeline` to remove failed/empty items |
| `flatten_filter` | `def flatten_filter(xs)` | Flatten one level and drop falsy values | Nested list results before downstream steps |

**Stateful**

| Operator | Signature | Role | When to use |
|----------|-----------|------|-------------|
| `agent_session` | `def agent_session(*, label=None, phase=None, instructions=None, options=None) -> AgentSession` | Multi-turn agent; `options` at creation and on each `send()` | Same worker across turns: iterative polish, stepwise coding, persistent context |
| `human_session` | `def human_session(*, label=None, phase=None, instructions=None, options=None) -> HumanSession` | Multi-turn human; waiting does not consume concurrency | Multi-turn clarification with a person, supplying info turn by turn |
| `human` | `async def human(prompt, *, schema=None, label=None, phase=None, options=None)` | One-shot human turn; closes after answer | One-time approval, confirmation, or choice (single-turn HITL) |

**Mechanism**

| Operator | Signature | Role | When to use |
|----------|-----------|------|-------------|
| `workflow` | `async def workflow(name_or_path, args=None)` | Inline another script (**at most one nest level**); shares concurrency and budget | Reuse an existing `workflow.py` as a sub-step; compose modules in a larger flow |
| `budget` | `budget.total` / `budget.spent()` / `budget.remaining()` | Read token budget and spend | Dynamic fan-out (`while remaining() > N`); wind down before hard cap |

**`options` bag** (defined in `_ENGINE_OPTIONS` at `openjiuwen/agent_teams/workflow/engine/primitives.py`)

Supported on `agent()`, `human()`, and `agent_session()` / `human_session()` via `send()`. Explicit kwargs (`label` / `phase` / `schema`) override same keys in `options`. Allowed keys are `_ENGINE_OPTIONS` union backend `KNOWN_OPTIONS`; **unknown keys fail fast**.

| Key | Applies to | Description |
|-----|------------|-------------|
| `label` | `agent` / session | Display label in progress events (also valid as explicit kwargs) |
| `phase` | `agent` / session | Assign to a Phase group; prefer explicit `phase` inside `parallel` / `pipeline` |
| `schema` | `agent` / `human` / `send` | Structured output: Pydantic model / JSON Schema dict / omit for text |
| `model` | `agent` / `agent_session` | Override this worker's model name; **omit by default** to inherit teammate model |
| `timeout` | `agent` / `human` | Timeout in seconds: backend call for `agent`; wait-for-human for `human` |
| `isolation` | `agent` | Only `'worktree'`: run worker in an isolated git worktree (parallel file edits; costly) |
| `agent_type` | `agent` | Use a named expert subagent instead of the default worker (composable with `schema`) |

Example:

```python
await agent(
    "Analyze competitors",
    label="analyst",
    phase="research",
    options={"model": "strong-model", "timeout": 120},
)

s = agent_session(label="writer", phase="draft", options={"model": "fast-model"})
await s.send("Draft section 1", options={"timeout": 60})
```

`options` passed to `agent_session()` / `human_session()` are session defaults; each `send(..., options=...)` may override or extend (per-key, `send` wins).

### Core concepts

| Concept | Description |
|---------|-------------|
| **Workflow** | One SwarmFlow **Run** instance driven by a script |
| **Phase** | A stage declared by `phase()` in the script |
| **Node** | Execution unit in a phase: `agent` / `agent_session` / `human` / `human_session` |
| **Run ID** | Unique id per run, used to identify and distinguish run instances |
| **Team budget** | Shared team token cap and usage (visible when `swarmflow_budget` is configured) |

### Workflow lifecycle

From the TUI, one Run looks like this (the engine advances Phases / Nodes; the TUI refreshes state in real time):

```
/swarmflow on → submit task → phases progress → [node: waiting_for_human → h reply] → completed / failed
```

| Step | You do | On screen |
|------|--------|-----------|
| 1. Enable | `/swarmflow on`; confirm with `/swarmflow` | `swarmflow: on · mode: team · budget: ...` |
| 2. Start | Submit in Team mode; Leader calls `swarmflow()` | **Running banner** (workflow name, elapsed) |
| 3. Follow | **`/swarmflows`** for the run tree | Phase / Node status, logs, Team budget |
| 4. HITL | `human` / `human_session` fires | Node **`waiting_for_human`**; **`h`** → `chat.swarmflow_reply` |
| 5. Finish | Wait for terminal state or close session | Workflow **`completed`** / **`failed`** / **`stopped`** |

**Common branches**

| Case | TUI behavior |
|------|--------------|
| Nested `workflow()` | Sub-phase card **`▸ {name} #{N}`** |
| `swarmflow_budget` configured | Team budget row; cap hit → run **`failed`**, no mid-run resume |

> Leader chat text is auxiliary; **trust `/swarmflows`**. After `/swarmflow on`, if monitoring is missing, try **`/new`** for a fresh session.

---

## Prerequisites

### 1. Install and Start the JiuwenSwarm Backend

```bash
# Install
pip install jiuwenswarm

# Initialize (first time)
jiuwenswarm-init

# Start the backend service
jiuwenswarm-start
```

### 2. Install and Start the TUI

```bash
# Install the TUI
pip install jiuwenswarm-tui

# Start the TUI (in a separate terminal)
jiuwenswarm-tui
```

> The TUI connects to the local Gateway's TUI endpoint via WebSocket (default: `ws://127.0.0.1:19001/tui`). Make sure the backend service is running.

### 3. Configure the Model API

On first use, you need to set up the model API in the configuration. You can do this through the Web frontend (`http://localhost:5173`) in the **Configuration** panel, or by using the `/config` command in the TUI.

---

## Enabling SwarmFlow

Runtime config: `~/.jiuwenswarm/config/config.yaml` (initialized by `jiuwenswarm-init` from **`jiuwenswarm/resources/config.yaml`**).

SwarmFlow uses only two fields under `modes.team.jiuwen_team` (excerpt from the repo template):

```yaml
modes:
  team:
    jiuwen_team:
      enable_swarmflow: false

      # Team-level token budget ceiling for swarmflow runs.
      # Unset / null = unbounded (no ceiling).  Set to a positive integer to
      # cap total tokens across ALL swarmflow runs spawned by this team.
      # swarmflow_budget: 500000
```

| Field | Description | Default |
|-------|-------------|---------|
| `enable_swarmflow` | When `true`, the Leader may call `swarmflow()` to start runs; when `false`, SwarmFlow **does not** run even in `/mode team` | `false` |
| `swarmflow_budget` | Leader-level token cap shared by **all** runs in this team; readable in scripts via `budget.total` / `spent()` / `remaining()`. A **positive integer** sets a cap; **unset or `null`** means unbounded | unset (commented in template) |

The shipped template defaults SwarmFlow to **off** (`enable_swarmflow: false`). Enable it via one of the paths below on first use.

### Three ways to change configuration

| Path | Fields | How | When it takes effect |
|------|--------|-----|----------------------|
| **1. Edit `config.yaml`** | `enable_swarmflow`, `swarmflow_budget` | Edit `modes.team.jiuwen_team` in `~/.jiuwenswarm/config/config.yaml` | **Restart the backend** for global effect |
| **2. Web UI** | `enable_swarmflow` only | Go to **More → Configuration Info → Other Config → SwarmFlow** and toggle on "Enable SwarmFlow" (**cluster mode**; the Web UI does **not** expose `swarmflow_budget` — set the budget via `config.yaml` or TUI `/swarmflow on --budget`) | **No hot reload**: after saving, **start a new session** (new chat or equivalent); active workflows are not interrupted |
| **3. TUI `/swarmflow`** | `enable_swarmflow`; budget via `on --budget` | `/swarmflow on` / `off`; `/swarmflow on --budget <tokens\|none>`; `/swarmflow` to query | Writes config, then **`Use /new to apply.`** — current session is not hot-reloaded; run **`/new`** for a fresh session |

> **Tip**: Use **`/swarmflow on`** day to day in the TUI; use `config.yaml` + restart for fixed deployment policy; after enabling SwarmFlow in the Web cluster mode, **open a new session**.

---

## TUI command: `/swarmflow`

`/swarmflow` is the **primary TUI entry** to enable or disable SwarmFlow: it reads/writes `enable_swarmflow` and shows the current mode.

### Subcommands

| Command | Action |
|---------|--------|
| `/swarmflow` | Query status, e.g. `swarmflow: on · mode: team · budget: unbounded` (shows the set token count when a budget is configured) |
| `/swarmflow on` | Set `enable_swarmflow=true`; switches to **team** if needed; optional `--budget <tokens\|none>` |
| `/swarmflow off` | Set `enable_swarmflow=false`; does **not** leave team mode automatically |
| `/swarmflow invalid` | Error with hint to use `on` or `off` |

### Recommended first-time setup

```
/swarmflow on
/swarmflow          # confirm: swarmflow: on · mode: team · budget: unbounded
```

Then type your task; the Leader runs the workflow script via **`SwarmflowTool`** (`openjiuwen/agent_teams/workflow/tool_swarmflow.py`) in the background.

### vs `/mode team`

| Approach | When to use |
|----------|-------------|
| **`/swarmflow on`** | Recommended: enables SwarmFlow and enters team in one step |
| `/mode team` | Mode only; if `enable_swarmflow: false` in config, the Leader **will not** run SwarmFlow |

### Session boundaries

| Scenario | Behavior |
|----------|----------|
| Not in team → `/swarmflow on` | Switch to team and persist config; monitoring appears on the **next** workflow run |
| Already in team, was off → `/swarmflow on` | Config updated; current session may lack monitor → run **`/new`** for immediate effect |
| Team with active workflow → `/swarmflow off` | Does **not** stop the current run; disable applies to **new** sessions (`/new` if needed) |
| Repeat `/swarmflow on` when already on | Reports already enabled; no duplicate config write |

To disable: `/swarmflow off`, then `/swarmflow` to confirm `swarmflow: off`.

---

## Using SwarmFlow

### Step 1: Enable SwarmFlow

In the TUI:

```
/swarmflow on
```

Verify:

```
/swarmflow
```

Expected: `swarmflow: on · mode: team · budget: unbounded` (shows `unbounded` when no budget is set, or the configured token count otherwise).

If you are already in team but just turned it on and no workflow banner appears, run **`/new`** then submit a task.

> Equivalent: `/mode team` with `enable_swarmflow: true` in config — `/swarmflow on` is simpler.

### Step 2: Submit a task

In Team mode, simply type a task description. SwarmFlow starts automatically after the Leader analyzes the task:

```
In swarmflow mode, research the new energy vehicle industry and produce an analysis report
```

The Leader Agent will:
1. Analyze the requirements and decompose the task into multiple phases (e.g., research → analysis → writing → review)
2. Launch the workflow via **`SwarmflowTool`** (`openjiuwen/agent_teams/workflow/tool_swarmflow.py`), generating a `run_id`
3. Assign workers / session nodes per phase

### Step 3: Persist a workflow as a Swarm Skill (optional)

When a workflow orchestration is meant to be reused, you can turn it into a reusable Swarm Skill (including its `workflow.py` script) with the built-in **`swarmskill-creator`** Skill. Once created, just have the Leader invoke that Skill in later sessions — no need to regenerate the script every time.

`swarmskill-creator` is a built-in Skill and is installed by default. If it is missing from your environment, install it first:

```
/skills search swarmskill-creator
/skills install swarmskill-creator
```

Then invoke the Skill directly in a Team session and describe the workflow to persist. It supports three modes: **CREATE** (build from scratch), **CONVERT** (turn a single-agent Skill into a Swarm Skill), and **MODIFY** (edit an existing Swarm Skill):

```
/swarmskill-creator Turn the "research → analysis → writing → review" workflow into a reusable Swarm Skill
```

The resulting Swarm Skill can be reused locally or published to the Skills Hub for other teams to install. See `docs/en/SwarmSkills.md` for details.

### Step 4: Monitor progress

After a workflow starts, the TUI main view automatically displays a running workflow status banner:

```
◐ 1 workflow running
  NEV Industry Research · 2m 15s
```

The banner includes:
- An animated spinner indicator (`◐◓◑◒`)
- The number of running workflows
- The workflow name and elapsed time

### Step 5: Inspect details

```
/swarmflows
```

Alias: `/swarmworkflows`.

### Step 6: Disable SwarmFlow (optional)

```
/swarmflow off
/swarmflow    # confirm swarmflow: off
```

Workflows already running in the current session are **not** force-stopped.

---

## Human-in-the-loop (HITL)

When the script uses `human` / `human_session`, nodes enter **`waiting_for_human`** and the TUI shows pending questions.

### Main view shortcuts

| Action | Purpose |
|--------|---------|
| **`h`** (lowercase) | Open the **pending-list** when human turns are waiting (not the `/swarmflows` list) |
| Reply box | Select a pending item, type Answer, `Enter` to send via `chat.swarmflow_reply` |

### In the `/swarmflows` viewer

- **human / human_session**: show Question; after reply, brief `running`, then `completed` or `failed`
- **agent_session**: multi-turn LLM session node, no human wait state
- **agent**: single-round worker node

### TUI command map

| Command / key | Purpose |
|---------------|---------|
| **`/swarmflow` / `on` / `off`** | Toggle SwarmFlow, query status |
| **`/swarmflows`** | Full-screen run tree |
| **`h`** | Pending human replies on the main view |

---

## SwarmFlow Interactive Viewer

Open with `/swarmflows` (requires `/swarmflow on`). Navigation: **Workflow list → Phase details → Node details**.

### Nested sub-workflows

When the script calls `workflow()`, the viewer creates **child phase** cards named like `▸ intro #0` (`#N` disambiguates concurrent runs). Nodes inside the sub-flow attach under the child card, not the parent author phase.

### Tokens and Team budget

| Where | What |
|-------|------|
| Node detail | Per-call **tokens** (when the provider reports usage) |
| Run summary | **Team budget** `spent / total` (when `swarmflow_budget` is set) |
| Budget exhausted | Run ends **failed**; not resumable (unlike pause) |

### Level 1: Workflow list

Displays an overview of all workflows in the current session:

```
Swarm workflows
2 running, 1 completed

  ● running  NEV Industry Research      3/8 agents
  ● running  Competitive Analysis       1/5 agents
  ✓ completed User Profiling            6 agents

up/down select - Enter view - r refresh - Esc close
```

| Information | Description |
|-------------|-------------|
| Status icon | `●` running / `○` pending / `◇` planned / `✓` completed / `×` failed / `■` stopped |
| Workflow name | Set by **`SwarmflowTool`** (`openjiuwen/agent_teams/workflow/tool_swarmflow.py`) at launch |
| Agent progress | `completed/total` format |

**Controls**:

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move focus between workflows |
| `Enter` | Enter the selected workflow's phase details |
| `r` | Refresh the workflow list |
| `Esc` | Close the viewer, return to chat |

### Level 2: Phase Details

After selecting a workflow, the left side shows phases and the right side shows **Agents** in the current phase (`agent` / `agent_session` / `human` / `human_session` — see [Level 3: Node details](#level-3-node-details)):

```
NEV Industry Research
Research the NEV industry and produce an analysis report
● running · 3/8 agents
2m 15s

Logs
  [leader] Starting research phase...
  [researcher] Searching industry data...

Phases                          Agents · Research
  ✓ Research    2/3              ● running  Data Researcher   · glm-5
  ● Analysis    1/3              ● running  Market Analyst    · glm-5
  ◇ Writing     0/2              ✓ completed Info Collector   · glm-5

press l to see full logs
up/down select phase · Right agents · Left back · Esc back
```

**Controls**:

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move focus between phases |
| `→` | Move focus from **Phases** to the **Agents** list; when already in Agents, open the selected node's details |
| `←` | Go back one level: Agents → Phases → workflow list |
| `l` | View full workflow logs (enters the file viewer) |
| `r` | Refresh |
| `Esc` | Return to the workflow list |

### Level 3: Node details

Each row in the Level 2 **Agents** list is one **Node** (`node_type`) from the script. Press `→` for details. Session operators may show a **parent row + turn rows** (e.g. `turn 0`, `turn 1`).

#### Four node types

| Script operator | List shape | Detail fields | Turns & history |
|-----------------|------------|---------------|-----------------|
| **`agent()`** | Single row | **Model**, **Prompt**, **Outcome** / **Error** | One shot; **no** Session History |
| **`agent_session()`** | Same phase + **label**: **session parent** + **turn** rows | Per turn: **Model**, **Prompt**, **Outcome** / **Error** | Multi-turn LLM; **`s`** in detail → **Session History** |
| **`human()`** | Single row | **Question**, **Answer** (`waiting_for_human` while pending) | One-shot HITL; **no** Session History; reply with **`h`** on main view |
| **`human_session()`** | Same tree as **`agent_session`** | Per turn: **Question**, **Answer** | Multi-turn HITL; **`s`** → Session History; **`Tab`** on waiting turn to reply |

> **Quick distinction**: `agent` / `agent_session` use **Prompt → Outcome** (AI worker); `human` / `human_session` use **Question → Answer** (person). Human rows may show as `human(model-name)` when the progress event carries a model.

#### Example: `agent()` node

```
Data Researcher
NEV Industry Research · Research
● running · glm-5
duration 45s

Prompt
  Research NEV industry market data for the past three years, including sales,...

Outcome
  (shown when complete)

press p prompt · o outcome · e error · s session history (when applicable)
Esc/← back
```

#### Example: `human()` / `human_session()` node

```
Approver
NEV Industry Research · Review
☺ waiting_for_human

Question
  Approve publishing this analysis report?

Answer
  (pending; press h on main view, or Tab in Session History)

press q question · a answer · s session history (human_session multi-turn only)
Esc/← back
```

**Controls**:

| Key | Action | Applies to |
|-----|--------|------------|
| `p` | Full **Prompt** (file viewer) | `agent` / `agent_session` |
| `q` | Full **Question** (file viewer) | `human` / `human_session` |
| `o` | Full **Outcome** | `agent` / `agent_session` |
| `a` | Full **Answer** | `human` / `human_session` |
| `e` | **Error** (on failure) | all nodes |
| **`s`** | **Session History** | **`agent_session`** / **`human_session`** multi-turn only |
| `Tab` | Enter reply mode (focus editor) | only `waiting_for_human` human nodes |
| `←` / `Esc` | Return to phase details |

### File Viewer

When viewing logs, Prompt, Outcome, or Error, a full-screen file viewer opens:

| Key | Action |
|-----|--------|
| `↑` / `↓` | Scroll up / down |
| `PgUp` / `PgDn` | Page up / down |
| `Home` / `g` | Jump to the beginning |
| `End` / `Shift+g` | Jump to the end |
| `Esc` | Exit the viewer |

---

## Workflow Status Reference

Status labels in **`/swarmflows`** match engine events:

### Workflow

| Status | Description |
|--------|-------------|
| `planned` | Planned, not yet started |
| `pending` | Created, awaiting scheduling |
| `running` | Executing |
| `completed` | All phases finished |
| `failed` | Script error or token budget exceeded |
| `stopped` | User interrupt or session ended |

### Phase

| Status | Description |
|--------|-------------|
| `planned` | Not started yet |
| `running` | In progress |
| `completed` | All nodes in this phase reached a terminal state |
| `failed` | Phase error, or sealed when the run reaches a terminal state |
| `stopped` | Sealed on run terminal or session teardown |

### Node

| Status | Description |
|--------|-------------|
| `running` | Executing (including multi-turn `agent_session`) |
| `waiting_for_human` | Awaiting HITL reply (`human` / `human_session`) |
| `completed` | Finished successfully |
| `failed` | Execution failed |
| `stopped` | Sealed on run/phase terminal or session teardown |

---
