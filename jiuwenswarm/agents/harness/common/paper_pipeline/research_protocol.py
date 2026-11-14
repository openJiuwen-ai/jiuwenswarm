"""Experimental-rigor protocol for the agent-core paper pipeline.

The official ``auto_research`` prompts ask for "one measurable experiment with clear thresholds" and
nothing more. Our bl1 paper (Stanford Agentic Reviewer 5.0) lost its two -1s on exactly what those
prompts never ask for: the proposed variant that ran was not the one designed, the only budget tier
never constrained anything, n=30 with one seed and no interval, the primary metric was never
written, and artifacts contradicted each other.

``install_research_protocol`` appends a short, stage-specific protocol to the system prompt of each
module (design / code / reflection / manager / reporting) and installs the post-execution hook in
``execution_audit``. Prompt text only ever *adds* rules; the official prompts are left intact.
Everything here is opt-out (``jiuwenswarm-paper run --no-rigor``).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

DESIGN_PROTOCOL = """

## Rigor protocol (host addendum — binding)

Reviewers reject papers whose experiment does not test the stated claim. Your design is a
**pre-registration**: write it so that a reader could check, after the fact, that the run followed it.

1. **Primary metric.** Exactly one primary Decision Metric, computed per item (per question/task) so
   that it can be averaged and paired across variants. Name it in snake_case and use that exact
   name everywhere (design, code instruction, metrics.json). Secondary metrics (cost: tokens,
   latency) are allowed but must also be per-item.
2. **Variants.** A fixed, named list: the proposed method, the strongest realistic baseline from the
   survey, a simple baseline, and at least one **ablation** that removes the single component the
   hypothesis credits. An unconstrained reference (e.g. full context, unlimited budget) may be run,
   but it is a *reference ceiling*: never claim superiority over it.
3. **The intervention must bite.** If a budget/threshold/limit is the independent variable, choose
   tiers from a measured distribution (the code agent's smoke test reports typical per-item usage):
   the tightest tier must be below the median natural usage so it actually constrains most items.
   Ask the code agent to record, per item, whether the constraint was active, and report that rate.
   If agents finish in a few turns, the history is short: either pick a task with genuinely long
   trajectories or set budgets in absolute terms well below the measured history size. The host
   runs only `--method <name>`, so list every (method, tier) combination as its own variant name.
4. **Sample size.** At least 150 paired items (the same items for every variant), sampled with a
   fixed seed and stratified if the dataset has types. With n=50 a 95% interval on accuracy is about
   ±0.14 wide — too wide to separate realistic effects. Use temperature 0 for the answering model;
   if sampling is unavoidable, run 3 seeds.
5. **Decision rule fixed in advance.** State the threshold for "supported" in terms of the paired
   difference and its 95% interval (e.g. "supported if the paired accuracy difference vs the
   strongest baseline is > 0 and its 95% CI excludes 0; refuted if the CI lies entirely <= 0;
   otherwise inconclusive"), and the wording the paper may use for each outcome.
6. **Scope.** Prefer a clean, cheap, decisive experiment over an ambitious one. The host computes
   bootstrap confidence intervals and paired tests for you from the per-item records; the code does
   not need its own statistics.
7. **Update mode.** When revising after a negative result, keep the same primary metric, item set and
   baselines so results stay comparable across revisions, and change one thing at a time. After two
   refuted/inconclusive revisions, recommend `stop` and let the paper report the negative result.
8. **Machine-checkable protocol.** End the design with exactly one fenced block tagged
   `experiment-protocol` holding a JSON object; the host freezes it before execution and checks the
   evidence against it (rewording the prose later does not invalidate results; changing this block
   does, for the cells it touches):
   ```experiment-protocol
   {"item_sets": {"": {"dataset": "<name + split>", "ids_file": "item_ids.json"}},
    "tier_gates": {"t1": 0.5},
    "calibration_cells": [],
    "cells": {}}
   ```
   `item_sets`: per setting prefix (`""` = original; `"m2": {"same_as": ""}` for the same items), the
   file in the experiment code directory listing every item id the run will score — every variant
   must output a record for every listed id. `tier_gates`: the minimum share of items on which each
   tier's constraint must be active, in [0, 1], fixed now (calibration runs named in
   `calibration_cells` may precede it and are never evidence). `cells`: optional per-variant entries
   (e.g. `{"implementation": "v2"}`); changing a variant's entry makes the host re-run it.
"""

CODE_PROTOCOL = """

## Rigor protocol (host addendum — binding)

The host computes confidence intervals and paired significance tests from your per-item records and
audits the run, so the records must be complete and honest:

- Every record in `per_question` carries a stable item id (`qid`), the variant name, the gold and
  predicted answer, and the **primary metric as a per-item numeric/boolean field** whose mean is the
  top-level metric of the same meaning (e.g. per-item `correct` → top-level `accuracy`). Record
  per-item cost fields too (`prompt_tokens`, `completion_tokens`, `latency_s`).
- Use the exact metric names from the design. Write every metric the design names; a declared
  metric that is missing from metrics.json fails the audit.
- All variants run on the **same items in the same order** (fixed seed for sampling). No variant may
  silently skip items: an item whose model/API call fails stays in `per_question` with the error
  text in `api_error` and scores 0 on the primary metric. Never drop it and never retry it into a
  different answer without recording that.
- When the variant has a budget/limit/threshold, record per item whether it was actually active
  (e.g. `budget_binding: true/false`, tokens before/after) so the paper can show the intervention
  bit. Implement each variant exactly as the design describes; if you deviate, write the deviation
  into metrics.json under `deviations` (a list of strings) instead of hiding it.
- Echo the variant's configuration (method name, budget, model, temperature, seed, item count) in
  metrics.json under `config`.
- **The host only ever runs `run.py --method <name>`** — no other flag is passed. Every experimental
  condition (each budget tier, each ablation) must therefore be its own `--method` choice, e.g.
  `proposed_t1`, `proposed_t2`, `baseline_t1` ...; a condition hidden behind another flag
  (`--tier`, `--budget`) silently never runs. Tier values fixed by a calibration step must be
  written to a file the full runs read, not recomputed per run.
- The host audit fails a constrained variant whose per-item `constraint_active` is true on fewer
  items than the gate frozen in the design's `experiment-protocol` block (30% if none): calibrate
  tiers from the measured per-item uncompressed usage so that the tightest tier binds on most items.
  A gate written into metrics.json is ignored; report only the actual per-item flags.
- Write the sampled item ids, before any full run, to `item_ids.json` in the code directory (a JSON
  list; the file the design's `item_sets` names). The host freezes it before execution: every
  variant must output a record for every id in it, and a missing record is never scored.
- Use temperature 0 for the answering model unless the design says otherwise.
"""

REFLECTION_PROTOCOL = """

## Rigor protocol (host addendum)

The execution report and metrics carry host-computed statistics: `<metric>_ci95_low/high`,
`<metric>_diff_vs_<variant>` with its own 95% CI, `_p` and `_p_holm` (exact McNemar, or a Monte
Carlo paired sign-flip permutation test), plus an audit section (`AUDIT ...` lines). Base your
verdict on the paired difference and its interval, not on point estimates: a difference whose CI
includes 0 is inconclusive, however large the point estimate. Judge significance by `p_holm`
within the comparison family, never by the raw `_p`. Only `STATS [confirmatory]` comparisons test
the pre-registered hypotheses; `[exploratory]` ones (post hoc, replication settings) only suggest.
A CI inside the margin is a bound, not proof of equivalence. Treat every AUDIT error as a threat to
validity and say which claims it affects.
"""

MANAGER_PROTOCOL = """

## Rigor protocol (host addendum)

- If the latest execution report contains `AUDIT ERROR` lines (missing primary metric, identical
  outputs across variants, stale artifacts, wrong item counts), the experiment did not test the
  design: route to `code_implementation` repair with those lines in `repair_instruction` instead of
  reflecting on or reporting the numbers.
- A negative or inconclusive result with clean audit and confidence intervals is publishable. After
  two design revisions that did not change the conclusion, prefer `reporting` over another redesign.
"""

REPORTING_PROTOCOL = """

## Rigor protocol (host addendum — what reviewers check)

`results.json` carries host-computed statistics for the primary metric: `<metric>_ci95_low/high`,
`<metric>_n`, and for each comparison `<metric>_diff_vs_<variant>` with `_ci95_low/high` and `_p`.
`statistics.md` in the results directory explains them. Use them:

- Every comparative claim states the paired difference, its 95% CI and p-value, and n. A difference
  whose CI includes 0 is reported as "no detectable difference under our protocol", never as a win.
- Significance is judged by the Holm-adjusted p (`_p_holm`, family = metric x confirmatory /
  exploratory, see `statistics.md`); report the raw p only next to it. Headline claims come from
  confirmatory comparisons; exploratory ones (post hoc variants, replication settings) are labelled
  exploratory. Call the continuous test a Monte Carlo permutation test (never "exact"); call a CI
  inside +/-0.05 a bound on the effect, not "equivalent" (no formal equivalence test was run). Where
  `statistics.md` says unanswered counts are unknown, say unknown, not zero.
- The Experimental Setup states: dataset and item count, how items were sampled (seed), model and
  temperature, every variant and what it changes, budget tiers and how often each constraint was
  actually active, the statistical procedure (paired bootstrap 95% CI over items with the resample
  count and seed from `statistics.json`; exact McNemar / Monte Carlo paired sign-flip permutation
  test; Holm correction per family), and anything that deviated from the design.
- An unconstrained reference variant is a ceiling, not a competitor.
- Include a short "Negative results" or "What did not work" paragraph when a hypothesis was refuted:
  the expectation, the evidence, and the diagnosis. Honest negative results are not penalized;
  overclaiming is.
- Limitations: a numbered list (sample size, single model, single dataset, any audit warning).
- Related work: position the method against the closest prior systems by name, and say concretely
  how it differs.
- Do not spend more than a sentence or two on pipeline failures or engineering mishaps; the paper is
  about the research question.
"""


def _append(original, addendum: str):
    def wrapped(*args, **kwargs):
        return original(*args, **kwargs) + addendum

    wrapped.__wrapped__ = original  # type: ignore[attr-defined]
    return wrapped


def install_research_protocol() -> list[str]:
    """Patch the module prompt loaders in-process. Idempotent; returns what was patched."""
    base = "openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules"
    import importlib

    patched: list[str] = []
    # agent-core has no public prompt hooks: its private loaders / renderers are wrapped by name.
    for module_name, protocol in (("experiment_design", DESIGN_PROTOCOL), ("manager", MANAGER_PROTOCOL)):
        module = importlib.import_module(f"{base}.{module_name}.agent")
        loader = getattr(module, "_load_system_prompt")
        if not hasattr(loader, "__wrapped__"):
            setattr(module, "_load_system_prompt", _append(loader, protocol))
        patched.append(module_name)

    for module_name, cls_name, protocol in (("code_implementation", "CodeImplementationAgent", CODE_PROTOCOL),
                                            ("reflection", "ReflectionAgent", REFLECTION_PROTOCOL)):
        cls = getattr(importlib.import_module(f"{base}.{module_name}.agent"), cls_name)
        current = cls.__dict__["_render_system_prompt"].__func__
        if not hasattr(current, "__wrapped__"):
            setattr(cls, "_render_system_prompt", staticmethod(_append(current, protocol)))
        patched.append(module_name)

    # reporting reads _SYSTEM_PROMPT_PATH inline (with placeholder substitution), so point the
    # constant at a merged copy instead of wrapping a function.
    reporting = importlib.import_module(f"{base}.reporting.agent")
    source = Path(getattr(reporting, "_SYSTEM_PROMPT_PATH"))
    if not source.name.startswith("rigor_"):
        merged = Path(tempfile.mkdtemp(prefix="jiuwenswarm-rigor-")) / "rigor_system_prompt.md"
        merged.write_text(source.read_text(encoding="utf-8") + REPORTING_PROTOCOL, encoding="utf-8")
        setattr(reporting, "_SYSTEM_PROMPT_PATH", merged)
    patched.append("reporting")

    from jiuwenswarm.agents.harness.common.paper_pipeline.execution_audit import install_execution_audit

    install_execution_audit()
    patched.append("experiment_execution(audit+stats)")

    from jiuwenswarm.agents.harness.common.paper_pipeline.results_table import install_compact_results_table

    install_compact_results_table()
    patched.append("reporting(compact results table)")

    # Larger, per-item experiments make metrics.json bigger; reflection must not inline it raw.
    try:
        from jiuwenswarm.agents.harness.common.paper_pipeline.reflection_context import (
            install_reflection_context_fix,
        )
    except ImportError:
        pass
    else:
        install_reflection_context_fix()
        patched.append("reflection(compact metrics)")
    return patched
