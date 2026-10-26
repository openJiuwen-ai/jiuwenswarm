---
symbol: AgentWebSocketServer._handle_session_fork
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_session_fork`

## Actual Role

Accepts caller-selected source/target IDs and a title, synchronously creates a target directory with fork-annotated history and queued metadata, selects a live agent by channel alone, best-effort copies its context and checkpointer state/plan, and returns only the filesystem result. Gateway and TUI treat `ok=true` as a complete fork and switch to the target even when later stages returned False or metadata is not yet durable.

## Key Signals

- Inputs: Source/target IDs and optional title are trimmed but not schema-, containment-, ownership-, or revision-validated; channel_id drives live-agent selection.
- Output: One E2A response containing only the fork_session result on success; ValueError codes are inferred from English message substrings and no per-store outcome is returned.
- Side effects: Creates/re-writes history, queues metadata, seeds a live context engine, writes transformed checkpointer state, and may copy a plan file.
- Main risks: Filesystem escape and cross-user disclosure, inconsistent live snapshots, partial-success switching, wrong agent/card identity, and event-loop blocking.
- Related flow/tests: The partial `agentserver-session-lifecycle` flow records non-atomic reconstruction and hostile-ID gaps; helper tests exist, but no handler/Gateway fork contract test was found. No tests were run during this re-audit.

## Detail Index

- Detail docs pending.
