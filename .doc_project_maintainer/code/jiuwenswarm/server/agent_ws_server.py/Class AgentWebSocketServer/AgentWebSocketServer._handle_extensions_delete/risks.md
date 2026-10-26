---
symbol: AgentWebSocketServer._handle_extensions_delete
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_extensions_delete audit evidence

## ISSUE-001: Deleting an enabled extension does not unregister its active Rail.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: The handler calls synchronous RailManager.delete_extension directly at lines 5988-5989. That method only discards the name from _registered_rails and removes the cached instance; unlike hot_reload_rail(name, false), it never awaits agent_instance.unregister_rail. Existing main-agent and team-member instances can therefore retain executable Rail behavior after its code and metadata are deleted.
- Suggested action: Make deletion an async lifecycle operation that unregisters the exact live instance from every active consumer before removing cache/files, and surface teardown failures.

## ISSUE-002: Runtime state, folder deletion, registry mutation, and config persistence are not transactional.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: RailManager discards runtime registration/cache state, recursively removes the folder, deletes the in-memory _extensions entry, and finally overwrites extensions_config.json. A folder or config-write failure leaves those layers divergent, and no lifecycle lock serializes delete against toggle/import or an awaiting hot_reload_rail call.
- Suggested action: Serialize extension lifecycle operations and use a recoverable unregister/quarantine/atomic-config transaction with rollback or explicit degraded-state reporting.

## ISSUE-003: A registry key can escape the extensions directory and select another filesystem target.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: RailManager._load_config accepts arbitrary JSON object keys without the identifier validation used by import_extension. delete_extension then builds folder_path as self._extensions_dir / name and recursively deletes it without canonical containment checking; an absolute or parent-traversing configured key can therefore target content outside the extensions root.
- Suggested action: Validate loaded and requested names against the import identifier contract, resolve the candidate path, reject absolute/traversing values, and require canonical containment under _extensions_dir before deletion.

## ISSUE-004: The destructive RPC has no server-side authorization or revision boundary.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: Lines 5982-5989 require only a truthy name. Web and TUI both forward extensions.delete, while the handler does not inspect permission_context, an administrator capability, ownership, confirmation token, expected revision, enabled state, or active consumers; the Web panel's browser confirm is only a client-side prompt and can be bypassed.
- Suggested action: Require an authorized administrative principal plus a server-issued confirmation/revision token, and reject deletion while consumers remain active unless teardown is explicitly requested.

## ISSUE-005: The extension-delete lifecycle has no regression coverage.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: Static search found no test referencing extensions.delete, EXTENSIONS_DELETE, _handle_extensions_delete, or RailManager.delete_extension. The only RailManager test checks its extensions-root path; no test covers active teardown, traversal keys, partial failure, concurrency, authorization, wire response, or cross-agent behavior.
- Suggested action: Add handler and manager lifecycle tests for unregister, traversal containment, partial failure/rollback, concurrent toggle/delete, authorization/revision, response encoding, and multiple active agents.

## ISSUE-006: Recursive filesystem deletion runs synchronously on the shared event loop.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: Although the handler is async, lines 5988-5989 call a synchronous manager method before the next await. delete_extension performs shutil.rmtree and a JSON file rewrite inline, so a large or slow extension tree stalls all AgentServer WebSocket work on that loop.
- Suggested action: Move bounded filesystem work to a worker thread or asynchronous job, report progress/result, and enforce extension size/file-count limits.
