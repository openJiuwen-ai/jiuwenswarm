# Project Maintainer Templates

Use these templates as compact starting points. Remove empty sections instead of filling space.

## Root README

```markdown
# Project Maintainer Docs

Status: initialized | partial | current
Last updated: YYYY-MM-DD
Sync status: current | partial | stale | unknown

## Project Brief

- Name:
- Purpose:
- Primary users:
- Main runtime:
- Tech stack:

## How To Read

Start with `INDEX.md`, then `manifest.yaml`, then the module, directory, code symbol, or flow relevant to the task.

## Maintenance Rules

- Keep files within the size budgets in `references/artifact-structure.md`.
- Use Knowledge Base Delivery Mode when `.doc_project_maintainer/` is the requested deliverable. Use Maintenance-Aware Fix Mode when Project Maintainer is requested during a bug fix, feature change, or refactor.
- Update module docs, directory docs, flow docs, changes, and decisions after project or code behavior, structure, contract, test strategy, health, or knowledge-model changes.
- Do not create change records for routine artifact-only sync, formatting, generated index refreshes, link maintenance, or documentation updates that merely mirror an already-recorded project or code change.
- Update code symbol docs and class, function, or method health after source behavior, contracts, side effects, risks, or tests change.
- Check existing artifact sync status before relying on it.
- In Maintenance-Aware Fix Mode, Project Maintainer must not be the primary debugging workflow: check whether `.doc_project_maintainer/` exists, ask the user whether to analyze first when relevant coverage is missing, use the external debugging or development workflow for the fix, then synchronize affected artifact slices after verification because artifact maintenance becomes part of the task completion criteria.
- Do not mark the artifact `current` without a recorded coverage closure audit and no actionable pending slices.
- Do not mark cross-boundary behavior `current` unless required flows are documented or out of scope.
- Do not mark repository code symbol coverage `current` unless every stable source file has a symbol inventory, every top-level class, top-level function, and class method has a symbol audit map record, and every top-level class, top-level function, and class method has an entry doc with `Actual Role` and health.
- Do not mark default product/runtime health audit `current` unless every `default_health_audit` top-level class, top-level function, and class method is audited or out of scope in `symbol-audit-map.json`.
- Treat `audit.status: script_assessed` as script progress only. It does not satisfy trusted agent, human, or out-of-scope audit closure and must remain pending until real agent or human review is promoted through `scripts/audit_integrity.py` and verified as `trusted_agent_audit`.
- For a `single symbol audit`, the current agent must read the symbol implementation, needed callers or callees, and related test evidence before recording health. For a `multiple symbol audit`, use one audit agent per required symbol; scripts may only inventory, queue, validate, or record reviewed results and must not bulk-generate health.
- Mark uncertain claims with `confidence: inferred` or `confidence: unknown`.
```

## Manifest

```yaml
version: "0.0.1"
artifact_root: ".doc_project_maintainer"
last_updated: "YYYY-MM-DD"

sync:
  status: "unknown"
  last_scanned_commit: null
  last_synced_commit: null
  coverage_closure:
    status: "not_run" # not_run | partial | passed | skipped
    source: null # git ls-files | filesystem listing | skipped
    last_audited: null
    tracked_paths: "not_audited" # not_audited | mapped_or_dispositioned
    untracked_paths: "not_audited" # not_audited | classified | not_applicable
    flow_traces: "not_audited" # not_audited | documented_or_dispositioned
    code_symbols: "not_audited" # not_audited | every_required_symbol_documented
    symbol_audits: "not_audited" # not_audited | every_required_symbol_audited
    notes: "Do not mark current until stable tracked paths and required flows are mapped or out of scope, every top-level class, top-level function, and class method has Actual Role plus health, every requested-scope health audit symbol is audited or out of scope, and no actionable pending slices remain."
  pending:
    - id: "SYNC-YYYYMMDD-001"
      reason: "Docs may not reflect recent changes."
      paths:
        - "path/to/directory"
      modules:
        - "module-id"
      flows: []
      code_symbols: []
      created: "YYYY-MM-DD"

project:
  name: ""
  summary: ""
  docs:
    overview: "project/overview.md"
    architecture: "project/architecture.md"
    glossary: "project/glossary.md"
    build_plan: "project/build-plan.md"
    source_symbol_inventory: "project/source-symbol-inventory.json"
    coverage_map: "project/coverage-map.json"
    symbol_audit_map: "project/symbol-audit-map.json"
    open_questions: "project/open-questions.md"
    flows_dir: "project/flows"
    code_dir: "code"

modules:
  - id: "module-id"
    name: "Module Name"
    summary: "Module responsibility."
    confidence: "confirmed"
    docs:
      readme: "modules/module-id/README.md"
      design: "modules/module-id/design.md"
      directories: "modules/module-id/directories.md"
      changes: "modules/module-id/changes.md"
      git_history: "modules/module-id/git-history.md"
    directories:
      - "path/to/directory"
      - "tests/module-id"
    related_decisions: []
    related_flows: []
    related_code_symbols: []
    recent_changes: []
    coverage:
      status: "partial"
      last_scanned: "YYYY-MM-DD"
      notes: "Entrypoints inspected; related flows still pending."

directories:
  - path: "path/to/directory"
    encoded: "path__to__directory"
    summary: "Directory responsibility."
    modules:
      - "module-id"
    doc: "directories/path__to__directory/README.md"
    related_changes: []
    related_decisions: []
    related_flows: []
    related_code_symbols: []
    coverage:
      status: "partial"
      last_scanned: "YYYY-MM-DD"

flows:
  - id: "flow-id"
    name: "Flow Name"
    summary: "Cross-boundary behavior that produces or restores relied-on state or output."
    confidence: "inferred"
    doc: "project/flows/flow-id.md"
    modules: []
    directories: []
    entrypoints: []
    source_of_truth: []
    related_changes: []
    related_decisions: []
    code_symbols: []
    coverage:
      status: "partial"
      last_scanned: "YYYY-MM-DD"

code_symbols:
  - symbol: "A.a"
    kind: "method"
    source: "src/foo/a.py"
    source_role: "runtime_source"
    audit_scope: "default_health_audit"
    doc: "code/src/foo/a.py/Class A/A.a.md"
    detail_dir: "code/src/foo/a.py/Class A/A.a"
    modules:
      - "module-id"
    directories:
      - "src/foo"
    flows: []
    health:
      overall: "watch"
      name_behavior_match: "partial"
      responsibility_focus: "mixed"
      length: "medium"
      complexity: "medium"
      implementation_soundness: "partial"
      input_contract: "implicit"
      output_contract: "clear"
      boundary_safety: "partial"
      side_effects: "explicit"
      error_handling: "partial"
      state_mutation: "shared"
      dependency_coupling: "medium"
      test_coverage: "partial"
      observability: "not_applicable"
      performance_risk: "low"
    audit:
      status: "unaudited" # unaudited | script_assessed | agent_audited | human_audited | audit_expired | out_of_scope
      auditor: null
      audited_at: null
      audited_commit: null
      audited_source_hash: null
      audited_symbol_hash: null
      confidence: "inferred"
      expired_reason: null
    issues: []
    coverage:
      status: "partial"
      last_scanned: "YYYY-MM-DD"
    confidence: "inferred"

code_symbol_inventory:
  generated_by: "scripts/inventory_symbols.py"
  source_symbol_inventory: "project/source-symbol-inventory.json"
  coverage_map: "project/coverage-map.json"
  symbol_audit_map: "project/symbol-audit-map.json"
  extractor_priority:
    - "python_ast"
    - "ctags"
    - "heuristic"
  requires_review_files: 0
  missing_file_docs: 0
  missing_entry_docs: 0
  missing_actual_role: 0
  missing_health: 0
  source_files:
    total: 0
    documented: 0
    pending: 0
    out_of_scope: 0
  directory_summary:
    recorded_directories: 0
    excluded_directories: 0
    skipped_non_source_directories: 0
  source_roles: {}
  audit_scopes: {}
  default_health_audit_source_files: 0
  repository_coverage_only_source_files: 0
  top_level_functions:
    total: 0
    documented: 0
    pending: 0
    out_of_scope: 0
  class_methods:
    total: 0
    documented: 0
    pending: 0
    out_of_scope: 0

coverage_map:
  generated_by: "scripts/inventory_symbols.py"
  path: "project/coverage-map.json"
  git_head: null
  git_status: "not_checked"
  recommended_mode: "single-agent" # single-agent | multi-agent
  pending_files: 0
  stale_files: 0
  pending_review_files: 0
  candidate_project_files: 0
  directory_summary:
    recorded_directories: 0
    excluded_directories: 0
    skipped_non_source_directories: 0
  default_health_audit_required_symbols: 0
  repository_coverage_only_required_symbols: 0
  suggested_slices: []
  suggested_audit_slices: []

symbol_audit_map:
  generated_by: "scripts/inventory_symbols.py"
  path: "project/symbol-audit-map.json"
  source_role_summary: {}
  audit_scope_summary: {}
  audit_statuses:
    unaudited: 0
    script_assessed: 0
    agent_audited: 0
    human_audited: 0
    audit_expired: 0
    out_of_scope: 0
  health_audit_summary:
    unaudited: 0
    script_assessed: 0
    agent_audited: 0
    human_audited: 0
    audit_expired: 0
    out_of_scope: 0
  audit_integrity_summary:
    provisional_agent_audit: 0
    trusted_agent_audit: 0
    suspicious_agent_audit: 0
    invalid_agent_audit: 0
    closure_eligible: 0
    pending: 0
  health_dimensions:
    - "overall"
    - "name_behavior_match"
    - "responsibility_focus"
    - "length"
    - "complexity"
    - "implementation_soundness"
    - "input_contract"
    - "output_contract"
    - "boundary_safety"
    - "side_effects"
    - "state_mutation"
    - "error_handling"
    - "dependency_coupling"
    - "test_coverage"
    - "observability"
    - "performance_risk"
  open_issues: 0
```

## Build Plan

```markdown
---
last_updated: YYYY-MM-DD
sync_status: current | partial | stale | unknown
coverage_status: planned | partial | current
flow_coverage_status: planned | partial | current
code_symbol_coverage_status: planned | partial | current
---

# Build Plan

## Current State

- Artifact status:
- Trusted coverage:
- Known stale areas:
- Known incomplete areas:
- Known incomplete flow slices:
- Known incomplete code symbol slices:
- Actionable pending slices that keep coverage partial:
- Every stable source file inventoried: yes | no
- Every top-level class documented with `Actual Role` and health: yes | no
- Every top-level function documented with `Actual Role` and health: yes | no
- Every class method documented with `Actual Role` and health: yes | no
- Every top-level class, top-level function, and class method audited or out of scope: yes | no
- Inventory command:
- Inventory extractor summary:
- Coverage map:
- Coverage map recommended mode:
- Symbol audit map:
- Symbol audit status counts:
- Default health audit status counts:
- Machine assessment status counts:
- Default health audit machine assessment counts:
- Directory summary:
- Open symbol issues:
- Git head inventoried:
- Dirty worktree state:
- Stale files:
- Untracked candidate project files:
- Files requiring manual review:
- Missing file docs:
- Missing entry docs:
- Missing `Actual Role`:
- Missing health fields:

## Completed Slices

- YYYY-MM-DD: `slice-id` - module, directory, or flow slice built and confidence

## Pending Slices

- `module-or-path`: why it remains and suggested next step

Pending slices mean coverage remains `partial` until the slice is completed or explicitly marked out of scope.

## Pending Flow Slices

- `flow-id`: required outcome or state, missing trace link, and suggested next step

Pending flow slices mean flow coverage remains `partial` until the flow is completed or explicitly marked out of scope.

## Pending Code Symbol Slices

- `source-or-symbol`: missing stable source file inventory, top-level class entry doc, top-level function entry doc, class method entry doc, health, or detail coverage and suggested next step

Pending code symbol slices mean code symbol coverage remains `partial` until the slice is completed or explicitly marked out of scope.

## Pending Symbol Audit Slices

- `symbol-id`: `unaudited`, `script_assessed`, `audit_expired`, suspicious, invalid, or untrusted class, top-level function, or class method in the requested audit scope; required reviewer type; whether a real audit agent or human has been assigned; health dimensions to verify; implementation, caller/callee, test, and flow evidence to inspect; open issue follow-up

Pending symbol audit slices mean requested-scope health audit remains `partial` until each required symbol is `human_audited`, explicitly `out_of_scope`, or `agent_audited` with a latest `trusted_agent_audit` verification result. `scripts/inventory_symbols.py` is not an auditor; script output, generated docs, extractor confidence, placeholder health fields, and `audit.status: script_assessed` must remain pending until a real audit agent has read the symbol implementation, recorded evidence-based conclusions, and promoted the result through `scripts/audit_integrity.py`.

For a `single symbol audit`, the assigned agent records evidence and promotes only that symbol. For a `multiple symbol audit`, assign one audit agent per required symbol by default. If independent audit agents are unavailable, leave the remaining symbols here as pending instead of bulk-generating health through a script.

## Suggested Subagent Queue

- `slice-id`: files, symbols, status, source roles, audit scopes, assigned agent, blocker, and integration state from `project/coverage-map.json`

Use `suggested_slices` for repository coverage and `suggested_audit_slices` for default product/runtime health audit when the coverage map recommends `multi-agent`. The coordinator owns assignment, merge, manifest/index updates, and rerunning the inventory command after each integrated batch. Expand `suggested_audit_slices` into symbol-level audit assignments before closure work so each closure-eligible agent audit comes from its own symbol review.

## Coverage Closure Audit

- Audit source:
- Tracked path audit:
- Unmapped stable tracked paths:
- Recorded source directories:
- Excluded directories and reasons:
- Skipped non-source directories:
- Actionable pending slices:
- Out-of-scope tracked paths:
- Untracked path disposition:
- Generated/local/runtime exclusions:
- Flow trace disposition:
- Code symbol disposition:
- Symbol audit disposition:
- Undocumented source files:
- Unaudited or audit-expired top-level classes:
- Unaudited or audit-expired top-level classes in requested audit scope:
- Undocumented top-level functions:
- Undocumented class methods:
- Unaudited or audit-expired top-level functions:
- Unaudited or audit-expired top-level functions in requested audit scope:
- Unaudited or audit-expired class methods:
- Unaudited or audit-expired class methods in requested audit scope:
- Inventory extractor and confidence disposition:
- Criteria to mark `current`:

## Multi-Agent Notes

- Slice boundaries:
- Integration owner:
- Files that require central merge:

## Open Questions

- Question:
```

## Module README

```markdown
---
id: module-id
name: Module Name
confidence: confirmed | inferred | unknown
last_updated: YYYY-MM-DD
read_when: "Working on this module or one of its mapped directories."
---

# Module Name

## Responsibility

One short paragraph.

## Boundaries

- Owns:
- Does not own:

## Entry Points

- `path/to/file`: why it matters

## Directory Map

See `directories.md`.

## Related Changes

See `changes.md`.

## Related Decisions

- ADR-NNNN: title

## Related Flows

- `flow-id`: why it matters

## Related Code Symbols

- `symbol-id`: why it matters
```

## Directory README

```markdown
---
path: path/to/directory
encoded: path__to__directory
modules:
  - module-id
confidence: confirmed | inferred | unknown
last_updated: YYYY-MM-DD
read_when: "Editing files under this directory or tracing its ownership."
---

# `path/to/directory`

## Purpose

One short paragraph.

## Important Files

- `file.py`: role

## Module Links

See `module-links.md`.

## Related Changes

See `changes.md`.

## Related Flows

- `flow-id`: why it matters.

## Related Code Symbols

- `symbol-id`: why it matters.
```

## Flow Doc

```markdown
---
id: flow-id
name: Flow Name
status: partial
confidence: inferred
last_updated: YYYY-MM-DD
user_visible_surface:
source_of_truth: []
modules: []
directories: []
code_symbols: []
entrypoints: []
---

# Flow Name

## Outcome

What users, operators, integrations, or future agents rely on.

## Causal Path

Producer -> boundary crossings -> transport or contract -> consumer state -> output surface.

## State Classification

Files, databases, caches, generated artifacts, telemetry, debug traces, fixtures, transient runtime state, or disposable local state touched by the flow. State which item is source of truth when one exists.

## Replay, Restore, Or Reconstruction

How state or output is reconstructed after reload, restart, pagination, retry, rebuild, or recovery.

## Contract

Stable methods, message types, file formats, schemas, payload fields, or compatibility layers.

## Consumer State And Output

State mutations, derived state, renderers, exporters, operator surfaces, or integration outputs.

## Failure, Ordering, And Identity

Errors, retries, cancellation, ordering guarantees, deduplication keys, idempotency, and finalization rules when they affect visible or durable behavior.

## Verification

Relevant tests, scripts, manual checks, or missing coverage.

## Known Gaps

Open questions and uncertain links.
```

## Change Record

```markdown
---
id: CHG-YYYYMMDD-NNN
title: Short semantic title
type: feature | fix | refactor | docs | test | chore # docs/chore must still describe a meaningful project, code, test, or knowledge-model change.
date: YYYY-MM-DD
modules:
  - module-id
directories:
  - path/to/directory
flows:
  - flow-id
code_symbols:
  - symbol-id
decisions:
  - ADR-NNNN
commits:
  - abc1234
confidence: confirmed | inferred | unknown
---

# Short Semantic Title

## What Changed

Brief project or code semantic change. Do not describe routine artifact sync unless the artifact update changed the project knowledge model, such as module boundaries, flow traces, coverage status, symbol audit disposition, out-of-scope decisions, or corrected architectural understanding.

## Why

Use evidence. Mark uncertainty explicitly.

## Impact

- User-visible:
- Internal:
- Tests:
```

## Decision Record

```markdown
---
id: ADR-NNNN
title: Short decision title
status: proposed | accepted | superseded | deprecated
date: YYYY-MM-DD
modules:
  - module-id
directories:
  - path/to/directory
flows:
  - flow-id
code_symbols:
  - symbol-id
related_changes:
  - CHG-YYYYMMDD-NNN
confidence: confirmed | inferred | unknown
---

# ADR-NNNN: Short Decision Title

## Context

What forced this decision.

## Decision

What was chosen.

## Consequences

- Positive:
- Negative:
- Follow-up:
```

## Code Symbol Docs

Use `references/code-symbol-docs.md` for file, class, function, method, detail, and health templates. Keep code symbol entry docs small: actual role, key signals, and detail index only.
