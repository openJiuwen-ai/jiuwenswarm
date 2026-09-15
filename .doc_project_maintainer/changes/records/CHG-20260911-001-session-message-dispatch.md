---
id: CHG-20260911-001
title: Add native cross-Session message dispatch
type: refactor
date: 2026-09-11
modules:
  - runtime-session
flows:
  - runtime-session-reference-chain
confidence: confirmed
---

# Add Native Cross-Session Message Dispatch

## What Changed

`AgentRuntime.send_session_message` now resolves persisted source and target Sessions, requires an exact owner match and a persisted target channel, restores unloaded Work/Code Normal targets, and submits a new asynchronous `SESSION_MESSAGE` execution. Idle targets start new work; targets waiting for interaction control retain the exact control owner and release queued messages in FIFO order only after control completion or cancellation.

The Coordinator owns message receipts, idempotent request IDs, cancellation, bounded execution status, and the control gate. Stale control input fails instead of becoming a new chat turn, and failed or cancelled control delivery retains the interaction ID for an exact retry. No legacy mode, default-channel fallback, compatibility alias, or second execution path was added.

## Verification

After rebasing onto upstream `c982b1e52`, the repository `.venv` passed all 276 Runtime unit tests and 122 Process CLI, AgentServer, and Gateway boundary tests. Six configured-model system cases passed for Work/Code persisted resume, chat and Goal ask-user resume, and Work/Code dispatch into unloaded persisted targets. Ruff, diff cleanliness, scoped documentation-size checks, and the project-maintainer current-slice check passed.
