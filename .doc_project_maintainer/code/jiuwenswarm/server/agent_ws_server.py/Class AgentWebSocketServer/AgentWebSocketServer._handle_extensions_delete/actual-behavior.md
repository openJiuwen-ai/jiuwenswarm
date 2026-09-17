---
symbol: AgentWebSocketServer._handle_extensions_delete
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_extensions_delete`

## Actual Role

Accepts a truthy registry key, invokes a synchronous process-global RailManager deletion, and reports a fixed deleted result or the raised error. The manager discards registration/cache bookkeeping, recursively deletes the derived filesystem path, removes the in-memory entry, and rewrites extensions_config.json, but does not unregister already attached Rail instances.

## Key Signals

- Caller: `_handle_message` dispatches `ReqMethod.EXTENSIONS_DELETE`; Web and TUI forward it.
- Input: Only truthiness of `name` is checked; type, imported-name syntax, canonical path containment, principal capability, ownership, and revision are not validated.
- Side effects: Process-global runtime bookkeeping changes, synchronous recursive filesystem deletion, registry mutation, and non-atomic JSON rewrite.
- Failure model: Any raised exception becomes `ok=false` with its raw string; partial mutations are not rolled back, while success always returns `{deleted: true, name}` and ignores the manager's boolean result.
- Main defect: Active Rail teardown is skipped even though registration bookkeeping and the cached instance are discarded.
- Tests/flow: The browser UI asks for confirmation, but the RPC is also directly forwarded by Web/TUI and has no direct manager/handler lifecycle tests. No tests were run during this re-audit.

## Detail Index

- Detail docs pending.
