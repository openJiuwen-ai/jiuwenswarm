---
id: agentserver-sandbox-runtime
name: AgentServer Sandbox Runtime
status: partial
confidence: confirmed
last_updated: 2026-07-14
user_visible_surface: "TUI /sandbox runtime controls and status."
source_of_truth:
  - "config.yaml:sandbox"
  - "resolved JiuwenBox policy YAML"
modules:
  - gateway-and-channels
  - agentserver-runtime
  - agent-harness
directories:
  - jiuwenswarm/channels/tui/frontend/src
  - jiuwenswarm/gateway
  - jiuwenswarm/server
  - jiuwenswarm/common
  - jiuwenbox
code_symbols:
  - createSandboxCommand
  - AgentWebSocketServer._handle_command_sandbox
  - AgentWebSocketServer._apply_sandbox_runtime_patch
  - AgentWebSocketServer._attach_landlock_status
  - AgentWebSocketServer._bootstrap_internal_jiuwenbox
  - AgentManager.recreate_agent
  - JiuWenSwarmDeepAdapter.apply_sandbox_runtime_patch
  - JiuwenBoxRunner.ensure_running
entrypoints:
  - jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/sandbox.ts
  - jiuwenswarm/gateway/channel_manager/tui/tui_connect.py
  - jiuwenswarm/server/agent_ws_server.py
---

# AgentServer Sandbox Runtime

## Outcome

Linux TUI users mutate global sandbox settings, manage an owned JiuwenBox, and reconcile active agents. Responses combine durable settings with filesystem, process, and Landlock observations that can diverge.

## Causal Path

1. `createSandboxCommand` maps `/sandbox` to unary `command.sandbox` actions: `status`, `enable`, `disable`, `exclude.list/add/remove`, and `files.list/allow/deny/remove`. TUI adds session, trusted-dir, project-dir, and cwd context.
2. TUI Gateway directly forwards the method to AgentServer. E2A transport preserves channel/request identity and correlates the reply by `request_id`.
3. `_handle_command_sandbox` requires Linux and validates configured files before every action, including reads. Validation/platform failures become `SANDBOX_BAD_REQUEST`; unexpected failures become `SANDBOX_INTERNAL`.
4. `enable` resolves endpoint/policy; `JiuwenBoxRunner` reuses/starts an internal uvicorn child or health-checks an external service; the handler best-effort persists the effective endpoint, writes `enabled: true`, then recreates the request channel's agents.
5. `disable` writes `enabled: false`, recreates that channel, then stops only an owned child. External processes remain running.
6. Exclusions write config then hot-patch an adapter. File changes validate paths, dry-run policy construction, write config, then patch and recreate the remote sandbox.
7. Successful actions attach effective files from the selected adapter's operation-card policy or a derived policy. Landlock output combines JiuwenBox `/health.landlock_supported` with policy-YAML compatibility.
8. TUI renders runtime, effective/configured files, and endpoint ownership. Its current response type/renderer ignores the attached `landlock` object.

## State Classification

- **Durable requested state:** `config.yaml:sandbox` holds endpoint/startup, exclusions, file rules, timeout, fallback, and enabled state.
- **Durable policy input:** the separate resolved policy YAML supplies JiuwenBox policy and Landlock compatibility.
- **Process state:** singleton `JiuwenBoxRunner` owns at most one child and its endpoint/policy metadata; external services are observed, never owned.
- **Applied agent state:** channel/mode/project caches, adapter `SysOperationCard`, launcher parameters, and remote `sandbox_id`.
- **Derived output:** effective files, health, Landlock, and process flags. Logs do not prove convergence.

## Replay, Restore, Or Reconstruction

New deep agents build a sandbox card from current config only when enabled, URL, and type exist. Enable/disable recreate only the request channel, so other channels may retain cards built from older global state.

On Linux startup, `_bootstrap_internal_jiuwenbox` runs when `startup_mode` is explicitly `internal`. It ignores persisted `enabled: false`: it starts JiuwenBox and best-effort rewrites endpoint and `enabled: true`. Thus `/sandbox disable` is not restart-stable while internal startup remains explicit. External mode is neither restored nor killed by AgentServer.

Hot patches optimize reconstruction. Exclusion updates affect subsequent launches; file changes attempt to recreate the remote sandbox and replace `sandbox_id`. Later full agent creation is the convergence path after missed/failed patches.

## Contract

- **Method/identity:** unary `command.sandbox`; `request_id` correlates transport and `channel_id` scopes reconciliation, while config is global. Project identity uses request context with adapter/cwd fallbacks.
- **Platform:** every action rejects non-Linux because the runtime depends on Linux namespaces, bubblewrap, and Landlock.
- **Enable:** requires an existing policy file. Internal mode may allocate another local port and launches uvicorn with `JIUWENBOX_POLICY_PATH`; external mode requires a healthy configured endpoint.
- **Results:** include `runtime`; enable adds endpoint/readiness and `agent_recreated`; disable adds `jiuwenbox_stopped`; file actions may add `effective_files`; successful actions may add `landlock`.
- **Errors:** validation is `SANDBOX_BAD_REQUEST`; other failure is `SANDBOX_INTERNAL`. Effective-file/Landlock enrichment is best-effort and may be absent on an otherwise successful command.

## Consumer State And Output

TUI shows enabled state, exclusions, effective-or-configured paths, and internal/external process handling. Agents consume cached cards and remote sandboxes, not response payloads; a successful transcript does not prove all variants enforce the displayed policy. Landlock can be wire-visible without TUI presentation.

## Failure, Ordering, And Identity

- Enable starts/verifies JiuwenBox before durable commit. Endpoint-write failure is ignored; runtime-write failure can leave a new child. Config is enabled before recreation, with no rollback if recreation raises.
- Disable persists false before recreation/shutdown. Recreation failure skips shutdown; stop failure softens to `jiuwenbox_stopped: false`, leaving disabled config with a live owned process.
- `AgentManager.recreate_agent` swallows per-agent rebuild failures although handlers report `agent_recreated: true`. Its missing-channel branch logs absence then dereferences the missing map, so first-use commands can fail after config changed.
- Exclusion/files write YAML before hot patch. Later validation has no rollback; ordinary adapter failures may be swallowed. Only one unqualified current agent is selected, not every mode/project variant or channel. File policy can change before swallowed remote recreation failure.
- Effective files may resolve project identity across request project, an unqualified adapter, trusted dirs, or cwd, so output need not match every affected agent.
- Landlock `supported: false` also represents unreachable/malformed health, not just kernel non-support. Attachment failures are softened.
- Requests can interleave without a transaction across YAML, process, and agents; runner locking covers process ownership only. TUI's 30-second timeout can expire during enable health/recreation or disable's up-to-60-second shutdown, while backend mutation continues.

## Verification

Static inspection confirmed TUI/Gateway routing, handler branches, config, runner, adapter patching, agent recreation, bootstrap, and enrichment. JiuwenBox tests cover `/health.landlock_supported`, policy round-trips, filesystem enforcement, and CLI errors. No direct test was found for `command.sandbox`, its cross-layer path, ordering, restart re-enable, or Landlock presentation. Tests were not run.

## Known Gaps

- No transaction, rollback, revision, or convergence report spans config, JiuwenBox, remote sandbox, and all agents.
- Global desired state versus channel-scoped reconciliation needs a fan-out/identity contract and multi-variant tests.
- Restart semantics must define whether explicit internal startup or `enabled` wins.
- Status should distinguish unreachable from Landlock unsupported and display compatibility in TUI.
- End-to-end tests should cover ordering failures, timeouts, fresh channels, concurrency, and project selection.
