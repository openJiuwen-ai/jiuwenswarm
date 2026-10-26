---
symbol: AgentWebSocketServer._handle_browser_start
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_browser_start`

## Actual Role

Acts as a thin asynchronous RPC wrapper around a synchronous, global browser launcher. It ignores request params, resolves the active config file, calls `start_browser(dry_run=False)` on the event-loop thread, and treats any normal launcher return as transport success containing only an integer returncode. Downstream launch logic resolves Chrome/CDP/profile settings, detaches the process, and persists shared profile state; exceptions become raw-error responses, but the handler has no process identity or rollback capability.

## Key Signals

- Input: Request/correlation identity only; all launch settings come from global config and environment, and no request-scoped override or ownership token is accepted.
- Output: One locked response containing only `returncode`, or a raw exception string. It does not return PID, CDP endpoint, ownership, readiness, or partial-spawn state; request metadata is not copied.
- Main side effects: Synchronously reads launch configuration, starts a detached OS process, rewrites the shared browser profile store, logs failures, and sends a WebSocket frame.
- Main risk: Process creation is conflated with readiness and is unmanaged across duplicate calls and post-spawn failures; the synchronous launcher also stalls the AgentServer loop.
- Related evidence: No relevant tests or browser-start flow doc were found in the existing audit evidence, so lifecycle behavior is documented from the handler/launcher call chain only. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
