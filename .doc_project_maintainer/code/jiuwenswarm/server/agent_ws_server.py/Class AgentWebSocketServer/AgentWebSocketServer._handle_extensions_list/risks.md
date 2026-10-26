---
symbol: AgentWebSocketServer._handle_extensions_list
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_extensions_list audit evidence

## ISSUE-001: The enabled field is configuration, not effective runtime state.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: Current RailManager.list_extensions returns only cached RailExtension.to_dict records, whose enabled flag is configured state; effective registrations live separately in _registered_rails and are not joined into this response.
- Suggested action: Return configured and applied states separately with the last load error.

## ISSUE-002: Unreadable or malformed configuration becomes a successful empty list.

- Dimension: `error_handling`
- Severity: `high`
- Status: `open`
- Evidence: On singleton initialization, RailManager._load_config catches any file/JSON/from_dict error, logs it, replaces _extensions with {}, and returns normally; this handler then emits ok=true with an empty list and no degraded/error field.
- Suggested action: Preserve the load error and return a typed degraded state.

## ISSUE-003: A read RPC performs synchronous initialization and emits an unbounded snapshot.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: get_rail_manager first initialization synchronously mkdirs the extensions directory and reads/parses extensions_config.json before the handler's first await; list_extensions then materializes every record without pagination or count cap.
- Suggested action: Initialize off-loop and cap or paginate the response.

## ISSUE-004: No list handler or RailManager list contract tests were found.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: The located RailManager test checks only its directory path; an AgentServer startup test fakes a different ExtensionManager. No test calls RailManager.list_extensions or _handle_extensions_list, or covers corrupt config, applied state, limits, or send failure.
- Suggested action: Add RailManager, handler, and Web UI contracts for applied and degraded states.
