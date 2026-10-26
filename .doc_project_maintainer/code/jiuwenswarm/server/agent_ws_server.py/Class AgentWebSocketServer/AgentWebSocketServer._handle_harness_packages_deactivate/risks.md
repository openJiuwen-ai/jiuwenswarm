---
symbol: AgentWebSocketServer._handle_harness_packages_deactivate
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_harness_packages_deactivate audit evidence

## ISSUE-001: Runtime and durable deactivation can diverge while success is returned.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: Current deactivate_package catches and logs selected-agent unload failures, while AgentManager catches every session-adapter and instance fanout failure. A missing config path skips both. update_active_status then mutates metadata, but save_packages also catches write failure; the handler still returns ok=true, so loaded resources may be marked inactive or unloaded resources may remain durably active.
- Suggested action: Collect fanout outcomes and commit durably as one serialized transaction, with compensation or explicit partial failure.

## ISSUE-002: Global activation metadata is reconciled only in one channel.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: Activation state lives in the single user-workspace harness-packages.json, but the handler always passes one request channel (`web` fallback) and AgentManager filters fanout to that normalized channel. It also targets only agent/agent.fast/agent.plan cache modes, so other channels and excluded modes can retain a package now globally marked inactive.
- Suggested action: For global state fan out to all channels, or persist activation per channel/project.

## ISSUE-003: Unknown package IDs are reported as successful no-ops.

- Dimension: `output_contract`
- Severity: `medium`
- Status: `open`
- Evidence: Current deactivate_package checks membership in active_package_ids before find_package_by_id. Any unknown or malformed truthy ID that is not active returns a deactivated_package_id payload and the handler sends ok=true, so the ValueError-to-NOT_FOUND-style mapping is never reached for the ordinary unknown-ID case.
- Suggested action: Validate package existence first, then distinguish already inactive.

## ISSUE-004: No deactivation handler or service tests were found.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: Repository search finds no test invoking deactivate_package, HARNESS_PACKAGES_DEACTIVATE, harness.deactivate, or this handler. Fanout scope, missing config, unload/write failures, unknown and non-string IDs, selected-agent identity, request mode mutation, routing, and wire responses remain unverified.
- Suggested action: Test success, partial failures, scope, IDs, identity, persistence, routing, and wire responses.
