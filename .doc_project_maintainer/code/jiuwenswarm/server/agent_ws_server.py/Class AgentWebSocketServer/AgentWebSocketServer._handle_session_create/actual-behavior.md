---
symbol: AgentWebSocketServer._handle_session_create
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_session_create`

## Actual Role

Handles unary `session.create`. Normal creation rejects a caller-selected ID outside TUI, validates project/work-mode identity, requires `create_token`, and asks AgentManager to claim a warm/warming or fresh server-owned ID. The TUI explicit-ID compatibility form accepts a sanitized ID up to 128 characters, logs the bypass, holds a per-ID lock, reuses authoritative metadata when present, or resolves the current TUI code project and creates metadata; it never calls the warm pool. Both forms prepare the session-switch product owner before acknowledging and dispatch optional KVC signals afterward.

## Key Signals

- Input: Channel identity, mode/work/project fields, creation token for normal creation, and an optional TUI-only external ID for compatibility creation.
- Output: `session_id`, normalized project binding, and prewarm status; explicit-ID TUI creation also returns `created` and resolved `mode`.
- Main side effects: Claims or bypasses warm state, writes metadata synchronously for new sessions, may prepare Team runtime ownership, sends one response, and schedules optional KVC signals.
- Main risk: The method mixes validation, allocation, persistence, product switching, response IO, and compatibility policy; Team preparation still precedes observable success.
- Related flow/tests: `agentserver-session-lifecycle` and `session-prewarm-allocation`. Direct tests cover server allocation, TUI explicit-ID concurrency/idempotency, validation/ownership rejection, stable project binding, warm bypass, and team preparation in both prewarm states.

## Detail Index

- [Risks](risks.md)
