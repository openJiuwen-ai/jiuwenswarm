---
symbol: AgentWebSocketServer._handle_harness_packages_scan
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_harness_packages_scan audit evidence

## ISSUE-001: Failed scans can overwrite valid state with partial data.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, every normal return from scan_runtime_extensions is passed directly to save_packages. The scanner catches directory/entry iteration failures and can return a partial package list with active IDs filtered against that partial discovery, so the handler persists degraded state instead of retaining the last valid snapshot.
- Suggested action: Fail the scan, retain the prior snapshot, and save only validated results.

## ISSUE-002: Persistence failure is reported as RPC success.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, save_packages catches write exceptions internally rather than returning/raising a durability result. The awaited to_thread therefore completes normally and this handler emits ok=true with the scanned payload even when harness-packages.json was not updated.
- Suggested action: Raise or return write status and acknowledge only verified persistence.

## ISSUE-003: Package metadata uses an unlocked, non-atomic read-modify-write cycle.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, scan_runtime_extensions reads package/activation state in one worker call and save_packages writes later in a second call, with no revision, shared lock, or atomic replace across the pair. Concurrent scan/activate/deactivate and Web fallback writers can lose activation updates or expose partially written JSON.
- Suggested action: Use cross-process locking, revisions, and temp-file atomic replace.

## ISSUE-004: Heavy service construction still runs on the event loop.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, AutoHarnessService is constructed before the first asyncio.to_thread call. Its constructor creates data/temp directories, loads or bootstraps config, and initializes scheduler-related components on the AgentServer event-loop thread.
- Suggested action: Reuse a managed service or move construction off-loop.

## ISSUE-005: No scan RPC or package-scan persistence test was found.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, no direct test references _handle_harness_packages_scan, HARNESS_PACKAGES_SCAN, harness.packages.scan, scan_runtime_extensions, or save_packages. Partial discovery, swallowed write failure, activation preservation, concurrent writers, routing/fallback parity, and loop responsiveness are unverified; no dedicated flow exists.
- Suggested action: Test failures, partial scans, concurrent writers, atomicity, fallback parity, and loop responsiveness.
