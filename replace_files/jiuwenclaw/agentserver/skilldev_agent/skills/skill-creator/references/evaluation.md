# Evaluation Workflow

Start here after the skill-creator workflow has entered evaluation mode.

The evaluation flow:

1. Clarify the evaluation scope if the requested target, baseline, or success criteria are ambiguous.
2. Visibly propose test cases and expectations.
3. First show the proposed cases visibly to the user, then immediately save them to `<workspace>/evals/evals.json`. Between showing the cases and starting the eval runs, do not ask the user any question or wait for user input.
4. Run each proposed case with the skill and with a baseline.
5. Grade all runs with the `grader` subagent.
6. Aggregate results with `scripts.aggregate_benchmark`.
7. Present the full benchmark results.
8. If changes are needed, improve the skill and rerun into the next iteration.

Hard checkpoints:

- Do not hide generated eval cases in thinking or tool-only output. The user must see them before any eval run starts.
- After showing generated eval cases, do not call `ask_user_question`, add a visible question, or wait for user input. The visible case display must not include extra text asking for confirmation, approval, preferences, revisions, or permission to continue. Save the cases and start the eval runs immediately.
- Do not proceed with missing executor artifacts.
- Do not present results until every run has `grading.json` and the iteration has `benchmark.md`.
- Do not fabricate scores, transcripts, metrics, or benchmark output.

---

## 1. Propose Test Cases

Generate 2-3 realistic test prompts, each with objectively verifiable expectations, and show them in the visible assistant message. Present prompts and expectations together. Do not include any extra text that asks the user for confirmation, approval, preferences, revisions, or permission to continue, and do not wait for user input after displaying the cases. Save them immediately to `<workspace>/evals/evals.json` using the schema in `references/schemas.md`, then run the evals.

Test types worth covering:

- `smoke` - minimal input works.
- `happy_path` - real user flow.
- `edge_case` - boundary or error input.
- `integration` - multi-step end-to-end behavior.

---

## 2. Run The Evals

Every proposed test case needs **both** a with-skill run and a baseline:

- **New skill:** compare with-skill against no-skill.
- **Improving a skill:** snapshot the old skill first and compare new skill against the old-skill snapshot. Do not run a no-skill baseline for improvement work.

Never fabricate. Put all evaluation artifacts under the current workspace's `evals/` directory using exactly this layout:

```text
<workspace>/evals/
├── evals.json
└── iteration-<N>/
    ├── benchmark.json
    ├── benchmark.md
    └── eval-<N>/
        ├── eval_metadata.json
        ├── with_skill/
        │   ├── transcript.md
        │   ├── grading.json
        │   └── outputs/
        │       └── metrics.json
        └── without_skill/ or old_skill/
            ├── transcript.md
            ├── grading.json
            └── outputs/
                └── metrics.json
```

### Execution Via task_tool

Use `task_tool` to delegate runs and grading to dedicated subagents. Two subagent types are available:

- `skill_executor` - runs a single test case, with or without a skill loaded.
- `grader` - evaluates execution results against expectations and produces `grading.json`.

Always use absolute paths in `task_description`. Subagents do not inherit your system prompt and don't know the workspace; every input/output path you reference must be absolute.

The same rule applies to any subagent you spawn outside the eval flow.

---

## 3. Prepare Metadata, Then Run All Configs In Parallel

Process each test case in parallel.

Write `eval_metadata.json` to `<workspace>/evals/iteration-<N>/eval-<N>/eval_metadata.json`. Copy the expectations from `<workspace>/evals/evals.json`; do not re-draft.

```json
{
  "eval_id": 0,
  "eval_name": "descriptive-name-here",
  "prompt": "The user's task prompt",
  "expectations": []
}
```

Run with-skill via `task_tool`:

```text
task_tool(
  subagent_type="skill_executor",
  task_description="workspace: <workspace>\nrun_dir: <workspace>/evals/iteration-<N>/eval-<N>/with_skill\noutput_dir: <workspace>/evals/iteration-<N>/eval-<N>/with_skill/outputs\ntranscript_path: <workspace>/evals/iteration-<N>/eval-<N>/with_skill/transcript.md\nmetrics_path: <workspace>/evals/iteration-<N>/eval-<N>/with_skill/outputs/metrics.json\nprompt: <task prompt>\nskill_path: <workspace>/skill/<skill-name>\ninput_files: [<workspace>/path/to/input, ...]"
)
```

Run baseline via `task_tool(subagent_type="skill_executor")` with the same artifact contract.

For a new-skill baseline:

```text
task_tool(
  subagent_type="skill_executor",
  task_description="workspace: <workspace>\nrun_dir: <workspace>/evals/iteration-<N>/eval-<N>/without_skill\noutput_dir: <workspace>/evals/iteration-<N>/eval-<N>/without_skill/outputs\ntranscript_path: <workspace>/evals/iteration-<N>/eval-<N>/without_skill/transcript.md\nmetrics_path: <workspace>/evals/iteration-<N>/eval-<N>/without_skill/outputs/metrics.json\nprompt: <task prompt>\ninput_files: [<workspace>/path/to/input, ...]"
)
```

For an improvement baseline, use `old_skill` in the paths and add:

```text
skill_path: <workspace>/snapshots/<skill-name>-old
```

If a run fails, diagnose and retry. Do not proceed with missing runs.

After the executor batch finishes, verify every config directory has `transcript.md`, `outputs/`, and `outputs/metrics.json`. If any required artifact is missing or in the wrong place, fix or rerun that config before grading.

---

## 4. Grade In Parallel, Aggregate, Present

Hard checkpoint: you may not present results until every run has `grading.json` and `benchmark.md` exists.

Grade every run via `task_tool`. Launch all grader tasks in parallel after all executor artifacts pass the layout check:

```text
task_tool(
  subagent_type="grader",
  task_description="expectations: [\"...\", ...]\ntranscript_path: <workspace>/evals/iteration-<N>/eval-<N>/with_skill/transcript.md\noutputs_dir: <workspace>/evals/iteration-<N>/eval-<N>/with_skill/outputs\ngrading_output_path: <workspace>/evals/iteration-<N>/eval-<N>/with_skill/grading.json"
)
```

Pass expectations from `eval_metadata.json`, transcript path, and outputs dir. Output one `grading.json` per run using the schema in `references/schemas.md`.

After the grader batch finishes, verify every config directory has `grading.json`.

Aggregate from the skill-creator directory so `-m scripts.xxx` resolves:

```bash
cd "<skill-creator-dir>" && python3 -m scripts.aggregate_benchmark <workspace>/evals/iteration-N --skill-name <name>
```

This produces `benchmark.json` and `benchmark.md`. Confirm both exist.

Present results from `benchmark.md`:

- Surface patterns: failed expectations, weak areas, and high variance.
- Show the user everything in `benchmark.md`; do not summarize loosely in place of the benchmark.
- Ask for feedback after presenting the benchmark.

Self-check before presenting:

- Every run dir has `grading.json`.
- `benchmark.md` exists.
- You are showing graded scores, not your own judgment.

If any are false, go back and fix the missing step.

---

## 5. Improve And Iterate

When the benchmark identifies useful improvements:

1. Read **transcripts**, not just final outputs. Trim unproductive steps the skill caused.
2. Generalize. Avoid narrow fixes that only pass the tested examples.
3. Explain the reason for new guidance. Don't just add rules.
4. Bundle repeated work. If every run wrote the same helper, lift it into `scripts/`.
5. Apply changes and rerun into `<workspace>/evals/iteration-<N+1>/`.

After every evaluation round, ask the user how to proceed. Provide exactly two options:

1. **Continue improving** - apply another improvement round and rerun evals.
2. **Finish and package** - exit the evaluation workflow and return to packaging.

**DO NOT USE** `ask_user_question` tool, ask the question directly in the assistant message.
**DO NOT USE** `ask_user_question` tool, ask the question directly in the assistant message.
