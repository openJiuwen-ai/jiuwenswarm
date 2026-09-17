# Slash Command Architecture

> **Document type**: Architecture and evolution conventions — describes "single source of truth", layering boundaries, and implementation principles.
> **Relationship to other docs**: See [`SlashCommands.md`](./SlashCommands.md) for the command reference table; [`CLI.md`](./CLI.md) for the CLI command list. This document does **not** enumerate all commands — it defines **how they are organized and how to prevent drift**.

---

## 1. Background & Problem

Slash-prefixed commands (`/`) are currently implemented in multiple places:

- **Gateway**: `MessageHandler._handle_channel_control` handles `/new_session`, `/mode …`, etc. on controlled channels, deciding whether to **intercept and NOT forward to Agent**.
- **IM Pipeline / Channels**: e.g. `gateway/im_pipeline/im_inbound.py` maintains its own control message set; Feishu/WeCom channels may have text-based checks that **diverge from the main logic**.
- **CLI TUI**: `jiuwenswarm/channels/tui/frontend/src/core/commands/` has a local registry; some commands call the backend via WebSocket, others are purely local.

Without clear layering and a single source of truth, the project risks: **semantic inconsistencies for the same command name, documentation-code drift, and duplicated parsing logic in new channels.**

---

## 2. Design Goals

| Goal | Description |
|------|-------------|
| **Single Source of Truth (SSOT)** | For gateway-controlled slash commands, the command name, valid forms, and match semantics should be defined in one place. Other modules **reference** rather than copy. |
| **Clear Boundaries** | Distinguish "Gateway-only", "Client-only", and "Name-aligned, execution-split" categories. Avoid stuffing all product slash commands into a single Gateway module. |
| **Evolvable** | Adding a new controlled channel only requires checking the registry and `_control_channel_types` policies. |
| **Consistent with REPL conventions** | Local-UI slash commands (help, diagnostics) are **resolved client-side first**; **unrecognized `/xxx` should NOT be forwarded as plain user content** by default (see REPL design doc). |

### 2.1 Non-goals

- All slash commands need NOT be executed in the Python Gateway (CLI-exclusive commands remain client-side).
- A single big-bang refactor is not required; incremental migration of constants and parsing to SSOT is acceptable.

---

## 3. Three-Layer Classification (Mandatory)

### 3.1 Category A: Gateway Channel Control

**Definition**: User messages arriving at the Gateway that are identified by `MessageHandler` (or its unified entry point), potentially **only changing session/mode/routing without entering Agent conversation**.

**Typical commands**: `/new_session`, `/mode agent|code|team` and `/switch plan|fast|normal|team` on controlled channels, plus direct forms like `/mode agent.plan|agent.fast|code.normal|code.team` (per current implementation). The TUI local command additionally supports `/mode plan` and `/mode team.normal`; those forms are not part of the Gateway controlled-channel whitelist.

**Requirements**:

- Valid forms, match semantics (exact / prefix / multiline), and error messages should be described by the **SSOT**. `im_inbound` and IM channels that need pre-filtering must **import the same SSOT** — private subset copies are prohibited.
- Registry entries must specify **applicable channel types** (aligned with `_control_channel_types` and `_session_map_channel_types`), avoiding blanket "all channels" defaults.

### 3.2 Category B: Client-Only

**Definition**: Commands parsed and executed **locally** in CLI TUI, Web REPL, etc., or commands that should NOT be semantically intercepted by the Gateway.

**Typical commands**: UI toggles, `/resume`, `/model` (if purely local config), help/diagnostic stubs — per [`CLI.md`](./CLI.md).

**Requirements**:

- **NOT** included in the Gateway intercept table as the sole truth. If backend mode alignment is needed, use existing protocol fields (e.g. `params.mode`), not re-parsing CLI aliases in the Gateway.
- Document the **processing process**: Node CLI / browser frontend, etc.

### 3.3 Category C: Name-Aligned, Execution-Split (Hybrid)

**Definition**: Commands where the **name** and help text should be consistent across CLI and IM, but **execution** spans client + backend (e.g. local validation then RPC).

**Requirements**:

- Add `canonical_name` + `cli_alias` fields in the SSOT or doc matrix, so name mismatches are traceable.
- The Gateway **need NOT** understand CLI aliases; if future server-side recognition is needed, add it as a new requirement and extend the protocol explicitly.

---

## 4. Single Source of Truth (SSOT) — Proposed Shape

### 4.1 Module Location (Proposed)

- **Python side**: Implemented as `jiuwenswarm/gateway/slash_command.py` (controlled channel parsing, `CONTROL_MESSAGE_TEXTS`, first-batch command metadata `FIRST_BATCH_REGISTRY`). May evolve into `channel_control_slash.py` etc.
  - **Data**: The set of controlled commands, match rules (exact / prefix / no-multi-line), metadata (description, whether to forward to Agent).
  - **Pure functions**: Given a channel type and user text, return a **structured decision** (not hit / hit & valid / hit & invalid). **No IO** in SSOT module (no `create_task` for notifications).
- **Naming suggestion**: If only containing Category A, the module name should avoid suggesting "all product slashes", preventing future contributors from stuffing Category B logic into the Gateway.

### 4.2 Registry Minimum Fields (Proposed)

| Field | Description |
|-------|-------------|
| `id` | Stable internal identifier (e.g. `new_session`, `mode_switch`). |
| `patterns` | Valid user input forms (including whether to match whole-line only). |
| `scope` | `gateway` (this design's scope). |
| `channels` | `all_controlled` or explicit list, aligned with config channel types. |
| `intercept` | Whether to withhold from Agent when hit and valid (per `_handle_channel_control` semantics). |
| `notes` | Cross-reference to CLI aliases and doc anchors (links to `CLI.md` sections). |

### 4.3 Relationship to `MessageHandler`

- **Parsing & decision**: Call the SSOT module, reducing inline `startswith` chains in `message_handler.py`.
- **Side effects**: Still in `MessageHandler` (or a dedicated service class) for task cancellation, notification dispatch, etc. — avoiding async side effects in the "table module" for testability.

---

## 5. How Each Entry Point Integrates (Principles)

| Entry Point | Principle |
|-------------|-----------|
| **Gateway `MessageHandler`** | Before controlled-channel user text enters unified control logic, use SSOT for decisions. |
| **`im_inbound` etc.** | If pre-filtering or statistics are needed, **import the same constants/functions** — no independent `frozenset` subsets. |
| **Feishu / WeCom etc.** | If lightweight pre-checking is needed at the Channel layer, **reuse SSOT** or only do coarse "might be a control command" filtering. Final semantics always per Gateway. If dual-point checking exists, document in registry `notes` and load-test to avoid duplicate prompts. |
| **CLI** | Continue maintaining the TS `registry`; align names with Category A per [`CLI.md`](./CLI.md) and §3.3 above. Do NOT duplicate Category B logic in the Gateway. |

---

## 6. "Unknown Slash" Protocol Conventions

- **Client (REPL/TUI)**: Input starting with `/` should go through the local command router first. Unrecognized commands should prompt the user and **NOT** be sent as plain conversation content (per product UX).
- **Gateway**: Only guarantees SSOT-based handling of Category A for **channels that actually reach the Gateway**. Does not need to recognize all CLI-only commands.

---

## 7. Testing & Quality Assurance

- Provide **unit tests** for the SSOT: cover exact match, illegal suffix, multiline text, `/mode` first-level vs. direct-value forms, `/switch` valid/invalid combos, etc. — consistent with production behavior.
- Regression note: **SessionMap** channel family vs. regular controlled channels' `session_id` behavior is still managed by the existing state machine. SSOT only addresses string-level consistency and maintainability.

---

## 8. Phased Implementation (Proposed)

| Phase | Content |
|-------|---------|
| **P0** | Extract Category A commands into SSOT module; `im_inbound` etc. switch to imports, eliminating set drift. |
| **P1** | Converge inline parsing in `message_handler.py` to SSOT calls; behavior unchanged. |
| **P2** | Fill in Category B/C cross-references with `CLI.md` in the doc matrix. Optional: generate read-only manifest for frontend (if unified completion is needed). |

---

## 9. Summary

- The "table" in the Gateway should primarily host **Category A (channel control)** SSOT, not all product slash commands.
- **Category B** stays client-side; **Category C** uses documentation and fields for name alignment — centralized execution logic is unnecessary.
- **Success criterion**: changing a command set in one place no longer causes divergence in pipelines and Gateway main paths; the CLI/IM experience boundary remains clear and documentable.

---

## 10. Current Command Status (based on `gateway/slash_command.py`)

The current command list has been split into a separate document: [`SlashCommands.md`](./SlashCommands.md).
