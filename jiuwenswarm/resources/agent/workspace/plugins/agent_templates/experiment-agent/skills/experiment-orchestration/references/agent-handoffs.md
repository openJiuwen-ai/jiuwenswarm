# Agent hand-off contract

## State order

`INITIALIZED → DATA_READY → CODE_REVIEW_REQUIRED → EXECUTION_REVIEW_REQUIRED → IMPLEMENTATION_READY → EXECUTION_READY → COMPLETED`

Any non-final stage may instead end at `REPLAN` or `FAILED` where permitted by the backend. The four direct subagents are one level deep and must never invoke one another.

## Root initialization response

Required fields: `run_id`, `run_dir`, `stage`, `terminal`, `next_agent`, `summary`, `output_path`, `artifact_manifest_path`, `warnings`.

## Child inputs

Every child receives:

```json
{
  "run_dir": "absolute-or-workspace-relative run directory",
  "run_id": "same immutable run identifier"
}
```

The data child may additionally receive `allow_downloads` and `max_download_gb`. `allow_downloads` defaults to `true` without another confirmation; pass `false` only for an explicit offline run. The deterministic data tool prefers the Module 2 URL and otherwise searches public registries for a unique exact-name match. Ambiguous or failed resolution returns `REPLAN` with the search evidence and the exact `run_dir`-relative directory for manual data placement.

## Child outputs

- Data: stage, terminal, prepared dataset count, dataset index path, summary, warnings.
- Implementation: read-only inspection payload/path, stage, terminal, execution task count/path, implementation review path, review phase, approval-required flag, summary, warnings.
- Execution: stage, terminal, successful/failed run counts, runtime-results path, summary, warnings.
- Analysis: stage, terminal, module status, final output path, completed artifact-manifest path, candidate-chart count, summary, warnings.

All child responses include `run_id`, canonical `run_dir`, and `consumer`. Never change either run identity field.

The Agent tool accepts paths only inside the current trusted workspace. A stage verifies the SHA-256 digest of the previous stage artifact before consuming it. Dataset fingerprints are verified both before and after experiment commands run.

At `CODE_REVIEW_REQUIRED`, the consumer is the root `experiment-agent` acting as an isolated reviewer. It receives only plan intent, source code and SHA-256, source/revision/license, exact dependencies and commands, data scope, resources, static findings and risks. `APPROVE_SMOKE` only authorizes the backend to run a maximum-60-second bounded smoke test after deterministic gates pass.

At `EXECUTION_REVIEW_REQUIRED`, the root reviewer additionally receives real smoke logs, metrics and their SHA-256. Only `APPROVE_EXECUTION` plus deterministic verification causes the backend to write `execution_approved=true`. Any code, command, dependency, manifest or smoke-evidence change invalidates both reviews and returns the run to `CODE_REVIEW_REQUIRED`.

## Module 4 hand-off

On `COMPLETED`, Module 4 receives the final output JSON. Inside it, visualization candidates point to renderings plus raw/aggregated data and generation metadata. The standard companion files are:

- `outputs/experiment-module-output.json`
- `outputs/artifact-manifest.json`
- `visualization/raw_metrics.csv`
- `visualization/aggregated_metrics.csv`
- paths named by `visualization_candidates`, `table_artifacts`, and `figure_artifacts`

Module 4 may select, combine, or redraw candidates. It must not silently alter the underlying metric values, and should verify every companion file against `artifact-manifest.json` before use.

## Return to Module 2

On `REPLAN`, read `planning_feedback` from the final output and return its reason, blockers, affected experiment IDs, and suggested changes. No scientific finding is valid in this state.
