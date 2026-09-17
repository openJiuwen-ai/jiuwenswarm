---
symbol: AgentWebSocketServer._resolve_active_project_dir
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_resolve_active_project_dir(self, channel_id: str, params: dict[str, Any] | None = None) -> str | None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: medium
  complexity: medium
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: weak
  output_contract: partial
  side_effects: none
  error_handling: flawed
  state_mutation: none
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: low
audit:
  status: unaudited
  auditor: null
  audited_at: null
  audited_commit: null
  audited_source_hash: null
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: error_handling
    severity: high
    status: open
    summary: "Agent lookup failures make valid request fallbacks unreachable."
    evidence: "An exception from get_agent_nowait returns None immediately, and a missing adapter also returns None. Both exits occur before the documented params.cwd and params.trusted_dirs[0] fallbacks."
    suggested_action: "Treat lookup/adapter failure as a missed candidate, then continue through cwd and trusted_dirs before returning None."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Returning None does not prevent the server working directory from becoming the project root."
    evidence: "The docstring says None avoids Path.cwd(), but callers pass it to build_filesystem_policy, list_effective_sandbox_files, and find_auto_managed_match; their sysop_builder path resolution falls back to Path.cwd(). This can classify or mount the agent-server cwd as writable."
    suggested_action: "Represent 'unknown; do not fallback' explicitly and make all policy/display/match consumers honor it."
  - id: ISSUE-003
    dimension: dependency_coupling
    severity: high
    status: open
    summary: "Unqualified adapter lookup can select a different cached project."
    evidence: "When params.project_dir is absent, get_agent_nowait(channel_id) prefers an agent-mode instance or the first cached instance, without requested mode/project identity. Its adapter project directory then outranks request cwd/trusted_dirs."
    suggested_action: "Resolve from stable request identity first, or require mode/project-qualified lookup and reject ambiguous channel caches."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No resolver or sandbox project-selection tests were found."
    evidence: "No tests cover lookup precedence, no-agent/no-adapter fallthrough, multiple cached projects, invalid adapter values, or the downstream None/Path.cwd behavior."
    suggested_action: "Add table-driven resolver tests plus policy-call tests asserting the selected project root and no server-cwd fallback."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._resolve_active_project_dir`

## Actual Role

Selects the project directory used by sandbox policy dry-runs, auto-managed path checks, and effective-file reporting. It prefers request `project_dir`, then an unqualified adapter value, then request `cwd`/first trusted directory. Lookup failure can return `None` early, which downstream helpers may reinterpret as the server working directory.

## Key Signals

- Input: Channel id and optional project/cwd/trusted-directory request metadata.
- Output: A request/adapter path string or `None`.
- Side effects: None; agent lookup failures are logged at info level.
- Main risk: The wrong host directory can be treated as the sandbox project's writable auto-managed root.
- Tests/flow: No direct or adjacent tests found; no project flow documents this resolution contract.

## Detail Index

- Detail docs pending.
