---
symbol: AgentWebSocketServer._handle_harness_packages_activate
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_harness_packages_activate`

## Actual Role

Coordinates activation across one selected runtime, global package metadata, and other single-agent variants. After only a truthy package-id check, it resolves mode/project identity and may auto-create an agent before package existence is known. A fresh AutoHarnessService then loads package metadata/config, either marks active with no runtime, or loads the selected agent, persists active status, and best-effort broadcasts to other agent.fast/agent.plan instances in the request channel. The handler returns the service payload as success even though durability and fanout convergence are not reported.

## Key Signals

- Input/routing: Truthy `params.package_id`, resolved mode/submode/project, and channel (default `web`); ID type/existence is not validated before potential agent creation.
- Output: One activation payload or coded raw-error response. Success can include absolute runtime/config paths but no per-target apply/durability status; request metadata is not copied.
- Main side effects: May create an agent, synchronously load/rewrite global package metadata, load harness config into one runtime, best-effort fan out to other single-agent/session adapters, and send a WebSocket frame.
- Main risk: Non-transactional ordering and swallowed fanout/write failures let runtime variants and active metadata disagree while clients receive success.
- Related evidence: No direct activation lifecycle tests or dedicated consistency flow were found. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
