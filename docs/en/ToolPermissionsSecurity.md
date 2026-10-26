# Tool Permissions & Security

This document explains how JiuwenSwarm **tool call permissions** (`allow` / `ask` / `deny`) take effect, how they relate to **workspace-external paths**, **built-in security rules**, **user approval persistence**, and what the **CLI `/add-dir`** command changes in configuration.

The main configuration file is typically `~/.jiuwenswarm/config/config.yaml`; you can override this via the `JIUWENSWARM_CONFIG_DIR` environment variable (consistent with [Configuration](Configuration.md)).

---

## 1. Overview

| Capability | Description |
|-----------|-------------|
| **Three-tier actions** | `allow` executes directly; `ask` requires user confirmation (Web/CLI, etc.); `deny` rejects. |
| **Master switch** | `permissions.enabled`; when disabled, the engine returns `allow` for all tool calls (still recommended to keep enabled in production). |
| **Policy mode** | When `permissions.schema: tiered_policy` (and compatible aliases), **tiered policy** is enabled; otherwise, legacy tool-level matching is used. |
| **Channels subject to checks** | Only when `channel_id` belongs to the engine's约定 set (`web` / `acp` / `cli`, etc.) are tool permission checks performed; others may skip. |

In digital persona and group chat scenarios, `ask` may be downgraded to `deny` — see [Channels](Channels.md) for `owner_scopes` details.

---

## 2. Tiered Policy Resolution — How a Tool Call Gets Its Level

This corresponds to `evaluate_tiered_policy()` in the `openjiuwen` harness SDK. Parameters are the current `tool_name` and `tool_args` (e.g. bash `command`, file-read `path`, etc.). The tiered policy engine is part of the `openjiuwen.harness.security` module shipped with the harness SDK.

### 2.1 Setup: `permission_mode` and `severity`

- `permissions.permission_mode`: `normal` (default) or `strict`.
- If a parameter-level rule (built-in or user) specifies an explicit **`action: allow|ask|deny`**, that action is used **directly** — `severity` is not consulted.
- Without an explicit `action`, **`severity`** is mapped to an action based on `permission_mode`:

| severity | normal mode | strict mode |
|----------|-------------|-------------|
| LOW | allow | allow |
| MEDIUM | allow | **ask** |
| HIGH | **ask** | **ask** |
| CRITICAL | **ask** | **deny** |

Unknown `severity` is treated as **HIGH**.

### 2.2 How Parameter-Level Rules "Match"

- A rule must include the current **`tool_name`**, and all tools listed in the same rule must belong to the **same category** (shell / path / network); otherwise the rule is skipped.
- **Shell category**: `pattern` matches the full `command` / `cmd` string (supports glob and `re:` regex).
- **Path category**: `pattern` matches path-like strings extracted from `tool_args` (common key names + path-like values); `re:` patterns normalize `\` → `/` before matching.
- **Network category**: Currently not matched by parameter rules per product design (see code comments).
- A single call can **match multiple** parameter rules; the **strictest** result wins (see §2.4).

### 2.3 `evaluate_tiered_policy` Step-by-Step (Core Logic)

1. **Whole-tool baseline** `_baseline_level`: reads `permissions.tools.<tool_name>`.
   - If **`deny`**: **immediately return DENY** — no parameter rules, overrides, or defaults are consulted.
   - If `allow` / `ask` / unconfigured: noted for later use when no parameter-level match occurs.

2. **Collect built-in parameter rule hits** `builtin_hits` (from `builtin_rules.yaml` via `get_builtin_security_rules()`).
   - If **any** hit is **DENY**: **immediately return** (finalized from built-in hits only; built-in **DENY takes precedence over same-level other results**).

3. **Collect user parameter rule hits** `user_hits` (from `permissions.rules`).
   - If **any** hit is **DENY**: **immediately return** (user parameter-level **DENY takes effect at this stage**).
   - Note: this happens **before** `approval_overrides`, so a user rule `deny` can block an override that would otherwise match (the override is never evaluated).

4. **`approval_overrides`** (only `action: allow` entries participate):
   - If **at least one** matches the current `tool_name` + `tool_args`: **immediately return ALLOW**, with `matched_rule` prefixed `tiered_policy:approval_overrides:...`.
   - Does **NOT** override step 2's built-in DENY (never reached). Does **NOT** override step 3's user parameter DENY.

5. **If not returned by override**: if **`builtin_hits` is non-empty**, **`_finalize_hits(builtin_hits)` is returned** — **`user_hits` are NOT used** for final level merging.
   - Meaning: if built-in has any parameter-level hit (and no deny or override has triggered), **only built-in hits determine the result**; user `rules` hits on the same call do not participate in cross-layer "strictest" merging.

6. **If `builtin_hits` is empty** and **`user_hits` is non-empty**: `_finalize_hits(user_hits)` is returned.

7. **If no parameter-level matches at all**: if the tool baseline **`bl` is configured** (`allow`/`ask`), return that level and `tools.<name>`.

8. Otherwise, if **`defaults."*"`** exists, parse it as a level and return.

9. Otherwise return **ASK** with `matched_rule` = `tiered_policy:fallback(no_config)` (the engine may treat this as "no configuration").

`_finalize_hits`: if the hit list contains **DENY**, result is **DENY**; otherwise, take the **strictest** of `allow/ask` hits (strictness order: `deny > ask > allow`).

### 2.4 What the Engine Does After `tiered_policy`

In `PermissionEngine.evaluate_global_policy_directly`, after getting the `evaluate_tiered_policy` result:

1. **`maybe_escalate_shell_operators`**: if the result is **NOT** from `approval_overrides`, and the tool is shell-type, **allow** may be escalated to **ask** if the command contains chaining/injection-risk characters.
2. **`external_directory` (optional)**: if `include_external_directory=True`, `ExternalDirectoryChecker` evaluates paths outside the workspace; the result is **strictest-merged** with the current level. `matched_rule` may get `|external_directory.*` appended.

On the `check_permission` path, tiered results are typically computed **without** external-directory first, then merged separately — this is equivalent to "compute in one step" from the user-visible perspective (see `core.py` for details).

---

## 3. Built-in Security Rules `builtin_rules.yaml`

- **Package default**: `jiuwenswarm/resources/builtin_rules.yaml`.
- **User override**: A `builtin_rules.yaml` in the **same directory** as `config.yaml` (i.e. `JIUWENSWARM_CONFIG_DIR` or default `~/.jiuwenswarm/config/`) takes **priority** if it exists.

Built-in rules mostly cover **shell high-risk commands** (deletion, formatting, download-and-execute, privilege escalation, etc.), some with explicit `action: deny`. User `rules` cannot override built-in denials (built-in deny returns first).

---

## 4. `external_directory` (Workspace-External Paths)

When a tool call involves **paths outside the agent workspace**, `ExternalDirectoryChecker` applies an additional check based on `permissions.external_directory`:

- Configured as a **dict**: `"*": ask` means default to asking for external paths; **specific prefixes** can be set to `allow` / `deny` / `ask`.
- **Keys** are path prefixes (use forward slashes, e.g. `C:/Users/me/data`); **overly short keys** (e.g. just a drive letter without `/`) may be skipped in implementation to avoid matching entire drives.
- **Tools subject to checking** include:
  - **Shell**: `bash`, `mcp_exec_command`, `create_terminal` (path extraction from command strings per rules).
  - **Path tools**: same set as in tiered path-tool matching (`read_file`, `write_file`, `list_dir`, `grep`, etc. — paths collected from arguments).

Configuring `approval_overrides` without `external_directory` may still result in `ask` on the external-directory dimension; they are commonly used together.

---

## 5. `approval_overrides` (User "Always Allow" Persistence)

When the user selects **"Remember this rule"** or equivalent persistence logic during approval, an entry is appended to `permissions.approval_overrides`. Fields typically include:

- `id`, `tools`, `match_type` (`path` / `command`), `pattern`, `action` (e.g. `allow`), `source` (e.g. `user_approval`, `cli_add_dir`).

**Shell-type** `pattern` prefixed with `re:` is a regex matching the **entire command string**.
**Path-type** `re:` patterns normalize `\` → `/` in path strings before matching.

---

## 6. `/permissions` Command Usage Guide

The TUI `/permissions` command is a quick interface for managing tool permissions and rules. It supports **viewing** all permissions, **setting tool-level permissions**, and **creating parameter-level rules**.

### 6.1 View All Permissions (No Arguments)

```
/permissions
```

Called without arguments, it requests both `permissions.tools.get` and `permissions.rules.get`, outputting two sections:

```
── Tool Permissions ──
  bash          →  ask
  write_file    →  ask
  read_file     →  allow
  mcp__context7 →  allow

── Rules ──
  [cli_rule_bash_ls_*]  bash  pattern: ls *  action: allow
  [rule_001]  write_file  pattern: re:.*\.env$  action: deny
```

### 6.2 Set Tool-Level Permissions

```
/permissions <allow|ask|deny> <tool_name>
```

Calls `permissions.tools.update` to write the specified tool into `permissions.tools`. Tool names are normalized to lowercase.

**Examples:**

| Command | Effect |
|---------|--------|
| `/permissions ask write_file` | Requires confirmation before writing files |
| `/permissions allow bash` | Allows bash to execute directly |
| `/permissions deny bash` | Rejects all bash calls (highest priority — skips parameter rules) |

### 6.3 Create Parameter-Level Rules (with Pattern)

```
/permissions <allow|ask|deny> <tool_name>(<pattern>)
```

Calls `permissions.rules.create` to create a parameter-level rule. If a rule with the same ID already exists (ID collision), it automatically falls back to `permissions.rules.update`.

**Pattern syntax:**

- Plain text: literal match, e.g. `ls *` (glob-style wildcard).
- Regex: prefixed with `re:`, e.g. `re:.*\.env$` (matches the full command string).

**Direct `action` field (no severity mapping):**

The `/permissions` command writes the user's `allow/ask/deny` choice **directly into the rule's `action` field**, rather than indirectly mapping through `severity`. When the engine resolves a parameter-level rule, if an explicit `action` is present, it **uses that action directly — bypassing the severity mapping table** (see §2.1).

This means:
- `/permissions deny bash(re:.*rm -rf.*)` → `action: deny` → **rejects in any mode** (independent of `permission_mode`).
- `/permissions ask write_file(re:.*\.env$)` → `action: ask` → **always requires confirmation**.

> **Why not severity?** The previous implementation mapped `deny` → `severity: CRITICAL`, but in `normal` mode CRITICAL resolves to `ask`, not `deny` — contradicting the user's intent. Writing `action` directly ensures the user's `allow/ask/deny` intent is faithfully expressed.

**Rule ID generation:** Format is `cli_rule_<tool>_<pattern-escaped>`, e.g. `cli_rule_bash_ls_*`.

**Examples:**

| Command | Generated rule |
|---------|---------------|
| `/permissions allow bash(ls *)` | tools: `[bash]`, pattern: `ls *`, action: `allow` |
| `/permissions deny bash(re:.*rm -rf.*)` | tools: `[bash]`, pattern: `re:.*rm -rf.*`, action: `deny` |
| `/permissions ask write_file(re:.*\.env$)` | tools: `[write_file]`, pattern: `re:.*\.env$`, action: `ask` |

### 6.4 Error Messages

| Condition | Message |
|-----------|---------|
| Level not in allow/ask/deny | `无效级别 "xxx"，仅允许：allow、ask、deny` |
| Empty tool name | `工具名不能为空。` |
| API request failure | Shows the specific RPC method name and error message |

### 6.5 Mapping to Configuration

| Command action | Config path written | RPC method |
|---------------|---------------------|------------|
| `/permissions ask bash` | `permissions.tools.bash = "ask"` | `permissions.tools.update` |
| `/permissions allow bash(ls *)` | New entry in `permissions.rules` array | `permissions.rules.create` |
| View without arguments | Reads `permissions.tools` + `permissions.rules` | `permissions.tools.get` + `permissions.rules.get` |

> **Note:** `/permissions` sets global tool permissions. For digital persona / group chat scenarios, `owner_scopes` permissions must be configured separately via the channel panel — see [Channels](Channels.md).

---

## 7. CLI: `/add-dir` and `persist_cli_trusted_directory`

In the terminal TUI, `/add-dir <path>` sends a `command.add_dir` request. The server calls `persist_cli_trusted_directory`. Key behaviors:

1. **`external_directory`**: writes an `allow` entry for the resolved directory path (keyed by normalized path with forward slashes).
2. **If `schema` is `tiered_policy`**: appends or updates two **`approval_overrides`** entries (one for **path tools**, one for **shell tools**), with `source` = `cli_add_dir`.
3. **Shell-side pattern**: currently generates a literal match fragment using **forward-slash paths** (e.g. `re:.*C:/Users/me.*`), avoiding `C:\Users` in YAML double-quotes which would cause regex `\U` illegal escape and invalidate the entire rule.
   At runtime, command text has `\` → `/` normalization applied before matching, ensuring compatibility with Windows command syntax.

If not using `tiered_policy`, typically **only** `external_directory` is updated; `approval_overrides` is not written (log will explain).

---

## 8. Related Files (Developer Reference)

| Module | Path |
|--------|------|
| Tiered policy | `openjiuwen.harness.security` (harness SDK) |
| Permissions persistence | `jiuwenswarm/agents/harness/common/rails/permissions/permissions_persist.py` |
| Owner scopes | `jiuwenswarm/agents/harness/common/rails/permissions/owner_scopes.py` |
| Tool permission RPC | `jiuwenswarm/agents/harness/common/rails/permissions/permissions_config_rpc.py` |
| Tool permission context | `jiuwenswarm/agents/harness/common/rails/permissions/tool_permission_context.py` |
| TUI `/permissions` | `jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/permissions.ts` |

---

## 9. See Also

- [Configuration](Configuration.md): `JIUWENSWARM_CONFIG_DIR`, configuration file location.
- [CLI Commands](CLI.md): CLI/TUI entry points (including slash commands).
- [Channels](Channels.md): `owner_scopes`, digital persona, and `ask` downgrade.