---
symbol: AgentWebSocketServer.send_push
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer.send_push`

## Actual Role

Best-effort encodes an internal chunk-style or structured response-kind message as a server-push-marked E2A frame, then sends it through the process-global current Gateway socket under what it expects to be that connection's shared lock. Oversized originals are replaced with a bounded push-marked error frame; missing connections, validation/encoding errors, and send failures are logged and collapsed to the same None result.

## Key Signals

- Inputs: An unvalidated mapping that implicitly selects chunk style through `payload` or structured style through any truthy `response_kind` plus `body`.
- Output: Always None; one original or oversized-error frame may be sent, while disconnect/encode/send failure is only logged.
- Side effects: Builds E2A wire metadata, acquires a global connection lock reference, and writes the shared Gateway WebSocket.
- Main risks: Connection-generation and pre-ack races can drop or misroute critical pushes; silent failure gives cron/proactive/ACP/session producers false success; a stalled send blocks all outbound frames.
- Tests/flow: Helper size/fallback behavior and transport delegation have adjacent coverage, but direct socket lifecycle/result tests and complete wire-branch inverse tables remain absent in the partial server-push flow. No tests were run during this re-audit.

## Detail Index

- Detail docs pending.
