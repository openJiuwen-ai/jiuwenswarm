# Smart Approval

Smart Approval reduces repetitive permission prompts for tool use in single-agent sessions. It first applies your existing permission rules, then reviews eligible tool requests in the context of the current task. For each request, it can:

- allow it once;
- deny it; or
- ask you to decide.

Smart Approval never grants lasting access to a tool and cannot override an explicit `deny` rule.

## Where it applies

| Scenario | Smart Approval | Notes |
| --- | --- | --- |
| Web, single-agent Work mode | Supported | This is the primary supported experience. |
| Code Agent and Code plan | Not supported | Support for Code modes is planned. |
| Teams, clusters, and distributed teams | Not supported | Tool approval is not yet available in these modes. Smart Approval support will follow. |

“Not supported” refers only to Smart Approval; the underlying mode remains available.

Each Smart Approval session is bound to one primary project or workspace. Start a new session before switching to a different directory.

## How it differs from other permission profiles

The permission menu next to the Web message box offers these profiles:

| Profile | Behavior |
| --- | --- |
| **Default** | Applies your existing tool and path rules to allow, ask, or deny. A Permission card appears when confirmation is required. |
| **Smart Approval** | Applies your existing rules and reviews eligible requests in the context of your task. It asks you whenever your judgment is needed. |
| **Full Access** | Disables the tool-permission boundary, allowing tools to run without approval. This carries significantly more risk and is not Smart Approval. |

Profile changes take effect on the **next conversation turn**.

## How to use it

### Enable it in the WebUI

1. Open a single-agent session in Work mode.
2. Open the permission menu next to the message box.
3. Choose **Smart Approval**.
4. Send your next message to continue.

When Smart Approval needs your judgment, JiuwenSwarm displays the standard Permission card. You can allow the request once, deny it, or use an existing option such as remembering your decision for the session.

### Enable it in configuration

By default, the configuration file is `~/.jiuwenswarm/config/config.yaml`. If `JIUWENSWARM_CONFIG_DIR` is set, use the `config.yaml` file in that directory instead.

Minimal configuration:

```yaml
permissions:
  enabled: true
  mode: auto
```

To switch back to the Default profile:

```yaml
permissions:
  enabled: true
  mode: manual
```

`permissions.mode` sets the runtime profile to `manual` or `auto`. Do not confuse it with `permissions.permission_mode`, described below.

## Settings that affect Smart Approval

Smart Approval works within your existing tool-permission boundary. The following settings directly affect its decisions.

### Tool and argument rules

```yaml
permissions:
  schema: tiered_policy
  permission_mode: normal
  defaults:
    "*": ask
  tools:
    bash: ask
    write_file: allow
  rules:
    - id: deny-dangerous-command
      tools: [bash]
      pattern: "rm *"
      action: deny
```

- `permissions.tools` sets the `allow`, `ask`, or `deny` action for each tool.
- `permissions.rules` defines more specific rules based on commands, paths, or arguments.
- `permissions.defaults` provides the fallback action when no other rule matches.
- `permissions.permission_mode` accepts `normal` or `strict` and controls how severity maps to actions for rules without an explicit `action`.
- `permissions.package_builtin_rules` controls whether to load the security rules bundled with agent-core. It defaults to enabled and can only be set in the Global/host configuration; User and Session settings cannot override it.

An explicit `deny` always takes precedence. Smart Approval does not treat a broad default `allow` as unrestricted access; high-risk or highly flexible tools may still require additional review or your confirmation.

### Path and workspace rules

```yaml
permissions:
  file_guard:
    enabled: true
    defaults:
      read: ask
      write: ask
      exec: ask
    workspace:
      read: allow
      write: allow
      exec: ask
    paths:
      - path: "/data/shared"
        read: allow
        write: ask
        exec: deny
```

- `file_guard` controls read, write, and execute access to the workspace, trusted directories, and external paths.
- More specific path-level `ask` and `deny` rules still restrict Smart Approval.
- `external_directory` is kept for backward compatibility. Use `file_guard` for new configurations.
- Directories added through `/add-dir` remain subject to the existing path rules.

### Remembered approvals and channel rules

- `permissions.approval_overrides` and decisions remembered for the session may reduce later prompts within their existing scope, but they cannot override an explicit `deny`.
- `permissions.owner_scopes` continues to scope tool access by channel and user. It does not enable Smart Approval for Teams or other unsupported modes.

### AutoReviewer options

Most users can leave these options unchanged:

```yaml
permissions:
  auto:
    reviewer_timeout_ms: 60000
    reviewer_min_confidence: 0.7
    persistent_audit_enabled: false
    bounded_write_max_files: 3
    bounded_write_excluded_paths: []
```

| Setting | Effect |
| --- | --- |
| `reviewer_timeout_ms` | Maximum time allowed for a review. A timeout sends the request to manual review; it never causes the request to be allowed automatically. |
| `reviewer_min_confidence` | Minimum reviewer confidence. Results below this threshold are sent for manual review. |
| `persistent_audit_enabled` | Writes approval decisions to a persistent audit log. |
| `bounded_write_max_files` | Maximum number of files allowed on the bounded-write fast path. |
| `bounded_write_excluded_paths` | Additional sensitive paths to exclude from the fast path. These are combined with the built-in protected paths. |

AutoReviewer uses the configured model service, so an eligible request may incur one additional model call and a small amount of latency. If the service is unavailable or times out, or if it returns an invalid response, the request is escalated or denied—never silently allowed.

## What you will see

- Approved automatically: the tool runs, and the interface displays a Smart Approval badge or reason.
- Needs your input: the standard Permission card asks you to decide.
- Denied: the tool does not run, and the interface displays the reason.
- Multiple pending requests: Permission cards appear one at a time, in order. There is no **Allow all** action.

## Known display limitation

For tasks with multiple subagents, the Web todo panel may show only one item as running while the rest remain pending, even when several subagents are running in parallel. This is a pre-existing status-display issue. Smart Approval does not serialize subagents or extend its authority to them.

## Safety recommendations

- Use Smart Approval instead of leaving Full Access enabled just to avoid prompts.
- Keep `ask` or `deny` rules for shell commands, external paths, and sensitive files.
- Use `file_guard` to define workspace and external-directory boundaries.
- After changing a rule, verify it in a new conversation turn. Start a new session when switching workspaces.
- Periodically review remembered decisions and `approval_overrides`.

For related settings, see [Tool Permissions & Security](ToolPermissionsSecurity.md) and [Configuration](Configuration.md).
