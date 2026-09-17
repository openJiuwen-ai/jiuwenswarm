---
id: CHG-20260803-002
title: Route TUI startup session creation through AgentServer
type: fix
date: 2026-08-03
modules:
  - agentserver-runtime
  - gateway-and-channels
directories:
  - jiuwenswarm/server
  - jiuwenswarm/gateway/channel_manager/tui
  - jiuwenswarm/channels/tui/frontend
flows:
  - session-prewarm-allocation
  - agentserver-session-lifecycle
code_symbols:
  - AgentWebSocketServer._handle_session_create
decisions:
  - ADR-0001
confidence: confirmed
---

# Route TUI startup session creation through AgentServer

## What Changed

TUI `--session <id>` now passes the supplied ID through the existing `session.create` RPC. AgentServer recognizes this TUI-only compatibility form, logs that it bypasses prewarming, validates the portable ID, uses a per-ID lock, preserves existing project/mode metadata, and always reports `prewarm_status="bypassed"`. No separate registration RPC was added.

Gateway forwards creation without owning metadata or warm-pool state. The frontend installs a constructor-time barrier before WebSocket callbacks can issue startup RPCs, releases it on `connection.ack`, and consumes the returned ID and resolved mode before queued command/chat frames are constructed. Normal startup uses a stable `create_token` and omits `session_id`, so AgentServer allocation and prewarm claiming apply; optimistic input is rebound from the temporary `new` identity. Explicit `--session` uses the same barrier and restores history after registration. Normal `/new` and `/clear` also omit `session_id`; other channels still reject caller-selected IDs.

## Why

The reported TUI E2E suite starts deterministic sessions supplied by its launcher. Rejecting those IDs at `session.create` prevented the session from becoming durable. A separate normal-startup race also allowed `chat.send(session_id=new)` to overtake `session.create`, making the first conversation bypass the allocated/prewarmed identity. The unified barrier preserves deterministic TUI behavior and ensures normal sessions consume AgentServer identity without introducing another public lifecycle method.

## Impact

- User-visible: deterministic `--session` startup works for new and existing sessions, and normal immediate input uses the AgentServer-returned session rather than the `new` placeholder.
- Internal: AgentServer remains the durable metadata owner; concurrent explicit-ID creation is idempotent and cannot consume a warm slot.
- Tests: 114 focused Python tests passed in both prewarm states, plus TUI typecheck, build, and full frontend tests. The external `clear.spec.ts` reproduction remains pending replay after the fix.
