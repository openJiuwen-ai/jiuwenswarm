---
symbol: AgentWebSocketServer._handle_command_model
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_command_model audit evidence

## ISSUE-001: add_model and list behavior are stubs on the direct AgentServer path.

- Dimension: `implementation_soundness`
- Severity: `medium`
- Status: `open`
- Evidence: Current add_model only string/strips target, logs it, and returns model_added even when blank; it ignores model configuration. The default/list branch reports available=['default-model'] instead of configured models, unlike the separate Gateway handler.
- Suggested action: Route through the gateway handler or implement its durable config contract.

## ISSUE-002: switch_model can report applied=true after partial failure.

- Dimension: `error_handling`
- Severity: `high`
- Status: `open`
- Evidence: Current switch loops over arbitrary env_updates into os.environ, swallows cache-clear and reload exceptions, then returns ok=true/applied=true. A non-string key/value can also fail after earlier keys were written, with no rollback.
- Suggested action: Propagate reload failure or return applied=false/partial status, and validate env_updates before arbitrary global env writes.

## ISSUE-003: The direct AgentServer switch_model path lacks coverage.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: test_agentserver_cli_commands.py directly covers only no-action status and add_model. Switch success, placeholder rejection, non-dict/non-string env input, partial mutation, reload failure, and concurrent switches are not covered.
- Suggested action: Add direct switch_model success, validation, and reload-failure tests.

## ISSUE-004: switch_model logging can expose API credentials.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: The switch log comprehension masks only the exact key API_KEY; VIDEO_API_KEY, VISION_API_KEY, EMBED_API_KEY, tokens, authorization, and secrets remain visible even though the class already has a broader _mask_sensitive_fields helper.
- Suggested action: Use the shared sensitive-field masker before logging env_updates.

## ISSUE-005: Process-global model switching has no serialization or rollback.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: The connection layer runs requests concurrently, while this method mutates os.environ key-by-key, clears a global config cache, and awaits global agent reload without a handler lock or revision check; overlapping switches can observe and report mixed state.
- Suggested action: Serialize model switches and apply validated updates transactionally, restoring the previous environment/cache state on failure.
