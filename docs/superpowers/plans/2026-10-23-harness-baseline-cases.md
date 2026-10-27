# DeepAgent Baseline Cases Implementation Plan

> For agentic workers: execute inline with the executing-plans skill; preserve existing worktrees and unrelated changes. No commits, deployments, service restarts or model runs are part of this task.

**Goal:** Create small, reproducible cases that measure observed native DeepAgent Harness weaknesses using deterministic acceptance, with separate training, holdout and benign controls.

**Architecture:** A standalone benchmark package owns public fixtures and its EvaluationJudger. It reuses native Solver, CaseRunner, TeamEvaluator and single-Harness orchestration via a local entry point. It does not change product scoring or expose private grading code as case assets. The Web script_based option does not execute arbitrary verifiers; document this limitation explicitly.

**Constraints:** Chinese task instructions; Python 3.11+ standard library for case tools and acceptance; no network/GPU dependencies; no secrets; Windows-compatible paths and subprocess calls; preserve 300-step host defaults; no formatter/static-analysis tools. Do not claim empirical baseline weaknesses until model calibration is run.

## Tasks

- [x] 1. Write a meaningful contract check before benchmark implementation. Cover good outputs, missing outputs, corrupted receipts, preserved-file changes, wrong checker acceptance, and control cases.
- [x] 2. Generate 24 cases: two training, one holdout and one control for each of delivery, pipeline, oracle, regression, schema feedback and log evidence. Materialize all declared public assets and content identities.
- [x] 3. Implement deterministic per-requirement grading and trusted bounded subprocess checks. Preserve valid partial evidence; final case pass requires all published requirements. Add content/schema/version identities to invalidate incompatible cached results.
- [x] 4. Implement offline validation, baseline evaluation, optimization and separate holdout evaluation entry points. Verify the custom judger is used in serial and concurrent evaluator construction. Never silently fall back to LLM grading.
- [x] 5. Verify generated fixtures, good/bad acceptance discrimination, model-free native evaluator integration and CLI help. Document cases, weakness evidence, allowed optimization surfaces, initial calibration and current integration limits.

## Decisions

- Use existing selected worktrees; all new implementation lives under benchmarks/harness_baseline_v1.
- Run only model-free benchmark contract checks. No API spend or new RSI task is authorized by dataset construction alone.
- Return binary case pass plus deterministic per-requirement partial progress. Report efficiency separately rather than failing a correct solution solely for a tool count.
- Do not automatically repair known unrelated RSI publication/Repair bugs in this task.

## Verification ledger

- Model-free contract checks: 160 passed; 24 cases; no model calls.
- Native TeamEvaluator/CaseRunner serial and parallel adapter exercised with reference-output fake backends; not a Solver baseline result.
- Native loader validate CLI passed: train 12, holdout 6, controls 6.
- Existing task profile, custom deterministic evaluator, 300-step policy and empty baseline package parsed successfully without calling models.
- Run-input freezing and per-experiment mh layout checked without model calls.
- Independent read-only review completed; fixed interface execution evidence, shell separators, clipped-evidence classification, oracle holdout variation, public delivery fields, relative Harness refs and concurrency propagation.
- No model calibration/optimization run, service restart, dependency change, commit or push. Existing unrelated modifications preserved.
