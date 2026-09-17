---
symbol: AgentWebSocketServer._handle_extensions_import
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_extensions_import`

## Actual Role

Acts as a persistent host-code ingestion RPC. It accepts a caller-selected `folder_path`, performs only an existence/directory precheck at the WebSocket boundary, then synchronously delegates to the singleton RailManager. The manager derives/validates extension metadata and rail.py superficially, recursively installs or replaces the folder under the agent workspace, registers the extension disabled in memory, persists its JSON metadata, and returns that record. This method does not activate/hot-load the rail, but it establishes code that a later toggle can execute.

## Key Signals

- Input: Caller-controlled host directory in `params.folder_path`; params/path type, authorized root, provenance, quotas, and symlink/special-file policy are not established locally.
- Output: One locked response containing the imported disabled extension record, or a raw exception string with top-level failure; no destination digest, copied-file inventory, or partial-state result is exposed.
- Main side effects: Synchronously reads and recursively copies host files, may replace an existing installed tree, mutates the process singleton registry, rewrites persistent JSON metadata, logs failures, and sends a WebSocket frame.
- Main risk: An unbounded forwarded request can install arbitrary Python code through a non-transactional, race-prone filesystem/config sequence; later enablement turns that persisted code into privileged execution.
- Related evidence: No direct import handler/manager lifecycle tests or extension flow were found. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
