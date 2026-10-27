# Harness continuous scoring implementation plan

**Goal:** Implement per-target continuous improvement > 0.05, one full rerun with per-case retention and positive mean gain, and baseline-aware candidate judging.

**Spec:** ../specs/2026-10-23-harness-continuous-promotion-design.md

**Architecture:** Add a versioned opt-in policy for new Swarm tasks. Preserve legacy runs. Share immutable per-case Judge context, and keep the existing optimizer, diagnosis and engineering gates.

## Constraints and rulings

- Use the existing Swarm 0.2.8-2 and agent-core release-v0.1.20 worktrees; preserve their pre-existing changes.
- No commits, service restarts or real model experiments are requested.
- Ruling: Do not add/run tests or static checks: the current developer instructions require an explicit user request for tests and prohibit static tools. Use source inspection and a fresh code reviewer; report the verification limit.
- Ruling: Preserve Behavior Check acceptance requirements. Downgrading its veto was an optional recommendation, not one of the user's two requested changes.
- First-gain threshold is 0.05; later same-version confirmation requires score strictly above its frozen baseline. No negative tolerance.
- Judge baseline information must never enter Solver inputs or leak the candidate's previous high score into confirmation.

## Original implementation tasks (superseded by the revision below)

- [x] 1. Add versioned policy and continuous score output; keep legacy threshold scoring.
- [x] 2. Add immutable baseline Judge context and evidence snapshots; include context in cache identity and all Judge paths.
- [x] 3. Implement complete-candidate decisions, target result reuse, bounded confirmation and durable resume.
- [x] 4. Wire Swarm defaults, reports and publication identity; document behavior in Chinese and English.
- [x] 5. Review changed source and obtain one independent read-only review; resolve material findings in source.

## Interfaces

- `PromotionConfig`: policy version, min_improvement and one confirmation.
- `BaselineJudgeContext`: frozen source eval, same-case task identity, per-case result and evidence fingerprint.
- `TeamEvaluator.evaluate_batch(..., baseline_contexts=...)` passes the context to `CaseRunner.execute`, then to the LLM judger only.
- Continuous orchestration stores baseline/first/latest scores independently and binds proof to full package, case set and baseline context identity.

## Review focus

- Same-package confirmation must actually rerun Solver and Judge once, while crash recovery resumes that same trial.
- Target scores must not become a full-dataset improvement proof.
- Missing evidence must not silently turn into a baseline score of zero or a context-free grade.
- Legacy config/state/cache must not be interpreted as continuous.
- Publication must match the official confirmed package, not any locally accepted gate.

## Progress

### Revision: authoritative two-stage requirements

User source: `/Users/jiuwen/.codex/worktrees/9bdc/0.2.8-2/docs/zh/Harness两项修改说明.md`.
This source supersedes the earlier first-full/extra-confirmation and high-water baseline rules below.

- [x] Target precheck: every target score strictly exceeds its own frozen baseline + 0.05.
- [x] Rerun every case once in the full stage; do not reuse target precheck results or add a third stage.
- [x] Full stage: every target > baseline; every non-target >= baseline - 0.05; full mean > baseline mean.
- [x] After promotion use this full evaluation's score vector as the next candidate's baseline.
- [x] Introduce continuous_two_stage_v2, remove confirmation_runs, update publication and UI metadata.
- [x] Obtain one independent read-only review and resolve material findings.

No tests, builds, static checks, model experiments, commits or service restarts are authorized in this revision. Existing workspace changes and the baseline-aware Judge evidence path are preserved.

Revision review: an independent read-only reviewer reported no substantive P1/P2 findings after inspecting per-case boundaries, two-stage frozen baselines, full reruns, trial/cache identity, legacy-policy separation, baseline updates, publication/installation and UI wording. This is source review, not runtime verification. No tests, builds, static checks or model calls were executed.

- Workspace and existing diffs inspected; implementation started.
- Legacy restoration retains its original binary contract identifier and prompt; the new score_mode field is excluded from legacy cache identities. Changing to continuous policy still requires a new task.
- Review fixes: separate continuous numerical acceptance from the legacy passed flag; validate target provenance before completing a full trial; index full trials by content rather than candidate directories; verify baseline files against the original judged-artifact manifest.
- Source inspection completed for the new scoring and context interfaces. Runtime verification has not been requested or performed.
- Independent review returned four actionable findings: legacy passed gating, target provenance, repeated same-content confirmation, and legacy protocol fingerprints. The corresponding code paths were changed and inspected; no tests/build/static tools were run, so these fixes have not been demonstrated at runtime.
- No service restart, frontend build, commits, dependency switch or real-model runs were performed. Existing worktree changes were preserved.
