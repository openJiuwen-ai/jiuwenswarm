---
symbol: AgentWebSocketServer._handle_extensions_import
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_extensions_import audit evidence

## ISSUE-001: A forwarded RPC can persist arbitrary host Python code as an installable Rail.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, folder_path comes directly from the forwarded request and the handler validates only existence/directory shape before RailManager import. Downstream validation checks a name, rail.py, a Rail marker, and compileability; there is no authorized root, symlink, provenance, or behavior policy before the copied Python is later loaded with AgentServer privileges.
- Suggested action: Require authorized import/root, reject symlinks, verify manifest/signature/policy, and require trusted-code approval.

## ISSUE-002: Import persistence is non-transactional and unsynchronized.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, RailManager import recursively copies/replaces the destination, mutates singleton extension state, and rewrites JSON without a shared transaction or import lock. Mid-sequence failure can leave destination files, registry memory, and persisted metadata divergent; concurrent same-name imports can race through removal/copy/config writes.
- Suggested action: Serialize imports and stage validation/copy/config in a temporary location, then atomically commit or roll back every layer.

## ISSUE-003: Unbounded synchronous folder ingestion blocks the AgentServer event loop.

- Dimension: `performance_risk`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, the async handler calls synchronous manager.import_extension before its response send. That call reads rail.py, recursively copies the caller-selected tree, and rewrites config with no size/file-count quota, timeout, or off-loop boundary for remote paths, links, or special files.
- Suggested action: Preflight a bounded regular-file tree, reject links/special files and remote paths, then perform staged copying off-loop with quotas and timeout.

## ISSUE-004: The extension-import trust and persistence boundary is untested.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, no direct test references extensions.import, EXTENSIONS_IMPORT, _handle_extensions_import, or RailManager.import_extension. Existing RailManager coverage only checks generic destination behavior; authorization, hostile trees, duplicate replacement, rollback, response semantics, and later enable/load remain unverified, and no dedicated extension lifecycle flow exists.
- Suggested action: Cover authorization, path/symlink policy, malicious code shapes, quotas, duplicate concurrency, copy/config failures, rollback, wire errors, and later enable/load behavior.
