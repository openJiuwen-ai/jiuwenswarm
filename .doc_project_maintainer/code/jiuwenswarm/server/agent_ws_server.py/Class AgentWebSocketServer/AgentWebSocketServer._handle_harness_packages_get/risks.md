---
symbol: AgentWebSocketServer._handle_harness_packages_get
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_harness_packages_get audit evidence

## ISSUE-001: A GET can replace corrupt package state with a fresh inactive scan.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, get_packages_info delegates to load_packages, whose broad read/JSON fallback scans runtime extensions with no preserved active-package state and saves a replacement harness-packages.json. The handler reports ordinary ok=true without distinguishing missing-file bootstrap from corrupt/read failure repair or activation-state loss.
- Suggested action: Separate missing bootstrap from read failure; back up corrupt state and require explicit repair.

## ISSUE-002: Parsed package metadata is not schema-validated before success.

- Dimension: `output_contract`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, _load_packages_no_fallback returns the decoded JSON value without enforcing the annotated dict/package schema. This handler forwards it as a successful payload, while Gateway normalization can collapse a non-dict value to {}, hiding malformed persistent state.
- Suggested action: Validate top-level and package-state fields; return a typed error.

## ISSUE-003: Construction blocks the loop and gives GET implicit mutations.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, AutoHarnessService is constructed on the event-loop thread before asyncio.to_thread. Construction creates data/temp directories, loads or bootstraps config, and initializes scheduler components; only get_packages_info runs in the worker. Its fallback may also scan/write metadata, with persistence failure softened downstream.
- Suggested action: Use a lightweight read-only repository or reused service; move I/O off-loop and surface persistence failures.

## ISSUE-004: No direct handler or package-info contract tests were found.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, no direct get_packages_info, HARNESS_PACKAGES_GET, harness.packages.get, or handler test was found. Valid/missing/corrupt/wrong-shape data, fallback rewrite, constructor/write failure, routing, encoding/send, and response envelopes are unverified.
- Suggested action: Test valid/missing/corrupt/wrong-shape data, persistence/init failure, routing, and response envelopes.
