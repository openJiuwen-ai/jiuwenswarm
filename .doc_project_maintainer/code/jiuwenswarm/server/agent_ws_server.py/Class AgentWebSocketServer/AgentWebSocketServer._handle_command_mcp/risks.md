---
symbol: AgentWebSocketServer._handle_command_mcp
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_command_mcp audit evidence

## ISSUE-001: Add/update normalization can drop runtime-supported fields.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: _normalize_mcp_payload reconstructs a whitelist entry and never preserves server_id; it preserves timeout_s only for non-stdio transports. common.mcp_config.build_mcp_server_config consumes explicit server_id and timeout_s for both stdio and HTTP-style transports, so add/update can silently discard runtime-supported values.
- Suggested action: Define one shared persisted-entry schema with the runtime builder and preserve validated server_id/timeout_s fields across add and partial update.

## ISSUE-002: Temporary tool discovery is not explicitly timeout-bounded.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: show and list_tools fall back to _fetch_mcp_tools_from_config, whose client.connect(), client.list_tools(), and client.disconnect() awaits have no asyncio timeout and do not propagate the configured timeout_s. A frontend request timeout only stops waiting client-side; it does not bound this server task.
- Suggested action: Use the shared MCP builder, bound connect/list/disconnect individually and overall, and return structured degraded/timeout status.

## ISSUE-003: list_tools can return ok=True with an empty tool list for an unknown server.

- Dimension: `output_contract`
- Severity: `medium`
- Status: `open`
- Evidence: list_tools first probes private ToolMgr state, then consults config only when no tools were found. If both runtime and config miss the requested name, it still returns ok=true with type=tools/tools=[], whereas show/update/enable/disable/remove map a missing config to MCP_NOT_FOUND.
- Suggested action: Return MCP_NOT_FOUND when runtime and config both miss the server.

## ISSUE-004: action=update rewrites config and enters reload even when normalization is unchanged.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: The add branch compares old and normalized config before reload, but update directly upserts and invokes reload. AgentManager may fingerprint-skip a repeated configuration only when its prior fingerprint matches; the redundant config write and reload entry still occur at this boundary.
- Suggested action: Reuse the add-path equality check before persisting or invoking reload, and test an unchanged partial update.

## ISSUE-005: Persisted config and live runtime can diverge after reload failure.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: Add/update/enable/disable/remove persist config before reload. A raised reload error is converted to ok=true with applied=false and no rollback; the TUI command flow treats a resolved request as an informational success, and the interactive remove flow unconditionally announces removal.
- Suggested action: Use transactional apply/rollback or a durable pending-reload state, and make clients surface applied=false as a failed/degraded operation with recovery guidance.

## ISSUE-006: HTTP/SSE add can report applied=True even if runtime registration is skipped.

- Dimension: `output_contract`
- Severity: `medium`
- Status: `open`
- Evidence: Add pre-checks only enabled stdio entries whose args look like local file paths. During reload, the runtime's HTTP reachability preflight can return False and silently skip registration; its callers do not aggregate that False, so the handler still reports applied=true.
- Suggested action: Preflight HTTP/SSE before persistence or propagate per-server runtime registration results through reload into the command response.

## ISSUE-007: Sensitive-field masking misses common MCP credential names.

- Dimension: `boundary_safety`
- Severity: `medium`
- Status: `open`
- Evidence: _mask_sensitive_fields matches api_key, token, authorization, and secret substrings plus a few value prefixes, but misses common env/header keys such as password, cookie, private_key, and access_key. The list/show responses expose recursively masked config objects; the only direct mask test covers env.TOKEN.
- Suggested action: Expand credential-key/value detection and add nested env/header tests for password, cookie, private-key, and cloud access-key variants.
