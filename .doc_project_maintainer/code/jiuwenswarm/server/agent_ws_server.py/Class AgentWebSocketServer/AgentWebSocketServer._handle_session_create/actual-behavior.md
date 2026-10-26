---
symbol: AgentWebSocketServer._handle_session_create
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_session_create`

## Actual Role

Handles unary `session.create` by defaulting the channel, treating non-dict params as empty, resolving the requested mode, and asking AgentManager for an ID. The manager either echoes a stripped explicit ID, generates an ACP ID, or returns `default`; no session is reserved or persisted. For resolved `team` mode it first prepares the channel's team runtime switch, then sends a camel-case `sessionId`; any exception becomes raw error text and may occur after team state changed.

## Key Signals

- Input: Optional channel, mode, and session ID; explicit strings are only stripped, while non-dict params and non-string IDs are treated as absent.
- Output: Camel-case `sessionId` on success or uncoded exception text on failure; success does not mean a durable or unique session exists.
- Main side effects: May stop stale distributed team sessions before the response; logs and sends one WebSocket frame, with an error-send retry if the success path raises.
- Main risk: Caller-controlled IDs become later filesystem identities, and reported creation can diverge from durable session and team-runtime state.
- Related flow/tests: `agentserver-session-lifecycle` confirms ID-only creation and downstream multi-store risks. Three fake-based success tests cover ID forwarding/generation and one team switch; real-manager, adversarial, persistence, rollback, and transport failures are untested. Tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
