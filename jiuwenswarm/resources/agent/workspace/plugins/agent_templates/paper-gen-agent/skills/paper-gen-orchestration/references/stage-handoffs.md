# Stage Handoff Reference

Canonical input/output schema for each of the 4 stages. The root Agent
reads this once; sub-agents may evolve their internal layout without
affecting the handoff contract.

---

## 1. conception (构思)

**Input** (`input` field of the envelope):
- `research_request`: from `conception-schemas.md` §1.1 (`direction`, `problem_context`, `desired_outcome`, `target_users`, `must_include`, `excluded_scope`)
- `domain`: from §1.2 (`domain_name`)
- `resource_constraints`: from §1.3 (`gpu_type`, `gpu_hours`, `memory_gb`, `budget`, `time_budget_days`)
- `search_constraints`: from §1.4 (`date_from`, `date_to`, `languages`, `sources`, `max_results`)
- `research_preferences`: from §1.5

**Output** (in `run_dir/stage1_conception/`):
- `research_question.json` — §2.1
- `hypotheses.json` — §2.2 (list)
- `gap_report.json` — §2.3
- `key_papers.json` — §2.4 (list)
- `research_frontier.json` — §2.5
- `domain.json` — §1.2
- `status.json` — `{status: "complete" | "replan" | "failed", summary, errors}`

**Canonical envelope return**:
```json
{
  "status": "completed",
  "output_dir": "run_dir/stage1_conception",
  "summary": "5 hypotheses, 8 key papers, gap on memory efficiency",
  "artifacts": {"research_question": "stage1_conception/research_question.json", ...},
  "next_action": {"type": "proceed", "target_stage": "planning", "reason": ""}
}
```

---

## 2. planning (规划)

**Input** (constructed from previous_stage_outputs["conception"]):
- everything under `run_dir/stage1_conception/*.json`
- `resource_constraints` (echoed from original request)

**Output** (in `run_dir/stage2_planning/`) — see plan-supervisor's 5 .json + 5 .md + status.json:
- `method_design.json`
- `experiment_plan.json`
- `data_plan.json`
- `budget_report.json`
- `execution_config.json`
- `status.json` (per plan-supervisor convention)

**Canonical envelope return**:
```json
{
  "status": "completed",
  "output_dir": "run_dir/stage2_planning",
  "summary": "method_design adequacy=8, 3 baselines, 60 GPUh, feasible",
  "artifacts": {"method_design": "...", "experiment_plan": "...", "execution_config": "..."},
  "next_action": {"type": "proceed", "target_stage": "experiment", "reason": ""}
}
```

`replan_to:planning` route writes `feedback/replan_to_planning.json` and
re-invokes with `iteration_context.last_feedback` populated.

---

## 3. experiment (实验)

**Input** (from `previous_stage_outputs["planning"]`):
- `method_design.json`
- `experiment_plan.json`
- `data_plan.json`
- `execution_config.json`

**Output** (in `run_dir/stage3_experiment/`) — see experiment-agent's contract:
- `agent-state.json` (state machine)
- `experiment-module-output.json` (canonical Module-3 deliverable)
- `artifact-manifest.json` (list of all produced files)
- raw/aggregated data and candidate figures (under sub-directories)

**Canonical envelope return**:
```json
{
  "status": "completed",
  "output_dir": "run_dir/stage3_experiment",
  "summary": "exp1/exp2/exp3 complete; primary metric +12% over baseline",
  "artifacts": {
    "module_output": "stage3_experiment/experiment-module-output.json",
    "manifest": "stage3_experiment/artifact-manifest.json"
  },
  "next_action": {"type": "proceed", "target_stage": "writing", "reason": ""}
}
```

`replan_to:planning` is the typical failure route: the experiment-agent
returns `{status: REPLAN, reason: "<category>"}` and the root Agent
maps it to `next_action.type = "replan_to:planning"`.

---

## 4. writing (写作)

**Input** (from previous_stage_outputs):
- `conception`: research_question / hypotheses / gap_report / key_papers / frontier
- `planning`: method_design / experiment_plan
- `experiment`: experiment-module-output.json

**Output** (in `run_dir/stage4_writing/`):
- `paper.pdf` (canonical deliverable)
- `latex_source.tex` (optional, for debugging)
- `writing_output.json` — `{status, pdf_path, latex_source, sections, review_history}` per `writing-schemas.md` §2.1
- `review_history` — list of `{round, score, issues, resolved}`

**Canonical envelope return**:
```json
{
  "status": "completed",
  "output_dir": "run_dir/stage4_writing",
  "summary": "ICLR 2024 PDF generated; review score 8.5/10",
  "artifacts": {
    "pdf_path": "run_dir/stage4_writing/paper.pdf",
    "writing_output": "run_dir/stage4_writing/writing_output.json"
  },
  "next_action": {"type": "proceed", "target_stage": "done", "reason": ""}
}
```

`revise` is the typical failure route: the writing-agent returns
`{status: WRITING_REVISE, review_score: 6.5}` and the root Agent maps
it to `next_action.type = "revise"`, staying on the writing stage.

---

## REPLAN target rules

| Source stage | `next_action.target_stage` | When |
|---|---|---|
| conception | `planning` | rare; only if conception discovers the request is unanswerable |
| planning | `planning` | method/baseline/metric/compute issue |
| experiment | `planning` | compute/baseline/metric failure |
| experiment | `conception` | hypothesis unsound |
| writing | `planning` | missing method evidence |
| writing | `experiment` | results don't support claims |
| writing | `writing` (revise) | score < threshold, fixable in writing |
