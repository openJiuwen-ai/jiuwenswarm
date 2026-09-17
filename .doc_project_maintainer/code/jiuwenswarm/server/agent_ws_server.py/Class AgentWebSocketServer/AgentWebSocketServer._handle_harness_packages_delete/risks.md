---
symbol: AgentWebSocketServer._handle_harness_packages_delete
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_harness_packages_delete audit evidence

## ISSUE-001: Deletion trusts persisted runtime_path without containment validation.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: AutoHarnessService loads runtime_path and config_path from the selected harness-packages.json record. It passes config_path to live unload code and converts runtime_path directly to Path for shutil.rmtree without proving either belongs to data_dir/runtime_extensions; an absolute or parent-escaping persisted path can address unrelated configuration or directories.
- Suggested action: Validate records on load and again before use; require resolved config/runtime paths to be non-symlink descendants of the canonical runtime_extensions package root.

## ISSUE-002: The RPC reports success after filesystem, unload, or persistence failure.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: delete_package catches direct unload errors, AgentManager catches every broadcast/fanout error, shutil.rmtree uses ignore_errors=True, and save_packages catches metadata-write errors; none reaches lines 6710-6715, which return ok=true. The service also performs an unlocked read-modify-write, so concurrent delete/activate/deactivate/scan operations can overwrite each other's package and active-ID changes.
- Suggested action: Propagate structured per-stage failures, serialize package lifecycle mutations, and verify unload, contained removal, plus atomic durable metadata replacement before reporting success.

## ISSUE-003: Global deletion does not reliably unload every active consumer.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: The package record and runtime directory are process-global, but line 6709 passes request.channel_id into a broadcast that limits non-empty channels and intentionally visits only agent/agent.fast/agent.plan cache modes, excluding team modes. The disconnected Web fallback deletes with no agent or AgentManager at all; other channels, team agents, or failed consumers can retain loaded resources after metadata and files disappear.
- Suggested action: Enumerate and confirm unload from every active channel/project/session/team consumer before the global commit, or make package ownership and deletion explicitly scope-local.

## ISSUE-004: The destructive global RPC has no server-side authorization boundary.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: Lines 6664-6689 require only a truthy package_id and special-case the literal native value. The handler does not inspect permission_context, an administrator capability, package ownership/project scope, confirmation token, or expected metadata revision before unloading code and recursively deleting shared files.
- Suggested action: Require an authenticated administrator/package owner, explicit global scope, and a server-issued confirmation plus expected revision before deletion.

## ISSUE-005: Deletion can create an expensive agent before it knows whether one is needed.

- Dimension: `dependency_coupling`
- Severity: `medium`
- Status: `open`
- Evidence: Lines 6691-6703 resolve mode/project and await AgentManager.get_agent, which may construct a full runtime variant, before AutoHarnessService loads the package or checks whether it is active. An inactive package deletion can therefore allocate models/tools or fail on unrelated agent initialization, while the Gateway's not-ready fallback deletes the same package without any agent.
- Suggested action: Load and validate package metadata first; only inspect already-live consumers when the package is active, without auto-creating an agent for deletion.

## ISSUE-006: Metadata and recursive deletion I/O block the AgentServer event loop.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: After awaited unload work, delete_package synchronously reads/parses harness-packages.json, recursively removes the runtime tree, serializes all package metadata, and writes the JSON file on the request event loop. Large trees or slow storage stall unrelated WebSocket handling.
- Suggested action: Move bounded filesystem work to a worker/job, enforce package size/file-count limits, and keep lifecycle serialization outside the event loop.

## ISSUE-007: No deletion handler or service test was found.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: Static search found no test referencing delete_package, HARNESS_PACKAGES_DELETE, harness.packages.delete, or harness.delete. Containment, authorization, inactive deletion without agent creation, direct/broadcast unload failure, team/cross-channel consumers, rmtree/save failure, concurrency, fallback parity, response codes, and wire routing are unverified.
- Suggested action: Add handler/service/Gateway-fallback tests for successful global teardown, containment and authorization, every partial failure, concurrency, consumer scope, no-agent behavior, and response/wire contracts.
