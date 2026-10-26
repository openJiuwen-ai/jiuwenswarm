---
symbol: AgentWebSocketServer._handle_hooks_list
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_hooks_list audit evidence

## ISSUE-001: The summary response exposes complete, unbounded hook definitions.

- Dimension: `output_contract`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, get_event_summary is returned verbatim as events with no redaction or size bound. The summary helper embeds each matcher hooks collection, including command and prompt bodies, so a nominal list call can expose full executable/prompt definitions and create a large wire frame.
- Suggested action: Define a privileged detail contract or return bounded, redacted metadata.

## ISSUE-002: The AgentServer RPC is bypassed by duplicated Gateway implementations.

- Dimension: `dependency_coupling`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, Web and TUI register independent local hooks.list handlers while _handle_message also dispatches HOOKS_LIST here. Existing routing evidence found no production sender using this AgentServer copy, leaving three independently evolving response/error contracts.
- Suggested action: Choose one authoritative layer, deprecate the orphan copy, and test retained surfaces against one contract.

## ISSUE-003: Malformed matcher hook collections are accepted and summarized misleadingly.

- Dimension: `input_contract`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, load_hooks_config accepts entry.get('hooks', []) without enforcing a list of valid hook objects. get_event_summary then applies len(...) and returns that malformed value, so strings/mappings can produce plausible counts and invalid hook definitions instead of a validation error.
- Suggested action: Validate matcher and hook objects during load and type the response schema.

## ISSUE-004: Only the summary helper is tested; the RPC and three-surface parity are not.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, test_hooks_config.py covers get_event_summary helper behavior, but no direct test references hooks.list, HOOKS_LIST, or _handle_hooks_list. Malformed config, payload bounds/redaction, encoding/send failure, routing ownership, and three-surface parity remain unverified; no dedicated flow exists.
- Suggested action: Add wire, malformed-config, payload-boundary, routing, and surface-parity tests.

## ISSUE-005: Global config parsing and unbounded payload construction run synchronously on the event loop.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, the async handler calls synchronous get_config, load_hooks_config, and get_event_summary before its first await. Large config files or hook bodies therefore consume the AgentServer WebSocket loop and are then duplicated into an unbounded response.
- Suggested action: Cache a validated bounded summary or move config loading/serialization off-loop with response quotas.
