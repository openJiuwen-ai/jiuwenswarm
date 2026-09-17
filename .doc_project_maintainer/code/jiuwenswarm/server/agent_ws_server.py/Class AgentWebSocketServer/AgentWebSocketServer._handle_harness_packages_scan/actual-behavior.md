---
symbol: AgentWebSocketServer._handle_harness_packages_scan
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_harness_packages_scan`

## Actual Role

Implements a global package-catalog rescan with an implicit persistence commit. It constructs a fresh AutoHarnessService on the event loop, then offloads runtime-extension discovery and metadata saving as two separate sequential worker calls. Scanner output—including partial discovery and filtered active IDs—is handed directly to a save routine whose write failures are softened internally. The same in-memory payload is then returned as success regardless of verified durability.

## Key Signals

- Input/routing: Request identity only; params/session/project are ignored. Direct AgentServer dispatch coexists with a Web local fallback when AgentServer is unavailable.
- Output: One scanned package snapshot or raw exception string. Success does not prove a complete scan or persisted harness-packages.json revision; request metadata is not copied.
- Main side effects: May bootstrap AutoHarness directories/config/scheduler state, scans global runtime extension trees, rewrites global package metadata, and sends one WebSocket frame.
- Main risk: A partial scan or concurrent activation update can be persisted as authoritative, while swallowed save failure makes the wire response disagree with disk.
- Related evidence: No direct scan/persistence tests or dedicated package lifecycle flow were found. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
