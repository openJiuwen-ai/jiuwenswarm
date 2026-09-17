---
symbol: AgentWebSocketServer._handle_command_chrome
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_command_chrome audit evidence

## ISSUE-001: Reports success with an empty payload without executing or describing a Chrome action.

- Dimension: `output_contract`
- Severity: `medium`
- Status: `open`
- Evidence: Lines 3458-3464 unconditionally build ok=true with payload={} and never inspect request.params or invoke browser code, while chrome.ts reports that the command was dispatched to the agent backend.
- Suggested action: Implement the intended Chrome behavior or make the route an explicit no-op/status acknowledgement.

## ISSUE-002: The frontend chrome command file appears disconnected from builtin command registration.

- Dimension: `boundary_safety`
- Severity: `medium`
- Status: `open`
- Evidence: chrome.ts exports createChromeCommand and Gateway/AgentServer retain the command.chrome forwarding route, but registry.ts neither imports the factory nor includes it in createBuiltinCommands.
- Suggested action: Register the command path intentionally, or remove the dead builtin and forwarding surface.

## ISSUE-003: No direct handler, dispatch, or frontend registration tests cover command.chrome.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: Static searches found no direct test for _handle_command_chrome, ReqMethod.COMMAND_CHROME dispatch, createChromeCommand, or its builtin registration path.
- Suggested action: Add tests for dispatch, response payload semantics, and command registration.

## ISSUE-004: The exception guard excludes the meaningful failure points.

- Dimension: `error_handling`
- Severity: `medium`
- Status: `open`
- Evidence: Lines 3458-3472 wrap only dataclass response construction; encoding and send_wire_payload execute afterward. Their failures bypass this handler's command.chrome log/error branch and instead rely on _handle_message's outer transport/error policy.
- Suggested action: Remove the ineffective local guard and rely on the explicit outer transport policy, or scope error handling around the real Chrome operation if one is implemented.
