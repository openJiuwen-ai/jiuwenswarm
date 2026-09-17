---
id: gateway-and-channels
name: Gateway And Channels
confidence: inferred
last_updated: 2026-08-03
read_when: "Working on Gateway routing, channel adapters, frontend/TUI command forwarding, or AgentServer client behavior."
---

# Gateway And Channels

## Responsibility

Accepts user/channel/front-end input, normalizes it into E2A or legacy-compatible request data, forwards it to AgentServer, receives unary or streamed responses, and dispatches visible output back to the appropriate channel.

## Boundaries

- Owns: channel ingress, Gateway message queues, WebSocket AgentServer client, routing/session map helpers, frontend/TUI transport surfaces.
- Does not own: AgentServer adapter execution, agent tool semantics, or final durable session ownership once delegated to AgentServer.

## Current Evidence

- `jiuwenswarm/gateway/routing/agent_client.py` receives AgentServer frames, routes normal responses by request ID, and treats server-push frames as out-of-band events.
- `docs/en/E2A-protocol.md` describes Gateway -> AgentServer E2A field contracts.
- Tests outside the AgentServer directory cover AgentServer client queueing, reconnect/close behavior, stream tail grace, and timeout policy.
- Gateway reports only prewarm-eligible live channel IDs through `agent.prewarm.sync`; ACP and A2A are excluded at both Gateway and AgentServer boundaries. Web/TUI local session creation proxies AgentServer, IM first contact and `/new_session` allocate before forwarding, and ACP/A2A/SSH retain external IDs only as aliases without creating warm targets.
- Startup, channel-change, and configuration-reload prewarm sync triggers are debounced and coalesced; pending delayed sync is cancelled during Gateway shutdown.
- Web session creation derives `user_id` from the authenticated connection and overwrites any request-body value before forwarding to AgentServer.
- Every TUI startup now waits for `connection.ack` and then calls `session.create` before any command or chat frame can leave the frontend. Normal startup omits `session_id`, supplies a stable `create_token`, and adopts the AgentServer-allocated/prewarmed ID; optimistic input entered before the acknowledgement is rebound from the `new` placeholder to that ID. `--session` forwards the caller-supplied ID through the same barrier, while `/new` and `/clear` also omit `session_id` and continue to use AgentServer allocation.

## Related Flows

- `gateway-agentserver-e2a-chat` and `agentserver-server-push`
- `agentserver-command-mcp` and `agentserver-sandbox-runtime`
- `agentserver-plan-mode-exit`
- `agentserver-schedule-auto-harness`
- `agentserver-history-stream`
- `session-prewarm-allocation`

## Pending

Full channel integration remains pending, but new-session identity ownership for Web, TUI, controlled IM, ACP, A2A, SSH, and Cron is traced in `session-prewarm-allocation`. On 2026-08-03 the focused AgentServer/Gateway/channel lifecycle suite passed 114 tests with prewarming disabled and another 114 with it enabled; the TUI frontend build, typecheck, and full frontend test suite also passed.
