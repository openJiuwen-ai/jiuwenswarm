---
symbol: AgentWebSocketServer._handle_extensions_import
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_extensions_import(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: weak
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: persistent
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: high
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:39:36Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:0488b3a4ef4ac872616280f0f8a577d2f9c58bcaf853b2e58f362d4f63a53d5f
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "A forwarded RPC can persist arbitrary host Python code as an installable Rail."
    evidence: "At HEAD 39feee89, folder_path comes directly from the forwarded request and the handler validates only. See AgentWebSocketServer._handle_extensions_import/risks.md#issue-001."
    suggested_action: "Require authorized import/root, reject symlinks, verify manifest/signature/policy, and require trusted-code approval."
  - id: ISSUE-002
    dimension: state_mutation
    severity: high
    status: open
    summary: "Import persistence is non-transactional and unsynchronized."
    evidence: "At HEAD 39feee89, RailManager import recursively copies/replaces the destination, mutates singleton. See AgentWebSocketServer._handle_extensions_import/risks.md#issue-002."
    suggested_action: "Serialize imports and stage validation/copy/config in a temporary location, then atomically commit or roll back every."
  - id: ISSUE-003
    dimension: performance_risk
    severity: high
    status: open
    summary: "Unbounded synchronous folder ingestion blocks the AgentServer event loop."
    evidence: "At HEAD 39feee89, the async handler calls synchronous manager.import_extension before its response send.. See AgentWebSocketServer._handle_extensions_import/risks.md#issue-003."
    suggested_action: "Preflight a bounded regular-file tree, reject links/special files and remote paths, then perform staged copying."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "The extension-import trust and persistence boundary is untested."
    evidence: "At HEAD 39feee89, no direct test references extensions.import, EXTENSIONS_IMPORT. See AgentWebSocketServer._handle_extensions_import/risks.md#issue-004."
    suggested_action: "Cover authorization, path/symlink policy, malicious code shapes, quotas, duplicate concurrency, copy/config failures."
---

# AgentWebSocketServer._handle_extensions_import

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_extensions_import/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_extensions_import/risks.md)
