---
name: project-maintainer
description: Use when initializing or updating structured in-repository project-maintenance docs; exploring existing code; analyzing a full project or repository; documenting complete source symbol coverage, coverage maps, symbol audit maps, or health for every top-level class, top-level function, and class method; summarizing git history; using project-maintainer as maintenance-aware context during a bug fix, feature change, or refactor; or syncing docs after verified code changes.
license: Apache-2.0
---

# Project Maintainer

## Purpose

Maintain a structured project knowledge base inside the target repository at `.doc_project_maintainer/`. Treat the artifact as an agent-readable project map, not a long-form documentation dump.

Use four complementary views:

- Modules are the primary understanding axis: product capability, service boundary, package, subsystem, or feature area.
- Directories are the evidence axis: real paths, file ownership, tests, runtime entrypoints, and git changes.
- Cross-boundary flows are the causal axis: how user-visible output, durable state, external integration state, generated artifacts, background status, or operator/admin decisions come into existence.
- Code symbols are the executable-detail axis: every stable source file, top-level class, top-level function, and method on every top-level class, each with actual behavior, detail indexes, and health assessment. Keep repository coverage separate from default product/runtime health audit scope.

Flow docs are required when behavior crosses module, process, service, runtime, storage, protocol, or UI boundaries and affects state or output that users, operators, integrations, or future agents rely on.

Keep files small enough for selective reading. Prefer indexes, summaries, and links over repeated narrative.

## Task Intent Router

Classify the user's intent before choosing a workflow.

### Knowledge Base Delivery Mode

Use this mode when the user asks to initialize, analyze, map, document, audit, summarize, make current, or deliver `.doc_project_maintainer/` as the main output.

1. Run Preflight.
2. If no artifact exists, initialize or explore according to the relevant workflow below.
3. If an artifact exists, assess staleness and choose the required build, coverage, flow, code symbol, audit, or git-history slices.
4. Continue until the requested deliverable is complete, or leave exact pending slices in `project/build-plan.md` and report partial status.

### Maintenance-Aware Fix Mode

Use this mode when the user asks to use Project Maintainer during a bug fix, feature change, refactor, or defect repair.

Project Maintainer must not be the primary debugging workflow. Reproduction, diagnosis, test-first repair, implementation, and verification belong to the debugging or development workflow. Project Maintainer provides context before the fix and artifact maintenance after the fix.

1. Run a context preflight before changing code:
   - check whether `.doc_project_maintainer/` exists,
   - if it exists, read only the relevant module, directory, flow, code symbol, change, and decision docs,
   - if it does not exist, or if the relevant area is not analyzed, ask the user whether to analyze first before continuing with the fix.
2. If the user declines analysis or the artifact is not available, continue the external debugging workflow with normal code inspection and record that project-maintainer context was unavailable or incomplete.
3. During the fix, note affected modules, directories, flows, code symbols, decisions, tests, and any artifact claims that are stale or contradicted by the code.
4. After verification, artifact maintenance becomes part of the task completion criteria: synchronize affected artifact slices after verification, including module docs, directory docs, flow docs, code symbol docs and health, change records, decisions, manifest links, indexes, and `project/build-plan.md` when relevant.
5. If the artifact is stale in areas that affect the fix, update those stale slices before final response when feasible. If synchronization cannot be completed safely, keep the task partial for project-maintainer purposes and report the exact pending sync items.

## Preflight

Use preflight to decide what artifact exists, whether it can be trusted for the current task, and how broad the next read should be. For full-repository or complete deliverable work, inspect the whole artifact structure through the index, manifest, build plan, coverage map, and audit map. For code changes, feature development, refactors, or scoped maintenance, use preflight to identify the relevant artifact slices, then follow the Reading Strategy below.

1. Identify the target repository root.
2. Check whether `.doc_project_maintainer/` already exists.
3. Ensure the artifact-local audit signing key is available whenever `.doc_project_maintainer/` exists or is being created:
   - Use `.doc_project_maintainer/project/audit-signing-key.json` as the default key record for `PROJECT_MAINTAINER_AUDIT_SIGNING_KEY`.
   - Run `python <skill-dir>/scripts/audit_integrity.py ensure-key --repo-root <repo-root>` during preflight so first-time artifacts get a key before later audit commands.
   - If the environment variable is absent, `scripts/audit_integrity.py` loads this artifact key and creates it on first use.
   - Treat this key as artifact-local agent workflow integrity, not a tamper-proof security boundary. It constrains controlled agent workflows; it does not protect against a user or process that can edit the artifact and rerun signing.
   - Reuse the existing artifact key for later promote, verify, report, and audit visualization commands unless the user explicitly provides a different signing key environment.
4. If the artifact exists, read `.doc_project_maintainer/INDEX.md`, `.doc_project_maintainer/manifest.yaml`, `.doc_project_maintainer/project/build-plan.md`, `.doc_project_maintainer/project/coverage-map.json`, and `.doc_project_maintainer/project/symbol-audit-map.json` when present.
5. Assess whether the existing artifact is stale before editing code or docs:
   - Compare the current task, touched paths, git status, recent commits, and manifest mappings.
   - If the task depends on stale docs, sync the relevant artifact files before relying on them.
   - If stale docs are unrelated to the current task, record or preserve the pending sync state and remind the user at the end.
6. If starting fresh, create the artifact structure described in `references/artifact-structure.md`.
7. Load `references/templates.md` before creating or heavily revising artifact files.
8. Load `references/code-symbol-docs.md` before creating or heavily revising code symbol docs.

## Core Workflows

### Initialize New Project Docs

Use this when the project is new or still being designed.

1. Capture project goal, audience, runtime shape, technical stack, and important constraints.
2. Propose initial modules before detailed files exist.
3. Map planned directories to modules.
4. Record early architecture decisions as decision records.
5. Create `.doc_project_maintainer/README.md`, `INDEX.md`, `manifest.yaml`, project overview, initial module docs, initial directory docs, and the artifact-local audit signing key via `scripts/audit_integrity.py ensure-key`.

### Explore Existing Project

Use this when the repository already has code.

1. Inspect root files, package manifests, build config, test config, and existing docs.
2. List top-level directories and likely entrypoints.
3. Infer modules from package boundaries, command entrypoints, tests, routes, services, and recurring directory names.
4. Identify candidate cross-boundary flows from entrypoints, boundary handlers, event or message types, persistence readers and writers, protocol adapters, queues, workers, schedulers, caches, stores, reducers, replay or hydration logic, renderers, importers, exporters, integrations, and tests.
5. Inventory stable source files, top-level functions, top-level classes, and methods on top-level classes. Record any unsupported language or parser uncertainty in `project/build-plan.md`.
6. Create directory docs as evidence, module docs as interpretation, flow docs for causal behavior that crosses boundaries, and code symbol docs for every inventoried top-level class, top-level function, and class method when the goal is complete coverage.
7. Mark uncertain module boundaries, flow links, or symbol behavior with `confidence: inferred` and add questions to `project/open-questions.md`.
8. For large projects, create or update `project/build-plan.md` before attempting full coverage. Document what has been inventoried or inspected, what is trusted, what remains, and the next suggested module, directory, flow, or code symbol slices.

### Build Large Artifacts In Slices

Use this when the project is too large to map accurately in one pass.

1. Create `project/build-plan.md` and refresh `project/coverage-map.json` plus `project/symbol-audit-map.json` with phases, scope, completed slices, pending slices, stale files, changed hashes, audit status, blocked questions, and integration notes.
2. Prefer module slices when the module boundaries are clear. Prefer directory slices when module boundaries are unknown. Prefer flow slices when the risk is causal behavior across boundaries. Prefer code symbol slices when the task requires class, function, or method behavior and health.
3. When multi-agent tools are available, assign independent slices to agents by module, directory, flow, or code symbol scope. Give each agent only the root index, manifest, build plan, assigned paths or flow scope, and required templates.
4. Require each agent to write or propose updates only for its assigned slice and to report affected modules, directories, flows, code symbols, changes, decisions, confidence, and open questions.
5. Integrate slice outputs centrally by updating `manifest.yaml`, `INDEX.md`, cross-links, `project/coverage-map.json`, `project/symbol-audit-map.json`, and `project/build-plan.md`.
6. Keep incomplete coverage explicit. A partial but honest artifact is better than a broad artifact with invented certainty, but partial coverage is not complete for a full-repository analysis goal.
7. Before marking coverage or sync status as `current`, or before completing a full-repository analysis goal, run the coverage closure audit below and record the result in `project/build-plan.md`.

### Full Repository Analysis Goals

Use this when a user request, Codex goal, or automation asks to analyze, map, document, or cover the whole project or repository, all areas, every module or directory, complete coverage, or project-wide current state.

1. Treat full project coverage as the completion condition, not merely a planning condition.
2. Start with Explore Existing Project, then Build Large Artifacts In Slices, then continue selecting the next actionable pending slice from `project/coverage-map.json`, `project/symbol-audit-map.json`, and `project/build-plan.md` until the coverage closure audit has no actionable pending module, directory, flow, code symbol, or symbol audit slices for the requested scope.
3. An actionable pending slice is any stable tracked path, candidate project file, required flow, stable source file, required top-level class, required top-level function, or required class method that lacks required documentation, lacks a required audit, is audit-expired, is stale, needs parser review, or has not been explicitly marked out of scope with a reason.
4. Do not mark a Codex goal complete while actionable pending slices remain. If context, time, token budget, missing tools, or external blockers prevent continued work, leave the artifact `partial`, update `project/build-plan.md` with the exact next slice, and report the goal as incomplete or blocked rather than complete.
5. Use `current` only when no actionable pending slices remain. Use `partial` when known pending slices, stale areas, unclassified paths, incomplete flow traces, or incomplete code symbol slices remain. Do not use `current` just because every gap has been listed.
6. For large repositories, use multi-agent slicing when available. The coordinator still owns closure: integrate slices, re-run the audit, and continue until no actionable pending slices remain or the goal is explicitly blocked.

### Exhaustive Code Symbol Coverage

Use this for complete deliverables, full-repository analysis, project-wide `current` status, or any request where future developers should be able to query arbitrary implementation detail.

1. Repository coverage includes every stable source file. Stable source files are tracked executable project source files, including tests, scripts, CLIs, workers, app code, library code, and tooling source, unless explicitly generated, vendored, build output, disposable local state, or out of scope with a reason.
2. Product/runtime health audit scope is narrower by default. Default health audit targets only files classified as `runtime_source` or `library_source`; tests, fixtures, scripts, tooling, docs-adjacent source, and package metadata remain repository coverage and verification evidence unless the user explicitly asks to audit them.
3. Every inventoried file must carry `source_role` and `audit_scope`. Use `default_health_audit` for `runtime_source` and `library_source`; use `repository_coverage_only` for test, fixture, script, tooling, package metadata, docs, generated, or other non-runtime support roles.
4. Every stable source file must have a file-level code doc and symbol inventory before repository code symbol coverage can be `current`.
5. Every top-level class must have a class entry doc with a functionality description in `Actual Role`, key signals, health summary fields, confidence, source path, method index, and manifest entry.
6. Every top-level function must have a function entry doc with a functionality description in `Actual Role`, key signals, health summary fields, confidence, source path, and manifest entry.
7. Every method on every top-level class must have a method entry doc with a functionality description in `Actual Role`, key signals, health summary fields, confidence, source path, class owner, and manifest entry. Include constructors, lifecycle methods, magic or dunder methods, static methods, class methods, and private methods when they are methods of a top-level class.
8. Top-level means declared at file or module scope. Nested functions, local classes, anonymous callbacks, generated declarations, overload-only signatures, and type-only declarations do not need separate entry docs unless they are independently callable, exported, risky, or needed to explain a documented flow. Record any exclusion rule in the file-level doc or build plan.
9. Detail docs such as `actual-behavior.md`, `contracts.md`, `side-effects.md`, `health.md`, `risks.md`, and `tests.md` may still be created in slices. However, complete code symbol coverage requires at least the entry doc and health summary for every required class, function, and method.
10. Do not mark repository code symbol coverage, project-wide coverage, sync status, or a full-repository coverage goal `current` while any required source file lacks an inventory, any required top-level class, top-level function, or class method lacks an audit map record, any required top-level class, top-level function, or class method lacks an entry doc, or any entry doc lacks `Actual Role` and health fields.
11. Do not mark default product/runtime health audit `current` while any `default_health_audit` symbol remains `unaudited` or `audit_expired`, unless it is explicitly `out_of_scope` with a reason. A full-repository symbol audit must say so explicitly before tests, scripts, tooling, fixtures, or package metadata become health-audit blockers.
12. If language tooling cannot reliably enumerate symbols, inspect the file manually or record the file as an actionable pending slice. Parser uncertainty never counts as `current`.

### Source Symbol Inventory

Use `scripts/inventory_symbols.py` as the single entry point for complete code symbol inventory. The script must run without external dependencies and use automatic extractor selection to choose the best available extractor.

1. Before claiming complete code symbol coverage, run:
   ```bash
   python <skill-dir>/scripts/inventory_symbols.py <repo-root> --output <repo-root>/.doc_project_maintainer/project/source-symbol-inventory.json --coverage-map-output <repo-root>/.doc_project_maintainer/project/coverage-map.json --audit-map-output <repo-root>/.doc_project_maintainer/project/symbol-audit-map.json --verify-docs
   ```
2. Treat extractor choice as automatic, not user-facing setup. The built-in priority is `python_ast`, then optional `ctags` when present, then dependency-free `heuristic` fallback.
3. Do not ask users to install optional extractors as a prerequisite. If an enhanced extractor is unavailable, continue with the fallback, record the extractor and confidence in `source-symbol-inventory.json`, and keep low-confidence files actionable.
4. The inventory must classify every file with `source_role` and `audit_scope`. Use those fields to keep repository completeness work separate from default product/runtime health audit work.
5. The inventory and coverage map must include `directory_summary` with recorded source directories, excluded directories and reasons, and skipped non-source directories. Review this summary before trusting coverage or audit slices.
6. A file inventoried with `heuristic`, `unknown`, parser warnings, or `requires_review: true` can be used to scaffold docs, but it cannot support `current` until manually reviewed, re-run with a stronger extractor, or marked out of scope with a reason.
7. Use file hashes from the inventory to focus later runs on changed files and symbols. Do not rescan or rewrite stable symbol docs just because the full inventory command was re-run.
8. When `--verify-docs` reports missing entry docs, missing `Actual Role`, missing health fields, `unaudited`, or `audit_expired` symbol audit records, record those items as actionable pending code symbol slices for their relevant scope.

### Coverage Map And Subagent Coordination

Use `project/coverage-map.json` as the machine-readable progress ledger for full-project analysis and complete code symbol coverage.

1. Generate or refresh `project/coverage-map.json` in the same command that generates `project/source-symbol-inventory.json`.
2. Treat file statuses as operational state:
   - `documented`: current source hash, sufficient extractor confidence, file doc present, and required class, function, or method docs include `Actual Role` plus health.
   - `pending`: required file, class, function, method, `Actual Role`, or health docs are missing.
   - `stale`: the source hash changed since the previous coverage map.
   - `pending_review`: extractor confidence, parser warnings, or unsupported language handling needs manual review.
   - `not_checked`: docs were not verified in this run.
3. Use git data from the coverage map to decide work:
   - `git.head` records the scanned commit.
   - `git.status_short` records dirty worktree state.
   - `git.untracked_candidate_source_files` must be classified as project source, generated/local state, ignored, or out of scope.
   - `directory_summary` records which directories were included as source, excluded by rule, or skipped as non-source.
4. Use multi-agent slicing when more than 20 stable source files, more than 80 required symbols, or any module slice with more than 40 symbols is pending or stale. If multi-agent tools are unavailable, continue serially by `suggested_slices` and keep coverage `partial`.
5. Use `suggested_slices` for repository coverage and `suggested_audit_slices` for default product/runtime health audit. Do not use repository-only test, fixture, script, tooling, docs, or package metadata slices as production risk samples unless the user asks for that scope.
6. Coordinator owns repository coverage closure. The Coordinator assigns `suggested_slices` according to the requested scope, gives each subagent only its assigned source paths and required templates, prevents overlapping edits, integrates outputs into `manifest.yaml`, `INDEX.md`, code docs, flow docs, `project/coverage-map.json`, and `project/build-plan.md`, then reruns the inventory command.
7. Coordinator owns health audit closure separately. For `suggested_audit_slices`, expand pending audit work into `multiple symbol audit` assignments and use one audit agent per required symbol by default. The coordinator may group symbols for queue management, but each closure-eligible `agent_audited` record needs its own symbol-level audit workflow below.
8. Subagents must report affected files, functions, class methods, modules, directories, flows, health status, tests, confidence, blockers, and any out-of-scope proposal for their assigned slice only.
9. Do not mark a full-repository goal complete while `coverage-map.json` contains pending, stale, pending_review, not_checked, removed, or candidate project file work that has not been documented, reviewed, removed from the artifact, or explicitly dispositioned.

### Symbol Audit Map

Use `project/symbol-audit-map.json` as the machine-readable audit ledger for every discovered top-level class, top-level function, and method on a top-level class. Use `health_audit_summary` and `health_audit_symbols` for default product/runtime health audit counts.

1. Generate or refresh `project/symbol-audit-map.json` in the same command that generates the inventory and coverage map.
2. Preserve repository-wide audit records for all roles, but treat `default_health_audit` entries as the default product/runtime risk pool. Repository-only entries should support completeness, test evidence, and explicit non-runtime audits.
3. Treat `audit.status` as the audit state:
   - `unaudited`: no agent or human has reviewed the symbol behavior, health, and issues.
   - `script_assessed`: the controlled audit integrity entrypoint processed the symbol, but no agent or human audit has been accepted for trusted closure.
   - `agent_audited`: an agent audit claim was accepted through `scripts/audit_integrity.py promote` with an agent call signature; it is provisional until `verify` or `report` classifies it as `trusted_agent_audit`.
   - `human_audited`: a human reviewed or confirmed the symbol and recorded health plus issues with evidence.
   - `audit_expired`: the symbol was audited, but its current symbol hash differs from the audited symbol hash. Legacy records without a symbol hash use the containing file hash until they can be safely migrated.
   - `out_of_scope`: the symbol is intentionally excluded with a reason.
4. Record health as a fixed-dimension snapshot: `overall`, `name_behavior_match`, `responsibility_focus`, `length`, `complexity`, `implementation_soundness`, `input_contract`, `output_contract`, `boundary_safety`, `side_effects`, `state_mutation`, `error_handling`, `dependency_coupling`, `test_coverage`, `observability`, and `performance_risk`.
5. Record concrete findings in `issues[]`; each issue needs `dimension`, `severity`, `status`, `summary`, `evidence`, and `suggested_action`. Health dimensions classify the risk; issues explain the evidence.
6. Preserve previous `agent_audited` or `human_audited` records when the current symbol hash still matches `audited_symbol_hash`. For Python, use a normalized AST hash so comments, formatting, and line movement do not expire an otherwise unchanged symbol.
7. Automatically treat a previously audited symbol as `audit_expired` when its symbol hash changes. A changed method expires that method and its containing class audit, but not unchanged sibling methods. When reliable symbol boundaries are unavailable, fall back conservatively to the containing file hash and keep parser uncertainty actionable.
8. Migrate a legacy audit that has only `audited_source_hash` by first confirming that the containing file hash is unchanged, then recording the current `audited_symbol_hash`. If the file changed before migration, expire the legacy audit conservatively because the changed symbol cannot be identified safely.
9. Do not mark default product/runtime health audit or a full-repository health-audit goal `current` while required symbols in the requested audit scope remain `unaudited`, `script_assessed`, `audit_expired`, or untrusted `agent_audited`, unless they are explicitly `out_of_scope` with a reason.
10. Treat health-audit closure as a derived predicate, not a raw status count: `closure_eligible` is true only for `human_audited`, `out_of_scope`, or `agent_audited` records whose latest integrity verification result is `trusted_agent_audit`. `script_assessed`, `provisional_agent_audit`, `suspicious_agent_audit`, and `invalid_agent_audit` must remain pending for closure.

### Generate Audit Visualization Report

Use this when the user asks for a human-readable audit summary, visual audit report, dashboard, HTML report, team review artifact, or security-review artifact.

1. Confirm `.doc_project_maintainer/project/coverage-map.json` and `.doc_project_maintainer/project/symbol-audit-map.json` exist. If they are missing or stale for the requested scope, explain that inventory should be refreshed before the report can be trusted.
2. Ensure the artifact-local audit signing key is available. If `PROJECT_MAINTAINER_AUDIT_SIGNING_KEY` is unset, the report refresh uses `.doc_project_maintainer/project/audit-signing-key.json` through `audit_integrity.py`.
3. Run:
   ```bash
   python <skill-dir>/scripts/render_audit_report.py <repo-root>
   ```
4. The report generator refreshes trust classification with `audit_integrity.py report` unless `--skip-integrity-refresh` is explicitly used.
5. Treat the generated `project/audit-report.html` as a presentation artifact only. The JSON maps and symbol docs remain the source of truth.
6. If inventory, coverage maps, symbol audit maps, or audit integrity reports are refreshed after the HTML report is generated, tell the user the report reflects older data and should be regenerated or refreshed in the browser.

### Agent Symbol Audit Contract

Use this contract before changing any symbol audit status from `unaudited`, `script_assessed`, or `audit_expired` to `agent_audited`.

1. `scripts/inventory_symbols.py` is not an auditor. It may inventory symbols, verify entry docs, preserve matching prior audit records, and expire stale audit records, but it must not mark a symbol `agent_audited` by itself.
2. Audit status writes must go through `scripts/audit_integrity.py`. The `promote` command may write `script_assessed` for script-only progress or provisional `agent_audited` when a recent agent call signature batch is supplied. If `PROJECT_MAINTAINER_AUDIT_SIGNING_KEY` is absent, the script loads or creates `.doc_project_maintainer/project/audit-signing-key.json`.
3. A symbol may become `agent_audited` only after a real audit agent has reviewed that assigned symbol or slice. The audit agent must read the symbol implementation, relevant callers or callees needed to understand behavior, related tests or missing-test evidence, and any linked flow or code symbol docs that affect the health judgment.
4. The audit agent must record evidence-based health dimensions and issues for the assigned class, top-level function, or class method. Evidence should cite observed behavior, source paths, tests, error handling, state mutation, side effects, contracts, or missing verification.
5. A coordinator may copy or integrate the audit agent's conclusion into entry docs and `project/symbol-audit-map.json`, but the coordinator must not mark a symbol `agent_audited` from script output, extractor confidence, generated health placeholders, or the mere existence of an entry doc.
6. If `promote` is missing required agent-promotion metadata such as `--agent-call-signature-json`, it should downgrade the record to `audit.status: script_assessed` and report `missing_agent_call_signature` instead of failing the whole workflow by default.
7. An audit agent assignment is incomplete until `scripts/audit_integrity.py promote` records `audit.status: agent_audited` for that exact symbol using that agent's recent call signature batch. If promotion fails or downgrades to `script_assessed`, the symbol remains pending and the agent or coordinator must report the pending state.
8. If no real audit agent or human has performed the review, the symbol must remain `unaudited` or `script_assessed`, even when entry docs contain `Actual Role`, health fields, and no known issues.

### Symbol Health Audit Workflow

Use this workflow whenever the task asks for health, risk, correctness, or audit judgment for a class, top-level function, method, or signature.

1. Classify the request before doing audit work:
   - `single symbol audit`: exactly one class, top-level function, method, or signature is in scope. The current agent must personally read that symbol's implementation, relevant callers or callees needed to understand behavior, related tests or explicit missing-test evidence, and linked flow or code symbol docs before recording health.
   - `multiple symbol audit`: two or more classes, top-level functions, methods, or signatures are in scope. The coordinator must create one audit agent per required symbol by default. Each audit agent must complete code exploration, health judgment, entry-doc update, and controlled promotion to `audit.status: agent_audited` for only its assigned symbol.
2. For a `single symbol audit`, update the symbol entry doc with evidence-backed `Actual Role`, health dimensions, issues, and key signals, then run `scripts/audit_integrity.py promote` for that symbol only. Run `verify` or `report` afterward and keep the symbol pending unless the latest result is closure-eligible.
3. For a `multiple symbol audit`, the coordinator may run scripts to discover pending symbols, create queues, verify entry docs, check formatting, or integrate completed records. These scripts may only inventory, queue, validate, or record reviewed results; they must not bulk-generate health, issues, `Actual Role`, audit rationale, or `agent_audited` status for multiple symbols.
4. Bulk script output, spreadsheet transforms, JSON rewrites, extractor confidence, generated health placeholders, repeated prompts over many symbols, or reused tool-call batches are not symbol health audits. Treat that output as `script_assessed` or planning evidence only.
5. If independent audit agents are unavailable, leave unaudited symbols in `Pending Symbol Audit Slices`, keep requested-scope health audit status `partial`, and state the exact pending symbols. Do not collapse the work into a bulk script audit to claim `current`.
6. A human may explicitly approve a lower-trust triage mode, but triage results remain `script_assessed`, `unaudited`, or otherwise non-closure-eligible until a real per-symbol audit agent or human review completes the controlled workflow.

### Trace Cross-Boundary Causal Flows

Use this when behavior crosses module, process, service, runtime, storage, protocol, or UI boundaries and affects user-visible output, durable state, external integration state, generated artifacts, background status, or operator/admin decisions.

1. Identify candidate flows from producers, boundary crossings, data contracts, persistence points, state mutation points, output surfaces, recovery paths, and verification coverage.
2. For each candidate, choose one disposition:
   - create or update a concise flow doc under `project/flows/`,
   - record it as a pending flow slice in `project/build-plan.md`,
   - mark it out of scope with a reason.
3. For each documented flow, record:
   - the user-visible or externally relied-on outcome,
   - the producer that creates the data, event, state, artifact, or decision input,
   - each meaningful boundary crossing,
   - the transport, protocol, data contract, or file format that carries it,
   - durable or derived state and whether it is source of truth, cache, generated output, telemetry, debug trace, transient runtime state, fixture, or disposable local state,
   - replay, restore, hydration, pagination, or reconstruction behavior when applicable,
   - consumer state and the final output, renderer, exporter, or operator surface,
   - failure, ordering, identity, deduplication, idempotency, and finalization rules when they affect visible or durable behavior,
   - tests, scripts, manual checks, or missing verification,
   - known gaps and confidence.
4. Keep flow docs focused on stable causal chains and contracts. Do not document every helper, local branch, or UI component unless it changes the causal behavior.
5. If the flow cannot be traced from producer through boundary crossings to state and output, keep the flow `partial` and add the missing link to `project/build-plan.md`.

### Coverage Closure Audit

Use this before marking an artifact `current` or treating `project/build-plan.md` as complete.

1. List stable project paths:
   - Use `git ls-files` when the target repository uses git.
   - If git is unavailable, use the best stable source listing available and record that git was unavailable.
2. Group tracked files by stable directory boundary. Do not require file-by-file documentation.
3. Compare those directories against `manifest.yaml` directory mappings, completed slices, out-of-scope dispositions, and documented pending slices.
4. For each unmapped stable tracked path, choose exactly one disposition:
   - add or update a module and directory doc,
   - add it as an actionable pending slice in `project/build-plan.md` and keep coverage `partial`,
   - mark it out of scope with a reason.
5. Review untracked paths from `git status --short` when git exists.
6. Classify untracked paths as artifact output, local runtime state, generated output, or candidate project files.
7. Treat candidate project files as pending until they are mapped, ignored, or explicitly out of scope.
8. Check whether documented user-visible, stateful, generated, integration, background, or event-driven behavior has a traceable flow from producer to boundary crossing, optional durable state, consumer state, and output surface.
9. Run `scripts/inventory_symbols.py` with `--verify-docs`, `--coverage-map-output`, and `--audit-map-output`. Check `directory_summary` for recorded, excluded, and skipped directories; check whether every stable source file has a file-level code doc, symbol inventory, `source_role`, and `audit_scope`; every top-level class, top-level function, and method on every top-level class has a symbol audit map record; every top-level class has an entry doc with `Actual Role` and health fields; every top-level function has an entry doc with `Actual Role` and health fields; every method on every top-level class has an entry doc with `Actual Role` and health fields; `project/coverage-map.json` has no actionable pending, stale, pending_review, not_checked, removed, or candidate project file work for repository coverage; and `project/symbol-audit-map.json` has no required `unaudited` or `audit_expired` symbols in the requested audit scope.
10. Do not set sync or coverage status to `current` while any actionable pending slice remains. Stable tracked paths must be mapped or out of scope; untracked paths must be dispositioned; required flow docs must be completed or out of scope; and every required code symbol doc must be completed or out of scope.
11. Record the audit source, unmapped paths, actionable pending slices, out-of-scope paths, untracked disposition, flow trace disposition, code symbol disposition, symbol audit disposition, undocumented source files, unaudited or audit-expired top-level classes in the requested audit scope, undocumented top-level functions, undocumented class methods, unaudited or audit-expired top-level functions in the requested audit scope, unaudited or audit-expired class methods in the requested audit scope, and criteria to mark `current` in `project/build-plan.md`.

Wide parent directory mappings are useful for navigation but do not by themselves prove closure over important stable subdirectories. If a broad mapping hides a meaningful subsystem, either add a narrower directory entry, add a pending slice, or record why the parent mapping is sufficient.

### Summarize Git History

Use this when a git repository exists and history should be documented.

1. Read recent commits and relevant path history with `git log --stat`, `git log -- <path>`, and `git show --name-status`.
2. Assign commits to directories by touched paths.
3. Assign commits to modules through `manifest.yaml` directory mappings.
4. Assign commits to flows when they alter cross-boundary behavior, persistence, replay, generated output, background status, integrations, or operator/admin surfaces.
5. Assign commits to code symbols when they alter source files, classes, functions, methods, behavior, contracts, health, or tests.
6. Create one change record per meaningful project or code semantic change, such as a feature, fix, refactor, behavior change, API or data contract change, architecture or module-boundary change, test strategy change, or source-level health/risk change.
7. Do not create change records for routine artifact-only sync, formatting, generated index refreshes, link maintenance, or documentation updates that merely mirror an already-recorded project or code change.
8. If an artifact update changes the project knowledge model itself, such as module boundaries, flow traces, coverage status, symbol audit disposition, out-of-scope decisions, or corrected architectural understanding, record that semantic knowledge-model change and explain the evidence.
9. Do not invent intent. Use `confidence: confirmed` only when commit messages, issues, PRs, docs, or code context clearly support the reason. Otherwise use `confidence: inferred` or `confidence: unknown`.

### Update After Verified Change

Use this after verified feature, refactor, or bug-fix work changes behavior, structure, boundaries, dependencies, tests, or known defects. In Maintenance-Aware Fix Mode, this update is part of completion criteria, not an optional follow-up.

1. Update affected module docs.
2. Update affected directory docs.
3. Update affected flow docs when the change affects cross-boundary behavior, data contracts, persistence, replay or hydration, generated output, background status, integrations, consumer state, or output surfaces.
4. Update affected code symbol docs and class, function, or method health when the change touches source files, classes, functions, methods, contracts, side effects, risks, or tests.
5. Add or update change records only when the underlying project, source, tests, architecture, contracts, or knowledge model changed in a way future maintainers need to understand. Do not add records for routine artifact synchronization alone.
6. Add or update decision records when design rationale changed.
7. Update `manifest.yaml`, `INDEX.md`, and any `changes/by-*` index that points to the new records.
8. Update `project/build-plan.md` when the task changes coverage, known gaps, pending flow slices, pending code symbol slices, stale analysis, or next steps.
9. Run the size-check script when available.

## Final Response Checklist

Before finishing a task that used this skill:

- State whether `.doc_project_maintainer/` exists.
- If an audit visualization report was generated, state the HTML path, whether integrity refresh succeeded, and whether any later artifact refresh made the report reflect older data.
- State whether the artifact was updated, was already current for this task's scope, or still needs sync.
- If claiming the artifact is `current`, state the coverage closure audit source or why the audit was skipped.
- State whether affected cross-boundary flow docs were updated, not applicable, or still pending.
- State whether affected code symbol docs and class, function, or method health were updated, not applicable, or still pending.
- For complete or full-repository deliverables, state whether every stable source file has a symbol inventory and whether every top-level class, top-level function, and class method has an entry doc with `Actual Role` and health.
- For complete or full-repository deliverables, summarize `directory_summary`: recorded source directories, excluded directories with reasons, and skipped non-source directories.
- For complete or full-repository deliverables, state the `coverage-map.json` summary, recommended mode, whether any `suggested_slices` remain, and whether any `suggested_audit_slices` remain for the default health audit scope.
- For complete or full-repository deliverables, state the repository-wide `symbol-audit-map.json` summary and the `health_audit_summary`, including unaudited, script_assessed, agent_audited, human_audited, audit_expired, out_of_scope, open issue counts, and the latest audit integrity report's provisional, trusted, suspicious, invalid, closure-eligible, and pending counts for the requested audit scope. `script_assessed` and untrusted `agent_audited` records do not satisfy audit closure.
- If sync is pending, name the affected module, directory, flow, or code symbol docs and the reason.
- If the project is only partially mapped, point to `project/build-plan.md` and summarize the next slice.
- If the task was a full-repository analysis goal, state whether actionable pending slices remain. Do not describe the goal as complete unless none remain.

## Reading Strategy

Use this strategy after preflight determines the task scope. For full-repository analysis goals, complete deliverables, or requests to make the whole artifact current, read the artifact broadly through the index, manifest, build plan, coverage map, audit map, and each required slice. For code changes, feature development, refactors, bug fixes, or scoped maintenance tasks, read only the relevant module, directory, flow, code symbol, change, and decision docs. Start scoped reads with:

1. `.doc_project_maintainer/INDEX.md`
2. `.doc_project_maintainer/manifest.yaml`
3. The target module `README.md`
4. The target directory `README.md`
5. The target code symbol entry doc under `code/` when working on a source file, class, function, or method
6. The relevant linked code symbol detail docs only when the summary is insufficient
7. The relevant flow doc under `project/flows/` when tracing behavior across boundaries
8. Only the linked change or decision records that matter for the current task

If a file exceeds its size budget, split it before adding more content.

## Required References

- Read `references/artifact-structure.md` when creating the artifact, checking size budgets, choosing filenames, or wiring links.
- Read `references/templates.md` when writing `manifest.yaml`, module docs, directory docs, flow docs, change records, or decision records.
- Read `references/code-symbol-docs.md` when creating or updating code symbol docs, class, function, or method entry docs, detail docs, health assessment, or code symbol coverage state.

## Validation

If `.doc_project_maintainer/` exists and `scripts/check_doc_sizes.py` is available, run:

```bash
python <skill-dir>/scripts/check_doc_sizes.py <repo-root>/.doc_project_maintainer
```

Fix any reported oversized file by splitting it and updating parent indexes.

For complete or full-repository deliverables, also run `scripts/inventory_symbols.py` with `--verify-docs`, `--coverage-map-output`, and `--audit-map-output`. Treat any missing docs, missing health, missing `Actual Role`, `requires_review` files, or `unaudited`/`audit_expired` symbols in the requested audit scope as pending work, not success.
