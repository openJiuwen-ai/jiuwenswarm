# Tool Permissions & Security

This document explains how JiuwenSwarm **tool call permissions** (`allow` / `ask` / `deny`) take effect, how they relate to **workspace-external paths**, **built-in security rules**, **user approval persistence**, and what the **CLI `/add-dir`** command changes in configuration.

The main configuration file is typically `~/.jiuwenswarm/config/config.yaml`; you can override this via the `JIUWENSWARM_CONFIG_DIR` environment variable (consistent with [Configuration](Configuration.md)).

Xiaoyi A2A `clientVariables.permission` travels in the request permission context and applies `default`, `auto_approve`, or `full_access` to a copy of each tool-permission snapshot. It does not write shared `config.yaml` or trigger a global reload. The Xiaoyi channel installs the local `PermissionRail`; initial checks and post-approval checks read the current request profile. Later messages without a profile use the shared configuration again. Desktop behavior security retains its host-owned checks. Global settings changed by older versions must be explicitly restored; upgrading does not infer or overwrite the user's current settings.

`mcp_exec_command` is an AgentServer command tool routed through its bound `SysOperation.shell()`. Register it with `get_mcp_tools(sys_operation=operation, agent_id=agent_id)`; multi-session sub-agents resolve that instance from their `sys_operation_id`. An unbound tool returns an error instead of launching a host process. Sandbox mode reuses the existing connector CLI, `excluded_commands`, and `fallback_on_failure` routing and passes the explicit `bash / sh / powershell / cmd` selection. Background requests use the same operation; unsupported providers return an error without tool-local host execution. Callers must still install permission rails; binding the execution route does not automatically enable FileGuard.

---

## 1. Overview

Before starting an internally managed Windows sandbox service, Swarm supplies common runtime declarations when `JIUWENBOX_TOOL_PATHS` is absent. It prefers the isolation venv, resolves its actual base Python, and finds standard Node and Git/Bash distributions on the service PATH. JiuwenBox prepares shared ACLs at service startup; sessions reuse those grants. Explicit declarations remain authoritative. This does not change shell approval rules or guarantee repair after arbitrary runtime upgrades. See [shared paths and environment configuration](../../jiuwenbox/docs/沙箱公共目录与环境变量配置.md) for the declaration and cache boundaries.

Managed venv bases are pinned in a protected host-user registry key. The host is trusted: an explicit `JIUWENCLAW_BASE_PYTHON` takes precedence over the old pin and reprovisions the venv. A removed base also triggers host discovery and reprovisioning. A host-recreated venv can replace the old pin when its actual base matches the independently selected host interpreter; no registry cleanup is required. Normal startup reuses a valid pin, allowing tool Python and runner Python to differ. New or recovered venvs are verified before updating the pin, running pip, or publishing the base directory. Writable `pyvenv.cfg` changes alone cannot authorize a base that differs from the host selection.

Ordinary venv provisioning failures are logged: the unusable `JIUWENBOX_VENV_DIR` is omitted, while host-selected `JIUWENBOX_BUNDLED_PYTHON` and `JIUWENCLAW_BASE_PYTHON` are still published. Trust-validation failures continue to block startup. On Linux, failure to resolve an existing venv falls back to subsequent host candidates, and `pyvenv.cfg` supports a `home` containing only `python3`. Optional Python discovery failures are logged and skipped without discarding other tool declarations.

Source launches declare the known `jiuwenbox/src` through `JIUWENBOX_SERVICE_READ_ROOTS` to enable sandbox-account imports. Skill directories are not automatically made shared read-only roots; their existing read/write permissions and session cleanup remain unchanged. This import fix does not resolve skill dependency scan latency or introduce a mandatory full scan on every startup.

JiuwenBox attempts bytecode migration for each declared read-only root only when object-level ACL preparation fails with Windows error 5 and no effective read denial applies. It removes only CPython cache files owned by the current sandbox account and backed by a source file inside the same canonical root. Original path components are still checked for links/reparse points before deletion; short aliases and `..` do not change the containment check. Directories (including empty `__pycache__` directories), unrelated artifacts, host-owned caches and orphan-owner files are not automatically removed or taken over. Required ACL failures remain fatal. Migration reuses the cold ACL walk; a cache hit adds no cleanup scan.

The sandbox first-hop runner (`jiuwenbox.supervisor.win_exec`) receives `PYTHONPYCACHEPREFIX` from `ProcessRuntime._create_windows` before it is spawned. The second-hop command receives its own prefix from `_create_process_as_user`, under sandbox Temp with the existing workspace fallback. This does not refer to the host box-server launched by Swarm's `ensure_running`. Explicit Python flags or code that clears its environment can override Python's default cache behavior; source read-only ACLs still apply.

ACL failure diagnostics include `root`, `path`, `operation`, `owner_sid`, `object_type`, `winerror`, `migration` and `required`. Owner metadata is queried only on failure; diagnostic failures do not replace the original exception. Candidate exclusions such as `directory_not_cache` or `owner_mismatch` are DEBUG messages, while required failures remain ERROR messages. Shared-root logs report `cache=hit/miss` and elapsed time. Stop affected processes and have the host administrator review the declared scope/policy and reinstall, rebuild or explicitly repair the identified runtime; no automatic elevation, ownership takeover or broad ACL reset is performed.

Windows commands do not receive the four service declarations `JIUWENBOX_SERVICE_READ_ROOTS`, `JIUWENBOX_TOOL_PATHS`, `JIUWENBOX_PATH_ALLOWLIST` or `JIUWENBOX_ENV_ALLOWLIST`, even through inherited allowlists or `JIUWENBOX_INJECT_ENV` (case-insensitive filtering). The service's declarations remain unchanged; existing execution protocol and host soft-delete settings retain their behavior.

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

4. **Legacy `approval_overrides`** (only `action: allow` entries participate; `exact_operation` records are evaluated after all guards, as described in §5):
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

## 5. Dynamic ASK Approval

An ASK interrupts before the tool runs. The user can allow once, allow for the current session, always allow, or reject. Session grants are stored in the session overlay; permanent grants are stored in `permissions.approval_overrides`. Remembering another operation permanently does not promote existing session grants.

`authorization_mode: allow` permits only the current object: resolved file paths and access actions, the exact URL, or the original unsplit command and working directory. `allow_with_scope` permits a server-derived scope: a file's immediate parent directory (including descendants), a URL's registrable domain and subdomains (keeping its scheme and port), or separately reusable shell subcommands bound to the original invocation directory. IP addresses, localhost and public suffixes do not offer domain broadening.

Object records use `match_type: exact_operation` and a structured JSON `pattern`; the engine checks them after all guards. Split shell approvals create a batch of `match_type: shell_subcommand` records, applied within ShellGuard before other guards are merged. They lift ordinary shell tool/command ASK or undetermined results while preserving structural checks and FileGuard, NetGuard and DeletionGuard decisions. Neither type overrides DENY, including a denial added while approval was pending. Existing overrides retain their matching behavior; saving a batch permanently does not promote unrelated session grants.

The Web configuration panel displays **Sandbox → File Security Guard → Shell Security Guard → Network Security Guard**. The Sandbox card has one action row: **Sync file guard**, **Sync network guard**, then **Apply and recreate sandboxes**. Both sync actions only save their respective rules; the third applies both together. The separate Network Security Guard card configures `allow`, `ask` and `deny` rules.

### 5.1 NetGuard Configuration RPCs

| Method | params | Response payload / behavior |
| --- | --- | --- |
| `permissions.net_guard.get` | `{}` | `net_guard`, `builtin_urls`, `enforcement_points`, `apply_mode: "hot_reload"`, `host_exit`, `warnings` |
| `permissions.net_guard.set` | `{"net_guard": {...}}` | Save a partial guard update; same response shape as get. Does not synchronize sandbox rules |
| `sandbox.network.get` | `{}` | `{"network": {"disable_all": false, "allow_domains": [], "deny_domains": []}}`; reads user settings from the current platform's runtime policy copy |

The existing get/set API now supports ASK. Updatable fields are boolean `enabled` / `enforce_host_exit`, `defaults: allow|ask|deny`, and `urls: {pattern: allow|ask|deny}`. Omitted fields stay unchanged; supplying `urls` replaces the entire user rule map, and `{}` clears it. Built-in DENY rules are merged at runtime and cannot be changed to allow or ask under the same pattern. Invalid values return `BAD_REQUEST`.

Example Web request:

```json
{
  "type": "req",
  "id": "net-guard-set-1",
  "method": "permissions.net_guard.set",
  "params": {"net_guard": {
    "enabled": true,
    "defaults": "allow",
    "urls": {"blocked.example.com": "deny", "review.example.com": "ask"}
  }}
}
```

ASK interrupts supported webpage fetch tools before execution. The host HTTP exit checks every hop: DENY always blocks, and a redirect target requiring ASK is stopped before connection. The error identifies the target URL for a separate tool call and approval; the exit does not open an approval dialog or carry the original URL grant to the redirect target. Sandbox process traffic remains subject to sandbox policy.

Across matching rules, DENY takes precedence, followed by ASK and then ALLOW. An allow rule does not override a matching ask or deny rule.

### 5.2 New RPC: `sandbox.network.sync`

`params` must be an empty object. The server reads saved NetGuard settings; clients cannot supply rules or widened scopes here. Requires sandbox type `jiuwenbox`, `permissions.enabled: true`, and `permissions.net_guard.enabled: true`. The sandbox itself need not currently be enabled.

```json
{"type":"req","id":"net-sync-1","method":"sandbox.network.sync","params":{}}
```

For the guard configuration above, the Web response is:

```json
{
  "type": "res",
  "id": "net-sync-1",
  "ok": true,
  "payload": {
    "urls": {"blocked.example.com": "deny"},
    "skipped": [{"pattern":"review.example.com","reason":"询问仅在工具调用前处理，不同步到沙箱"}],
    "restart_required": true
  }
}
```

| Field | Meaning |
| --- | --- |
| `urls` | Domain-to-allow/deny mapping saved to `config.yaml` under `sandbox.urls`; replaces the previous mapping |
| `skipped` | `{pattern, reason}` entries, or `[]`; partial skipping still returns success. Current reason strings are server-generated Chinese text |
| `restart_required` | Always true; call `sandbox.restart` to apply both sets of rules |

Only allow/deny rules for plain domains or `*.domain` are copied. **ASK is skipped, never mapped to DENY**. Full URLs, ports and unsupported wildcard patterns are also skipped rather than broadened. Non-allow defaults are reported with `pattern: "defaults"` because domain lists cannot represent them. If all rules are skipped, `sandbox.urls` becomes `{}`. Original NetGuard rules are unchanged. Sandbox allowlist, blocklist and base-policy semantics still apply; synchronization is not a complete translation of NetGuard.

Like file sync saving `sandbox.files`, network sync only saves `sandbox.urls` in `config.yaml`. It does not change the runtime policy copy or start, reload or recreate sandboxes. `ok: true` means saved, not applied. Example saved configuration:

```yaml
sandbox:
  files: []
  urls:
    example.org: allow
    blocked.example.com: deny
```

`sandbox.files` and `sandbox.urls` store sandbox-supported rules synchronized from their respective Guards. `sandbox.restart` reads both saved configurations, applies them and rebuilds the sandboxes. It does not reread Guard rules or copy runtime rules back into configuration. Guard edits require another sync to update the pending configuration. Network application converts `sandbox.urls` into domain lists in the platform runtime policy copy, preserving `disable_all`. The defaults `files: []` and `urls: {}` both mean empty rules. Starting the internally managed service also loads the current configuration. Applying network rules fails explicitly if a custom policy does not use the platform runtime copy.

Read pending rules from the sync response or `config.yaml`. The separate legacy `sandbox.network.get/set` interfaces still operate on the runtime policy copy. They do not participate in Guard synchronization or read pending `sandbox.urls`. Unified application uses the saved sandbox configuration.

Both sync actions feed the same **Apply and recreate sandboxes** action: `{"type":"req","id":"apply-1","method":"sandbox.restart","params":{}}`. It waits for the service to load the saved network policy, reloading when the runtime policy copy changed, then recreates active instances with the saved file rules. Unchanged policy reuses a healthy service. Reload/recreation stops current tasks; AgentServer itself is not restarted.

When an internally managed box-server exits or moves ports, the runner retires the old URL's provider cache and pending deletions only after confirming its owned process has exited and completing Windows Job teardown. Delete lifecycle hooks remain strict; cleanup failures block replacement startup and are retried on the next attempt. A connection error, timeout, or changed port alone never authorizes discarding deletion state. Unreachable external services still fail and retain pending deletes.

On Windows, the console-less managed box-server receives normal Uvicorn shutdown requests through a private parent/child stdin pipe, without relying on `CTRL_BREAK_EVENT`. Source and packaged entry points share this path, including windowed executables with no `sys.stdin`. Control-module import or stream-write failures retain the existing signal/terminate fallbacks; non-Windows processes without stdin never call Win32 APIs. HTTP request draining is limited to five seconds; full lifespan sandbox/ACL cleanup still runs afterward, retaining the runner's existing 60-second process-exit fallback. Sandbox switch RPCs, TUI `/sandbox enable|disable` commands, and `sandbox.restart` share one configuration-application lock across writes, stop, strict cleanup, and readiness. Requests acknowledge only after completion; enabling cannot overwrite configuration while disabling is in progress. Internal restart callbacks for managed configuration reuse the caller's lock without acquiring it again. Real hidden Windows child-process exit tests carry the `integration` marker and can be excluded with `pytest -m "not integration"`.

Disable (including the legacy `/sandbox disable` command) and restart share lifecycle-aware cleanup of cached and pending sandboxes across all URLs known to this AgentServer process. Disable also unregisters session/project sandbox SysOperations and stops its owned service. A failed stop does not skip bulk cleanup: a strict cleanup failure takes precedence, while successful cleanup still propagates the original stop error. Resubmitting the same disabled configuration or command retries failed cleanup. Restart finishes preparation for active adapters and cleanup of orphaned scopes before creating any replacement instance. Bulk cleanup attempts all matching entries, converges successful entries, retains failed entries for retry, and raises the first strict error after traversal. Any strict cleanup failure prevents the creation phase, preserving the Windows ordering of all old ACL cleanup before any new ACL installation.

New sessions and sandbox switches use the current switch to validate the executor mode. The next idle lookup of an existing session rebuilds its runtime if its SysOperation mode disagrees with that switch. Disabling selects a local SysOperation; enabling selects a sandbox SysOperation. The normal history warmup restores the same session ID, retaining the instance configuration, mode, and submode while rebuilding skills and tools. Repeated switches require no new conversation. The session lock serializes concurrent lookups; executing turns keep their existing mode until idle. Matching runtimes keep being reused; other sessions switch on their own next lookup. Creation and rebuild validate the executor before starting the interaction. Failures report the expected and actual modes with configuration retry guidance and clean up the unpublished runtime. Enabling without a valid endpoint rejects even the first call of a new session, rather than silently using a local executor.

Runtime rebuilds reuse the full `cleanup()`, which clears the session's file-send deduplication records. The same file may therefore be sent again after switching modes.

The paired agent-core cleanup and retirement paths also invalidate Gateway endpoint/provider caches and derived sandbox IDs, so reopening the same session cannot reuse an old port or policy. Retirement affects only the matching service URL; bulk cleanup affects only JiuwenBox. Bulk cleanup gates new acquisitions and waits for initial creations and automatic recreations already in progress before deleting their IDs, including external mode and cleanup after a managed stop failure. DELETE failures remain pending and strict cleanup cannot report success early. Regression tests exercise real SysOperation → Gateway → provider → HTTP client routing, mocking only remote HTTP, across same-port/changed-port reuse, session disable/enable, and concurrent creation/cleanup with deletion retry.

Windows manual regression: enable and create a sandbox, kill the owned box-server, then change a file/network rule to trigger `security.config.sandbox.update` application. Recovery must return `saved: true, applied: true`, including when the port changes, without retrying the confirmed exited endpoint. If another injected fault prevents application, removing that fault and resubmitting the same configuration must recover. An unchanged configuration already marked applied may still return `not_required`. Disable must converge session/project registrations, old-URL caches, and pending deletes. An unreachable external service must still return `saved: true, applied: false`; restoring that service and resubmitting the same configuration must retry successfully. Ship the host change together with agent-core's `retire_jiuwenbox_service` API.

Success payload example: `{"restarted":2,"scope":"all_active_sandboxes","status":"applied"}`. If neither instances were recreated nor service policy reloaded, status is `no_active_sandboxes`. A completed service-policy reload with no active instances can return `applied` and `restarted: 0`. Requires an enabled sandbox. Internal services reload automatically; external mode still recreates instances with file rules and adds `network_status: "externally_managed"` to the response. The UI states that the external service must load network policy; no external service configuration or process is changed. Internal service application failure prevents instance recreation. Service or instance failures return `SANDBOX_RESTART_FAILED`, including partial rebuild failure, and saved rules remain available for retry.

| Error code | Condition / message |
| --- | --- |
| `AGENT_NOT_READY` | Web forwarding cannot reach a ready AgentServer: `AgentServer is not ready` |
| `BAD_REQUEST` | Nonempty params: `sandbox.network.sync accepts no parameters` |
| `BAD_REQUEST` | Unsupported provider: `sandbox.network.sync currently supports jiuwenbox only` |
| `BAD_REQUEST` | Permissions or NetGuard disabled: `NetGuard must be enabled before synchronization` |
| `INTERNAL_ERROR` | Failure to read or save `config.yaml` |

Runtime policy write/readback failures during application, such as `Sandbox network configuration could not be saved`, are returned by `sandbox.restart` as `SANDBOX_RESTART_FAILED`.

Web errors have the shape `{"type":"res","id":"net-sync-1","ok":false,"error":"...","code":"BAD_REQUEST"}`. Internal AgentServer RPCs use `AgentResponse`: success data is in `payload`, while failures have `ok: false` and `payload: {"error":"...","code":"..."}`. These are different envelopes.

### 5.3 Dynamic Authorization Protocol Fields

The existing `chat.ask_user_question` event uses `source: "permission_interrupt"`. Each `questions[]` entry may include `authorization_scopes: [{value, label}]`. This is event metadata, not a new RPC. The server default and Web prompt select the available broadened scope: `parent` for files, `domain` for URLs, and `subcommands` for shell calls. Clients omitting scope fields use the same server default. Users can explicitly select `exact`; the default does not approve execution. Unsupported operations or missing metadata remain exact-only, and each new request starts with its own default. For `https://api.example.com/data`, an example scope list is:

```json
[
  {"value":"exact","label":"仅当前对象"},
  {"value":"domain","label":"域名（含子域名）：example.com"}
]
```

The client submits optional fields in `chat.user_answer` → `params.answers[]`:

| Field / combination | Default and constraint |
| --- | --- |
| `authorization_mode` | When omitted, inferred from the resolved scope: `allow` for `exact`, otherwise `allow_with_scope`. Explicit `allow` without a scope selects `exact` |
| `authorization_scope` | When omitted, the server selects the applicable `parent`, `domain` or `subcommands`, falling back to `exact`. Explicit values must match the actual object type |
| `allow` + `exact` | Current tool and resolved file paths/actions, exact URL, or original unsplit command plus working directory. Command wildcard characters stay literal |
| `allow_with_scope` + `parent` | Immediate parent directory and descendants; preserves tool and access-action limits |
| `allow_with_scope` + `domain` | Registrable domain and subdomains, preserving scheme and port; e.g. `api.example.co.uk` → `example.co.uk`. Not offered for IP addresses, localhost or public suffixes |
| `allow_with_scope` + `subcommands` | One approval saves multiple literal child commands for independent or combined reuse; all retain the original invocation directory, tool, interpreter and lifetime |
| `allow_with_scope` + `exact` | Valid; remains an exact-object authorization |

Split scope reuses the existing AST and is offered only when it covers the whole input. Unreliably parsed structures, redirections and outer interpreter launchers fall back to `exact`. Custom shell tools bind all arguments conservatively and remain exact-only. Grants for bash, powershell and mcp_exec_command bind the original execution `workdir` (or contextual directory) and `shell_type`; an unrecognised `cwd` cannot override that binding. For `cd repo && git status` called from `E:\work`, both child grants bind `E:\work`, including the `git status` grant. FileGuard handles directory changes for its current path checks. Children may be reused alone or recombined from that initial directory; different arguments, interpreters or initial directories do not match. Unapproved children still require approval, and covered children are not executed early.

The core-to-Swarm persistence callback uses only `approval_grants: list[dict]`, for both session and permanent approvals. A single object is a one-element list; a single Shell scope approval includes its child grant and an exact grant for that invocation. Each remembered approval invokes one batch write, preserving the batch's session ID and lifetime. Only that batch is saved, never unrelated grants from the merged snapshot. The desktop sends the user's answer through `chat.send`; it does not implement this Python persistence callback.

An explicit `approval_grants=[]` succeeds immediately for session and permanent persistence without reading or writing configuration, clearing saved permissions, or falling back to the full snapshot. Internal management calls that omit the argument (`None`) retain snapshot handling. File, Net, and Shell keep their scope defaults and reusable shell combinations.

Older object command grants without interpreter information require approval again. Scope and lifetime are independent; allow-once does not save a scope for future calls. Clients must use server-provided option values and scope choices, and cannot supply arbitrary paths, domains or children. Old clients omitting the new fields also use the server default scope. Explicit `exact` retains whole-command authorization; existing exact grants are not automatically converted into child grants.

Ordinary permission prompts and ACP command prompts display the working directory captured when approval is requested: the tool's `workdir`, or the execution context directory when omitted. This display snapshot does not rewrite tool arguments and remains stable if the context later changes.

Example session grant using domain scope:

```json
{
  "type": "req",
  "id": "answer-rpc-1",
  "method": "chat.user_answer",
  "params": {
    "request_id": "permission-1",
    "answers": [{
      "selected_options": ["session_allow"],
      "authorization_mode": "allow_with_scope",
      "authorization_scope": "domain"
    }]
  }
}
```

The outer `id` identifies this RPC; `params.request_id` references the pending approval event. Send it through the original session connection/routing context.

| Choice | `selected_options[0]` | Internal confirmation fields | Lifetime |
| --- | --- | --- | --- |
| Allow once | `approve` | `approved: true, auto_confirm: false` | Current call only |
| Allow in session | `session_allow` | `approved: true, auto_confirm: true, persist_allow: false` | Current session overlay |
| Always allow | `always_allow` | `approved: true, auto_confirm: true, persist_allow: true` | Permanent user configuration |
| Reject | `reject` | `approved: false, auto_confirm: false` | Reject current call; no grant |

Internal confirmation also carries `feedback` (default empty string) and the scope fields. For legacy callers, `auto_confirm: true` with missing/null `persist_allow` means permanent persistence. New Web clients should use the explicit choices above.

Malformed confirmation structures or unknown enum values trigger another prompt. Structurally valid but invalid combinations, such as `allow/parent` or a command using `domain`, are rejected with `[PERMISSION_DENIED]`. Rejection returns `[PERMISSION_REJECTED]` or the supplied feedback. A new DENY added while awaiting approval also blocks resumption. Persistence failure rolls back the in-memory change and logs failure; the approved current call may still proceed.

`chat.user_answer` returning `accepted: true` only acknowledges receipt, not authorization, persistence or execution success. Continue observing interrupt/tool results. Dynamic grants do not synchronize sandbox configuration or override sandbox denials.

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
## 10. Local source protection for outbound file tools

`send_file_to_user` checks FileGuard read access for every source in `abs_file_path_list`, including native arrays, JSON or legacy Python array strings, and single paths. `save_media_to_gallery` and `save_file_to_file_manager` check read access when `url` is a local path. HTTP/HTTPS sources and device-side writes are outside this local-file scope. All three honor `permissions.file_guard` allow/ask/deny policies without requiring source write permission.

Sources are read through the owning agent's `SysOperation.fs().read_file(mode="bytes")`. With jiuwenbox enabled this uses the sandbox filesystem API; LOCAL mode uses the local backend. Missing bindings and read failures stop delivery without a host-read fallback.

The save tools upload the approved bytes. `send_file_to_user` preserves missing-file skipping, session deduplication by source path and content, and downloads. After a successful SysOperation read, channel delivery and download tokens use the live source path (the materialized workspace path in team mode); no outbound snapshot or version cache is created. Permission denials and backend read failures stop delivery. Relative source paths resolve against the agent's current working directory, without home or environment expansion. Delivery consumers must be able to access the same source file at that path. Later reads observe the current file, and moving or deleting it can invalidate delivery. This is a send-time access check, not a fixed-content guarantee or a new permission check on every download. Unchanged desktop path-based moderation/reporting deduplication does not guarantee another review for each edit at the same path. FileGuard approval does not modify jiuwenbox policy: both layers must permit the read.

Only `send_file_to_user` opts into the signed `approved-outbound-file` download purpose after the read succeeds. This permits that exact live path outside the web server's fixed roots; ordinary tokens (including existing links and harness exports) retain the root restriction. The download uses a checked file handle to reject symlink/junction redirection and preserves size, Range, and timeout limits. The URL format and ordinary-token behavior are unchanged; this exception does not rerun FileGuard at download time.

Outbound tokens also bind to the digest of a random 32-byte `.outbound_download_key` in the existing session directory. Each request reads this generation from disk, without caching or recreating a missing session. Deleting the session invalidates its outbound tokens; recreating the same session ID cannot reactivate them. Concurrent sends share one key, and no source-file copies or per-send records are stored. Tokens without a generation or with a missing/corrupt key fail closed and require a fresh send. Revocation prevents subsequent requests (including Range retries); it does not interrupt a transfer that already passed validation or remove a file already downloaded by the recipient.
