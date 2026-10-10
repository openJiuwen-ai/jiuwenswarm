---
last_updated: 2026-09-08
sync_status: partial
coverage_status: partial
flow_coverage_status: partial
code_symbol_coverage_status: partial
---

# Build Plan

## Current State

- Artifact status: AgentServer-first map with normalized Python AST hashes.
- Latest expiration check: the 2026-07-15 scan at `10afedf2` found 0 expired audits among 128 existing `AgentWebSocketServer` method reviews; no symbol was promoted.
- Flow delivery: eight AgentServer flows cover MCP, sandbox, plan exit, Auto-Harness, and history streaming.
- Project status remains partial: most modules, directories, source symbol entry docs, cross-layer flows, and default-health audits are still pending.
- The 2026-07-31/2026-08-01 prewarm follow-ups are flow-synced without widening coverage; affected symbols remain unaudited or audit-expired.
- The 2026-08-18 Persist Session slice is flow-synced; source inventory and signatures were not widened.
- The 2026-09-07 expanded Runtime Session slice is implemented and flow-synced: the new process-local Coordinator/Registry/Scheduler and Process CLI Work/Code reference chain are verified, while AgentServer and all later module adaptations remain legacy. Source inventory and audit signatures were not widened or regenerated.
- The 2026-09-08 follow-up removes the Process CLI rollback and migration executor. Work/Code use the direct facade executor; unsupported CLI modes fail explicitly. AgentServer remains outside this cutover.
- Every stable source file inventoried: yes, by the 2026-07-15 `inventory_symbols.py --verify-docs` scan.
- Every required symbol documented or out of scope: no.
- Every requested-scope audit symbol closure eligible: no; 69 records have entry-document hash mismatches and broader audits remain pending.
- Inventory extractor summary: 1,023 `python_ast`, 252 `heuristic`; heuristic files require review.
- Coverage map recommended mode: multi-agent.
- Latest scanned git head: `10afedf222bcd6db98b24347a28f75e4613b3c87`; this scoped update did not widen the authoritative inventory.

## Inventory Summary

- Source files: 1,275; documented file docs: 1; missing file docs: 1,273; pending review files: 252.
- Required repository symbols: 15,584; documented: 131; missing entry docs: 15,453.
- Default-health symbols: 9,040; repository-coverage-only symbols: 6,544.
- Repository audit statuses: 15,456 unaudited, 128 agent audited, 0 expired, 0 human audited, 0 out of scope.
- Default-health audit statuses: 8,912 unaudited, 128 agent audited, 0 expired.
- Audit integrity: 59 trusted and 69 invalid from entry-document hash differences; all 128 source hashes remain current.
- Open symbol issue records: 492.
- AgentServer queue: 823 frozen methods; 128 documented, 59 closure eligible, 695 unaudited, 0 source-expired. Six later methods remain outside the queue.

## Completed Slices

- 2026-07-07: AgentServer entrypoint, dispatch core, and initial chat/session/push flows.
- 2026-07-13: normalized-AST migration, 52 legacy-expiration re-audits, all 128 method cards, and five additional flows.
- 2026-07-14/15: re-reviewed 64 rebase-expired methods, then confirmed all 128 existing audits source-current at `10afedf2`; six new methods stayed outside scope.
- 2026-07-31: AgentServer-owned session allocation and one-slot DeepAgent prewarming across enabled channels/projects; Web, TUI, IM, ACP, A2A, SSH, fork, and single-Agent Cron creation paths were aligned.
- 2026-08-01/03: Prewarm priority/cache correction and unified TUI startup creation; early RPCs wait for allocation, while explicit IDs retain the compatibility bypass. Focused lifecycle tests pass in both prewarm states.
- 2026-08-18: Persist Session creation contract and prewarm-safe idempotency. Focused regression is green; a Windows atomic-replace test and part of the four-quadrant acceptance remain pending.
- 2026-09-07: Runtime Session foundation and Process CLI reference chain. Nineteen deterministic managed/core tests are included in a 200-test Runtime/SessionManager/Process CLI selection, and two configured-model two-turn Work/Code system gates passed. The next slice is Adapter/AgentManager/Repository port adaptation; AgentServer, Gateway, participants, Team, and background work follow in that order.
- 2026-09-08: Managed execution has no per-request rollback, forwarding executor, implicit registration, or unused lifecycle scaffolding. Unary execution bypasses facade scheduling; external stream cancellation wakes its consumer.

## Completed AgentServer Flow Slices

- `gateway-agentserver-e2a-chat`, `agentserver-session-lifecycle`, `agentserver-server-push`, `agentserver-command-mcp`, `agentserver-sandbox-runtime`, `agentserver-plan-mode-exit`, `agentserver-schedule-auto-harness`, `agentserver-history-stream`, `session-prewarm-allocation`.

## Pending Code And Audit Slices

- `jiuwenswarm/server/agent_ws_server.py`: 158 required symbols; all 128 methods and the class are documented, while 29 top-level functions still need entry docs and audits.
- Other `jiuwenswarm/server` classes: 695 frozen-queue methods remain unaudited; 6 newly observed unaudited methods were intentionally not added in this expiration-only update.
- `jiuwenswarm/server/runtime/agent_adapter`: interface, code, deep, evolution, sysop, and team helpers.
- `jiuwenswarm/server/runtime/skill`: skill manager and skilldev flows.
- `jiuwenswarm/gateway/message_handler`: channel queues and Gateway-to-AgentServer forwarding.
- `jiuwenswarm/agents/harness/team`: distributed team lifecycle and remote member bootstrap.
- `jiuwenswarm/channels/web`, `jiuwenswarm/channels/tui`, and `jiuwenbox`: UI state, command consumers, and sandbox service boundaries.
- `tests/unit_tests/agentserver`: repository-coverage-only test symbols remain largely undocumented.
- Session prewarming and touched TUI/pool/adapter/Gateway symbols remain unaudited or expired; external `clear.spec.ts` replay remains pending.

## Highest-Risk Findings To Carry Forward

- Existing-session IDs still reach filesystem-backed helpers without confirmed containment. New session and Cron identity now originate in AgentServer, but create-token durability and warm resource bounds need follow-up.
- MCP, agent, extension, harness-package, and sandbox mutations lack transactional rollback; extension import also accepts executable host folders.
- ACP responses use process-wide correlation without confirmed connection/session ownership; server push and global managers also have under-specified ownership.
- Model caching collapses same-name providers and lacks a config fingerprint, allowing partial or stale initialization.

## Coverage Closure Audit

- Audit source: `git ls-files` plus `git status --short`, via the 2026-07-15 UTC inventory at `10afedf2`.
- Tracked path audit: partial; 298 suggested repository slices remain.
- Untracked path disposition: `.doc_project_maintainer/.work/` is temporary generation state and must not ship; no candidate source files were found.
- Flow trace disposition: eight AgentServer flows documented; wider project flows remain pending.
- Code symbol disposition: inventory complete, entry docs incomplete, and 252 heuristic files need review.
- Symbol audit disposition: 0 of 128 existing AgentServer records are source-expired; 59 are integrity-trusted, 69 have entry-document hash changes, and the ledger was not widened.
- Criteria to mark the entire artifact `current`: no pending repository/flow/code-symbol slices, no unresolved heuristic review, every required entry doc has `Actual Role` and health, and every requested-scope audit is closure eligible or explicitly out of scope.

## Suggested Subagent Queue

- `agentserver-other-methods`: 695 frozen-queue methods plus six later methods if scope is widened.
- `agentserver-top-level-functions`: 29 missing `agent_ws_server.py` function entry docs and audits.
- `agentserver-downstream-risks`: AgentManager reload, session path containment, scheduler identity, extension lifecycle, and harness package consistency.
- `tests-agentserver`: test evidence inventory, repository coverage only.
