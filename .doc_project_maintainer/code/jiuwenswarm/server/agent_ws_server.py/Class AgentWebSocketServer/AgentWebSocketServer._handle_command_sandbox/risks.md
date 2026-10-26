---
symbol: AgentWebSocketServer._handle_command_sandbox
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_command_sandbox audit evidence

## ISSUE-001: Sandbox writes can leave half-applied persistent state.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89 and in agentserver-sandbox-runtime, enable starts/verifies JiuwenBox, best-effort persists the endpoint, writes enabled=true, then recreates agents; disable writes false before recreate/stop; exclusion/files helpers write YAML before hot patch. No rollback spans these steps, and AgentManager can swallow per-agent rebuild failures.
- Suggested action: Apply first then persist, or roll back/report degraded status when recreation or patching fails.

## ISSUE-002: files.* remote sandbox refresh failures can be swallowed while responses show cached effective files.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, adapter file patching can update the cached policy before force-recreating the remote sandbox, while ordinary failures are warned/softened downstream. The route can still attach effective_files from that cache and report success although applied remote enforcement is stale.
- Suggested action: Propagate files_changed remote recreate failures or include applied_to_remote=false in responses.

## ISSUE-003: enable/disable on a channel with no active agent can become an internal error or ambiguous success.

- Dimension: `error_handling`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, enable/disable call recreate_agent after persistent runtime mutation. The sandbox flow confirms the missing-channel path logs absence and then dereferences the missing agent map, producing SANDBOX_INTERNAL after desired state has already changed.
- Suggested action: Make no-active-agent a clear delayed-effect success or a structured bad-state response.

## ISSUE-004: No direct command.sandbox handler tests were found.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, no direct command.sandbox/COMMAND_SANDBOX handler test was found. Platform guard, status, malformed params, unknown subcommands, enable/disable ordering, endpoint persistence failure, hot-patch failure, enrichment, timeout, concurrency, and response codes are unverified at this boundary.
- Suggested action: Add handler tests for status, platform guard, unknown subcommands, enable/disable, files changes, and attach degradation.

## ISSUE-005: Global sandbox policy is reconciled only against request-scoped runtime variants.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: agentserver-sandbox-runtime confirms config.yaml sandbox state is global, but enable/disable recreates only the request channel and hot patches select one unqualified current agent rather than every channel/mode/project variant. A successful response therefore does not establish convergence for all agents consuming the global policy.
- Suggested action: Fan out reconciliation across all live variants, or version desired/applied policy per runtime and report incomplete convergence explicitly.

## ISSUE-006: Successful disable is not restart-stable for explicit internal startup.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: The sandbox flow confirms that _bootstrap_internal_jiuwenbox runs when startup_mode is explicitly internal, ignores persisted enabled=false, starts JiuwenBox, and best-effort rewrites enabled=true. This handler can report a successful disable that the next AgentServer startup reverses.
- Suggested action: Define enabled as authoritative during bootstrap or return/document a persistent startup-mode change as part of disable.

## ISSUE-007: Malformed params can bypass the sandbox-specific error contract.

- Dimension: `input_contract`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, params = request.params or {}, params.get(...), and sub normalization occur before the handler try block. A truthy non-mapping params value raises before SANDBOX_BAD_REQUEST/SANDBOX_INTERNAL normalization and falls through to the generic outer request error path.
- Suggested action: Validate params as a mapping inside the protected block and return SANDBOX_BAD_REQUEST for malformed request shape.
