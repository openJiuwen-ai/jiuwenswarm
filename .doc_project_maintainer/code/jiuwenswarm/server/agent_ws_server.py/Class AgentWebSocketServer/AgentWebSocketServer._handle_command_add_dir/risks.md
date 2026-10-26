---
symbol: AgentWebSocketServer._handle_command_add_dir
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_command_add_dir audit evidence

## ISSUE-001: Durable trust mutation accepts any non-empty path from the command route.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: _handle_message routes by COMMAND_ADD_DIR without a handler-local caller/channel authorization check. Any nonempty or stringifiable path reaches persist_cli_trusted_directory, which expanduser/resolve(strict=False)s it and writes a global permissions.external_directory allow entry without requiring existence or directory type.
- Suggested action: Gate this command by channel, auth, or permission policy and validate intended directory scope/existence before persistence.

## ISSUE-002: remember is echoed but ignored.

- Dimension: `input_contract`
- Severity: `medium`
- Status: `open`
- Evidence: Current handler reads remember with default false, but every accepted path calls persist_cli_trusted_directory regardless of that value; remember is only copied into the response.
- Suggested action: Honor remember=false as non-persistent behavior, or remove/document it as response metadata only.

## ISSUE-003: The handler no longer performs a blocking or best-effort agent reload.

- Dimension: `output_contract`
- Severity: `low`
- Status: `fixed`
- Evidence: Current method contains no get_config/reload_agents_config path after persistence. test_handle_command_add_dir_does_not_wait_for_agent_reload injects a blocking reload and asserts it is never started.
- Suggested action: No handler change required; retain the non-reload regression test.

## ISSUE-004: Direct tests do not cover real YAML writes, invalid inputs, or authorization boundaries.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: Two direct tests stub persistence and cover success payload plus fixed no-reload behavior; empty/non-dict params, helper exception/failure, real temp-config mutation, send failure, and unauthorized routing remain untested.
- Suggested action: Add empty/non-dict input, helper failure, temp-config persistence, send-failure, and unauthorized-routing tests.
