---
last_updated: 2026-08-01
sync_status: partial
coverage_status: partial
flow_coverage_status: partial
code_symbol_coverage_status: partial
---

# Build Plan

## Current State

- Artifact status: AgentServer-first delivery rebuilt with normalized Python AST symbol hashes.
- Latest expiration check: the 2026-07-15 normalized-AST scan at `10afedf2` found 0 expired audits among the 128 existing `AgentWebSocketServer` method reviews. No method card or audit signature required refresh, and no previously unaudited symbol was promoted.
- Flow delivery: eight AgentServer flows are documented, including MCP, sandbox, plan exit, scheduled Auto-Harness, and history streaming.
- Project status remains partial: most modules, directories, source symbol entry docs, cross-layer flows, and default-health audits are still pending.
- The 2026-07-31/2026-08-01 scoped prewarm follow-ups are synced without widening coverage or claiming a health audit. They record the global slot cap, foreground promotion/cancellation, registry serialization, sync coalescing, MemoryRail singleflight, and canonical code cache identity; affected symbols remain unaudited or audit-expired.
- Every stable source file inventoried: yes, by the 2026-07-15 `inventory_symbols.py --verify-docs` scan.
- Every required symbol documented or out of scope: no.
- Every requested-scope audit symbol closure eligible or out of scope: no; 0 AgentWebSocketServer method audits are source-expired, while 69 records currently have entry-document hash mismatches and broader repository audits remain pending.
- Inventory extractor summary: 1,023 `python_ast`, 252 `heuristic`; heuristic files require review.
- Coverage map recommended mode: multi-agent.
- Latest scanned git head: `10afedf222bcd6db98b24347a28f75e4613b3c87`; this scoped update did not widen the authoritative inventory.

## Inventory Summary

- Source files: 1,275; documented file docs: 1; missing file docs: 1,273; pending review files: 252.
- Required repository symbols: 15,584; documented: 131; missing entry docs: 15,453.
- Default-health symbols: 9,040; repository-coverage-only symbols: 6,544.
- Repository audit statuses: 15,456 unaudited, 128 agent audited, 0 expired, 0 human audited, 0 out of scope.
- Default-health audit statuses: 8,912 unaudited, 128 agent audited, 0 expired.
- Audit integrity: 59 trusted, 0 provisional, 0 suspicious, 69 invalid because entry-document hashes differ from their signed state; 128 unique signature batches and 59 closure-eligible records. This does not change the source-expiration result: all 128 existing method hashes remain current.
- Open symbol issue records: 492.
- AgentServer method queue: the frozen queue remains 823 methods under `jiuwenswarm/server/`; 128 documented and `agent_audited`, 59 currently closure eligible, 695 unaudited, and 0 source-expired. The fresh scan observed 829 server methods, including 6 additional unaudited methods that were not added to this scoped delivery.

## Completed Slices

- 2026-07-07: AgentServer entrypoint, dispatch core, and initial chat/session/push flows.
- 2026-07-13: normalized-AST migration, 52 legacy-expiration re-audits, all 128 method cards, and five additional flows.
- 2026-07-14: `agentserver-rebase-expiration-reaudit` - re-reviewed exactly the 64 existing AgentWebSocketServer methods expired by the rebase at `39feee89`; all 64 are trusted, with no new unaudited symbol added or promoted.
- 2026-07-15: `agentserver-expiration-scan` - scanned `10afedf2`; all 128 existing AgentWebSocketServer method audits remain source-current, 0 expired, and 6 newly observed unaudited server methods were left outside the frozen queue.
- 2026-07-31: AgentServer-owned session allocation and one-slot DeepAgent prewarming across enabled channels/projects; Web, TUI, IM, ACP, A2A, SSH, fork, and single-Agent Cron creation paths were aligned.
- 2026-08-01: Prewarm priority/cache correction: one slot, foreground priority, canonical code identity, lightweight Adapter guard, CodeCheck cleanup, and 167 final tests.

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
- Session prewarming remains unaudited. Modified stream/unary wrappers are `audit_expired`; new pool, adapter, Gateway filter, and implementation-helper symbols are pending audit.

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
- Symbol audit disposition: the latest 128-method AgentWebSocketServer expiration check has 0 source-expired records. Current verification trusts 59 and flags 69 as integrity-invalid only because their entry-document hashes changed. The fresh repository scan observed 8,963 unaudited default-health symbols, but the frozen authoritative ledger was not widened in this scoped update.
- Criteria to mark the entire artifact `current`: no pending repository/flow/code-symbol slices, no unresolved heuristic review, every required entry doc has `Actual Role` and health, and every requested-scope audit is closure eligible or explicitly out of scope.

## Suggested Subagent Queue

- `agentserver-other-methods`: 695 unaudited methods from the frozen `audit-queues/server-method-audit-queue.json`, plus 6 newly observed methods to consider only if the audit scope is explicitly widened; one agent assignment per symbol.
- `agentserver-top-level-functions`: 29 missing `agent_ws_server.py` function entry docs and audits.
- `agentserver-downstream-risks`: AgentManager reload, session path containment, scheduler identity, extension lifecycle, and harness package consistency.
- `tests-agentserver`: test evidence inventory, repository coverage only.
