---
name: paper-gen-orchestration
description: Orchestrate the 4-stage paper generation pipeline (conception → planning → experiment → writing). Use when the root Agent of paper-gen-agent is initializing, querying, or routing between stages. Load once at session start.
---

# Paper Generation Orchestration

You are the root Agent of `paper-gen-agent`. This skill is the canonical
reference for stage transitions, REPLAN routing, and pipeline-state
mutation. Load it once per session; do not re-read between stages.

## Canonical invoke envelope

Every `invoke_*_subagent` tool accepts the same envelope:

```json
{
  "run_id": "run_xxx",
  "stage_id": "conception | planning | experiment | writing",
  "run_dir": "<abs path>",
  "input": { ...stage-specific... },
  "previous_stage_outputs": {"conception": "...", "planning": "..."},
  "iteration_context": {"rounds": 0, "max_rounds": 1, "last_feedback": null}
}
```

Every invoke returns:

```json
{
  "status": "completed | replan | revise | aborted | failed",
  "output_dir": "<abs path>",
  "summary": "...",
  "artifacts": {...},
  "next_action": {
    "type": "proceed | replan_to:<stage> | abort",
    "target_stage": "...",
    "reason": "..."
  }
}
```

Each invoke also persists `run_dir/token_usage.json`. Before the final user
response, call `read_token_usage` and report the four modules plus these three
separate totals: `successful_path`, `replan_or_failed`, and `overall_total`.
Do not describe `successful_path` as a completed end-to-end cost unless
`full_pipeline_completed` is true. Never estimate missing provider usage.

## State machine

```
conception --proceed--> planning --proceed--> experiment --proceed--> writing --proceed--> done
    ^                       ^                      ^                      |
    |                       |                      |                      v
    +----- replan_to:X -----+----------------------+                  revise (stay)
                                                                       |
                                                                       v
                                                                   writing again
```

`replan_to:<stage>` can jump back to any prior stage. `revise` retries the
current stage without changing `current_stage`.

## Per-stage handoff contract

Read [references/stage-handoffs.md](references/stage-handoffs.md) for the
exact input/output schema of each stage. The handoff format is stable;
the internal implementation of each sub-agent is not.

## Pipeline state

`run_dir/pipeline_state.json` is the single source of truth. Read it
before any stage transition; update it after any state change. Schema:

```json
{
  "version": "0.2.0",
  "run_id": "...",
  "current_stage": "conception",
  "stages": {
    "conception": {"status": "pending|in_progress|completed|aborted|failed", "rounds": 0, "output_dir": "..."},
    "planning":   {"status": "...", "rounds": 0, "output_dir": "..."},
    "experiment": {"status": "...", "rounds": 0, "output_dir": "..."},
    "writing":    {"status": "...", "rounds": 0, "output_dir": "..."}
  },
  "iteration": {
    "planning_replan_rounds": 0,
    "writing_revise_rounds": 0,
    "max_planning_replan": 2,
    "max_writing_revise": 3
  }
}
```

## REPLAN feedback aggregation (v0.2 minimal)

1. Read `iteration_context.last_feedback` from the invoke result (or
   from `feedback/replan_to_<stage>.json` if the sub-agent wrote one).
2. `update_pipeline_state` to bump `iteration.<stage>_rounds` and stamp
   `last_feedback`.
3. If `rounds > max` → set `current_stage` to the failed stage with
   `status: aborted` and stop. Render a final summary to the user.
4. Otherwise → re-invoke the target stage with the same envelope plus
   `iteration_context.rounds++`.

Semantic merging of multiple feedback entries is **not** required in
v0.2 — concatenate and pass through. Replace with a real aggregator in
v0.3.

## Human review checkpoints

By default the root Agent does **not** call `ask_user` between stages.
The pipeline is fully autonomous from the user's perspective. Set a
checkpoint only when:

- the user explicitly opts in via `quickInput` ("stop at experiment for review")
- an invoke tool returns a result that includes `"require_human_review": true`
  (sub-agents may set this flag in v0.3)
- a hard constraint violation (e.g. compute_estimate > 4× budget) is detected

When a checkpoint fires, use the 4-option template from
plan-supervisor (`approve / modify / abort / details`) and feed the
answer back into the state machine.
