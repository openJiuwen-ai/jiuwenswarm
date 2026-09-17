---
symbol: AgentWebSocketServer._handle_harness_packages_deactivate
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_harness_packages_deactivate`

## Actual Role

Handles unary `harness.packages.deactivate` by requiring only a truthy package ID, canonicalizing `request.params.mode`, and getting or creating one channel/project/mode-specific Agent (`auto_harness` maps to `agent`). It passes that instance to AutoHarnessService, which treats an ID absent from the active list as successful, otherwise best-effort unloads the selected instance and eligible same-channel single-agent/session adapters before removing the ID from global metadata. Validation/internal exceptions become coded responses, then one frame is sent under `send_lock`.

## Key Signals

- Input: Any truthy `package_id`; non-dict params become empty, and mode/project/channel select or create the primary Agent.
- Output: Deactivation payload, including successful unknown/inactive no-ops, or coded validation/internal error; no per-runtime applied results are returned.
- Main side effects: Mutates the request's canonical mode, may create an Agent, unloads selected and same-channel eligible runtimes, rewrites global harness package metadata, logs, and sends a WebSocket frame.
- Main risks: Runtime and durable state can diverge silently; global inactive state is not reconciled across all channels/modes.
- Related tests: No handler, service-deactivation, routing, fanout-failure, persistence-failure, or wire-contract test was found; tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
