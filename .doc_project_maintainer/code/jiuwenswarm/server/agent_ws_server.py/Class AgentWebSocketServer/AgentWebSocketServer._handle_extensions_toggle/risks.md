---
symbol: AgentWebSocketServer._handle_extensions_toggle
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_extensions_toggle audit evidence

## ISSUE-001: A missing enabled field silently means disable.

- Dimension: `input_contract`
- Severity: `high`
- Status: `open`
- Evidence: Current code uses params.get('enabled', False), so an omitted key becomes False and the later enabled is None check cannot detect omission; the request proceeds to persist and hot-reload disable.
- Suggested action: Require the key explicitly and reject missing values before mutation.

## ISSUE-002: Non-boolean enabled values can invert intended behavior.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: enabled is never boolean-type checked. A value such as string 'false' is assigned to RailExtension.enabled, serialized to JSON, and treated truthy by hot_reload_rail's enable branch.
- Suggested action: Accept only a JSON boolean and reject all other types.

## ISSUE-003: Persistent config is changed before runtime application and is not rolled back.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: manager.toggle_extension mutates in-memory enabled and writes JSON before hot_reload_rail. Missing Agent, plugin load, register, or unregister failure returns ok=false but leaves configured state changed without compensation.
- Suggested action: Restore prior memory/file state when runtime update fails.

## ISSUE-004: Hot reload targets an unqualified single Agent and tracks registration globally.

- Dimension: `dependency_coupling`
- Severity: `high`
- Status: `open`
- Evidence: Current get_agent_nowait() omits request.channel_id/project/mode. Singleton RailManager stores one _agent_instance and one global _registered_rails set, so rebinding cannot represent registration state across Agent variants.
- Suggested action: Track per-Agent registrations and update every intended runtime.

## ISSUE-005: No toggle handler or Rail hot-reload tests were found.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: Repository search finds no _handle_extensions_toggle, toggle_extension, or hot_reload_rail test covering enabled validation, Agent identity, rollback, failures, concurrency, encoding, or locking.
- Suggested action: Test the handler/manager with temp config, multiple Agents, failures, and concurrency.

## ISSUE-006: Concurrent toggles can finish with config and registration in opposite states.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: Requests run concurrently and no lock spans toggle_extension plus awaited hot_reload_rail. A disable can observe 'not registered' and return while an earlier enable is still awaiting registration, after which that enable adds the rail despite durable disabled state.
- Suggested action: Serialize per-extension transitions and verify/reconcile configured and registered state after the awaited operation.
