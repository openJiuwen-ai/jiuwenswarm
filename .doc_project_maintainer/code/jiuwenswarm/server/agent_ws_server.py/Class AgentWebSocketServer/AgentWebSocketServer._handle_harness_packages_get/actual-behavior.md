---
symbol: AgentWebSocketServer._handle_harness_packages_get
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_harness_packages_get`

## Actual Role

Implements a nominally read-only package catalog RPC by constructing a fresh AutoHarnessService without rail or agent, then running `get_packages_info` in a worker thread. Service construction still occurs synchronously and creates directories, loads/bootstraps base config, and initializes scheduler components. The worker loads harness-packages.json, but broad load failure can scan runtime extensions and rewrite replacement metadata before the handler forwards the unvalidated value as success. It then encodes and sends one response under `send_lock`.

## Key Signals

- Input: Request/correlation identity only; params, session, channel scope, and project scope do not affect which global package store is read.
- Output: The service value is forwarded as one success payload without schema validation, or a raw exception string on failure; request metadata is not copied.
- Main side effects: Fresh service construction creates/bootstraps global data and scheduler state, and load fallback may scan extensions and rewrite package metadata despite the GET name.
- Main risk: Corrupt or malformed persistence can be silently replaced or reported as successful empty/malformed state, while some supposedly offloaded initialization still blocks and mutates on the event loop.
- Related evidence: Architecture mentions package dispatch, but no dedicated flow or direct package-info/handler tests were found. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
