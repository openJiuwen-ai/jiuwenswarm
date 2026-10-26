---
symbol: AgentWebSocketServer._handle_agents_update
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_agents_update audit evidence

## ISSUE-001: The request can select an arbitrary host workspace for a persistent write.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: Lines 5658-5675 accept workspace_dir from request params and pass it directly to AgentConfigService. The service converts any supplied root to Path and overwrites the active matching definition beneath that root (or a symlinked target); Web and TUI forward agents.update without binding the root to authenticated project identity.
- Suggested action: Resolve and canonicalize authenticated project identity; reject roots/symlink targets outside it.

## ISSUE-002: Concurrent updates can lose data and writes are not crash-atomic.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: AgentConfigService.update_agent resolves the active definition, mutates the in-memory object, then overwrites its Markdown via Path.write_text. No per-definition lock, revision check, temporary file, or atomic replace covers concurrent requests; AgentManager's reload lock begins only after persistence.
- Suggested action: Use per-definition locking plus revision/ETag conflict detection, and write through a temporary file followed by atomic replace.

## ISSUE-003: A persisted update can be reported as RPC success while live agents remain stale.

- Dimension: `output_contract`
- Severity: `medium`
- Status: `open`
- Evidence: Line 5675 overwrites the definition before lines 5680-5685 attempt global hot reload. Reload failure is swallowed, and lines 5687-5697 return ok=true with applied=false; the TUI update flow checks only payload.error and otherwise displays 'Agent updated', so durable and live runtime state can diverge without a visible failure.
- Suggested action: Return a failed/degraded RPC that clients must surface, or roll back/retry reload and provide explicit recovery guidance.

## ISSUE-004: Service happy paths exist, but the update RPC boundary is untested.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: Service tests cover field update plus builtin/nonexistent rejection. Static search found no direct RPC/handler test; workspace authorization, unknown fields, no-op updates, concurrency, generation, reload failure, and wire response are uncovered.
- Suggested action: Add handler/Gateway contracts, concurrent/failed-write and reload-failure cases, and a flow doc.

## ISSUE-005: Unknown or empty update fields can be reported as a successful update.

- Dimension: `input_contract`
- Severity: `medium`
- Status: `open`
- Evidence: Lines 5672-5673 silently retain only UpdateAgentParams dataclass fields. Typos and unsupported keys are discarded; if no recognized field remains, UpdateAgentParams contains only None values, yet update_agent still rewrites the existing file and the handler enters reload before returning success.
- Suggested action: Reject unknown fields and require at least one recognized effective change; compare normalized before/after definitions to skip no-op writes and reloads.
