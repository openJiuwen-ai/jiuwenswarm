---
symbol: AgentWebSocketServer._handle_command_sandbox
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_command_sandbox`

## Actual Role

Acts as the orchestration boundary for the global `/sandbox` control plane. It parses `params.sub` before entering its protected block, requires Linux and validates configured file rules for every read or mutation, then routes status, enable/disable, exclusion, and file-policy actions to helpers. Those helpers can mutate config.yaml, own or observe JiuwenBox process state, recreate request-channel agents, or hot-patch one selected adapter/remote sandbox. On success the method best-effort enriches the action payload with effective files and Landlock observations, then sends one locked response; it coordinates requested, process, applied, and derived state without making them transactional.

## Key Signals

- Input: `AgentRequest.params.sub` plus subcommand-specific file, pattern, project, cwd, and trusted-directory context; a truthy non-mapping params value fails before local error normalization.
- Output: One action/status payload enriched where possible with effective files and Landlock, or `SANDBOX_BAD_REQUEST`/`SANDBOX_INTERNAL`. Success describes requested/observed state, not convergence across every agent or remote sandbox.
- Main side effects: Persists global endpoint/runtime/file/exclusion config, starts/health-checks/stops an owned JiuwenBox, recreates request-channel agents, hot-patches a selected adapter and remote sandbox, performs health/policy reads, and sends a WebSocket response.
- Main risk: Global desired state, owned process state, per-agent cached cards, remote sandbox IDs, and derived response fields can diverge while the command reports success; disable is also reversed on restart under explicit internal startup.
- Related evidence: `agentserver-sandbox-runtime` confirms the cross-layer ordering, identity, restart, timeout, and convergence gaps. No direct handler tests were found, and tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
