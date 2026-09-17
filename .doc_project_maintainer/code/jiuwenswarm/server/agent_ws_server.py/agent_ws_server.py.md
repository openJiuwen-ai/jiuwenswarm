---
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
language: python
confidence: confirmed
last_updated: 2026-07-07
---

# `agent_ws_server.py`

## Actual Role

Defines the AgentServer WebSocket runtime and many helper functions for E2A/legacy request handling, history and workflow wire limits, mode resolution, plan-mode reminders, sandbox path validation, session operations, command handlers, server push, ACP, scheduler, and harness package operations.

## Symbol Inventory

- Top-level helpers: 28 functions for wire sizing, history/workflow payload bounding, request text/mode/project normalization, sandbox validation, and plan-mode prompt injection.
- `AgentWebSocketServer`: singleton WebSocket server class with 124 inventoried methods.
- Selected high-value method cards exist for request dispatch, unary/stream chat, cancel, push, and lifecycle.
- Full file has 152 discovered symbols; remaining symbol entry docs are pending.

## Key Signals

- Input: WebSocket frames, E2A envelopes, legacy request dicts, config and runtime state.
- Output: E2A response wire frames, stream chunks, server-push frames, session/command payloads.
- Main side effects: active agent work, session files, history, checkpointer, config mutation, env mutation, scheduler, jiuwenbox subprocess, extension and agent reload.
- Main risk: very broad central surface with many cross-boundary handlers and mutable runtime state.
- Related tests: `tests/unit_tests/agentserver/*`, `tests/unit/agentserver/*`, and Gateway routing tests.
