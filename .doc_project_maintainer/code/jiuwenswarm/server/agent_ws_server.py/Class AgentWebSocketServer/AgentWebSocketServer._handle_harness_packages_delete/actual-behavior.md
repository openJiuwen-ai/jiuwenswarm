---
symbol: AgentWebSocketServer._handle_harness_packages_delete
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_harness_packages_delete`

## Actual Role

Validates only presence and the reserved native ID, resolves or creates a mode/project Agent, then asks a fresh AutoHarnessService to delete a globally recorded package. For active packages the service best-effort unloads the selected instance and some cached single-agent consumers, recursively deletes the persisted runtime_path, removes package/active metadata, rewrites the shared JSON file, and returns a fixed success payload even when several stages failed silently.

## Key Signals

- Inputs: Only package_id truthiness and exact equality with `native` are checked; type, principal capability, ownership, revision, and stored-path containment are not validated.
- Side effects: May create an Agent, unload live harness resources, recursively delete a recorded directory, mutate global active-package state, and non-atomically rewrite shared JSON.
- Failure model: Missing/native/not-found errors produce coded failures, but unload/broadcast/rmtree/save failures are swallowed below the handler and appear as `ok=true`.
- Main risks: Arbitrary-path destructive I/O, unauthorized global deletion, stale resources in excluded consumers, lost concurrent updates, and event-loop blocking.
- Flow/tests: Web normally forwards to this handler but performs a no-agent local delete fallback when AgentServer is unavailable; neither path has matching deletion tests. No tests were run during this re-audit.

## Detail Index

- Detail docs pending.
