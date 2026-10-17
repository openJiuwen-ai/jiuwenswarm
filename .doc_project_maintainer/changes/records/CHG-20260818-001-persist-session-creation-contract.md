---
id: CHG-20260818-001
title: Lock Persist Session during Session creation
type: feature
date: 2026-08-18
modules:
  - agentserver-runtime
  - gateway-and-channels
  - agent-harness
flows:
  - agentserver-session-lifecycle
  - session-prewarm-allocation
  - eternal-conversation-memory
code_symbols:
  - AgentWebSocketServer._handle_session_create
confidence: confirmed
---

# Lock Persist Session during Session creation

## What Changed

The Web new-conversation menu now exposes **Persist Session**. The draft boolean is sent once in `session.create`, stored as `persist_session` in Session metadata, returned by create/list/restore, and displayed as a locked tag after creation. Chat requests cannot change it; AgentServer derives the existing internal Eternal Conversation adapter flag from metadata on every turn.

`persist_session` is part of create-token idempotency but deliberately not part of the warm-pool key. Legacy metadata can initialize the field once from the old runtime flag so existing controlled acceptance Sessions remain resumable.

## Why

The feature is a property of a durable Session, like its initial identity and project binding, rather than a mutable process config or per-message preference. Keeping it out of the prewarm key avoids duplicate warm slots while retaining deterministic create retries.

## Impact

- User-visible: a new Session can opt into Persist Session before its first message; restored Sessions show the authoritative locked state.
- Internal: config/per-chat values no longer override an initialized Session.
- Verification: focused metadata, session-create, authoritative chat injection, frontend lifecycle, and production-build checks cover the contract. The higher-level four-quadrant real-model acceptance remains separately tracked.
