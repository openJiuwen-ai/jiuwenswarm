"""Model-free walk-through of the paper pipeline's evidence contract and PaperEvidenceRail.

    python scripts/paper_evidence_demo.py [--out DIR]

Runs, without any model call, the host side of one paper run and one review-driven revision:

1. a first execution (proposed vs baseline) — protocol frozen before it from the design's
   ``experiment-protocol`` block (pre-declared item ids, activation gate), evidence recorded, verified;
2. a revision that asks for a second-model replication (a review comparison bound to the review
   item): of its two new cells one succeeds and one fails -> acceptance says ``needs_repair``, the
   revision (guard, caps, budgets) stays active;
3. a "restart": a new guard is built from the persisted revision; the successful cell is *reused*
   from its verified evidence version (not re-run, no execution counted), only the failed cell is
   re-run -> the evidence verifies -> PaperEvidenceRail's check passes -> the review item is
   resolved by verified evidence -> acceptance is deliverable and the revision is accepted.

The experiment subprocesses are replaced by a deterministic stand-in (the only fake); the
execution wrapper, guard, ledger, audit, statistics, evidence manifest, acceptance and the rail's
evidence service are the real code. The LaTeX check is reported as passed (no TeX needed). Output:
the run directory (``--out``) with ``acceptance.json``, ``experiments/demo-r1/evidence/`` (protocol,
manifests, ledger) and the revision. This is a simulation, not a paper run.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

N_ITEMS = 120
DESIGN = """# Experiment design (demo)
- Metric answer_em (primary, per item)
- Metric prompt_tokens
Variants: proposed_T1, baseline_T1 (budget 325 tokens each).

```experiment-protocol
{"item_sets": {"": {"dataset": "demo-items-v1", "ids_file": "item_ids.json"}},
 "tier_gates": {"t1": 0.3}}
```
"""
REVISION_DESIGN = """# Experiment design (demo, revision 1)
- Metric answer_em (primary, per item)
- Metric prompt_tokens
Existing (do not re-run): proposed_T1, baseline_T1. New (post hoc): the same comparison answered by a
second model, m2__proposed_T1 vs m2__baseline_T1, answering review item R-demo0001.

```experiment-protocol
{"item_sets": {"": {"dataset": "demo-items-v1", "ids_file": "item_ids.json"}, "m2": {"same_as": ""}},
 "tier_gates": {"t1": 0.3},
 "cells": {"m2__proposed_T1": {"post_hoc": true}, "m2__baseline_T1": {"post_hoc": true}},
 "review_comparisons": [{"item": "R-demo0001", "a": "m2__proposed_T1", "b": "m2__baseline_T1",
                         "conditions": {"model": "m2"}}]}
```
"""


_OUT = logging.getLogger("jiuwenswarm.paper.demo")


def say(*parts: object) -> None:
    """Demo output on stdout (space-separated like ``print``), apart from the pipeline's own logs."""
    if not _OUT.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        _OUT.addHandler(handler)
        _OUT.setLevel(logging.INFO)
        _OUT.propagate = False
    _OUT.info(" ".join(str(p) for p in parts))


def cell(name: str, accuracy: float, *, model: str = "demo-model", n: int = N_ITEMS, budget: int = 325) -> dict:
    """Deterministic per-item records for one variant (stands in for an experiment subprocess)."""
    rng = random.Random(name)
    records = []
    for i in range(n):
        correct = int(rng.random() < accuracy)
        records.append({"qid": f"q{i:03d}", "answer_em": correct, "predicted": f"{name}-{i}-{correct}",
                        "model": model, "budget_tokens": budget, "prompt_tokens": 200 + rng.randrange(50),
                        "constraint_active": i % 2 == 0})
    return {"answer_em": sum(r["answer_em"] for r in records) / n,
            "prompt_tokens": sum(r["prompt_tokens"] for r in records) / n, "budget_tokens": budget,
            "config": {"model": model, "item_list_hash": "demo-items-v1"}, "per_question": records}


def executor(results: Path, outcomes: dict[str, dict | None], ran: list[str]):
    """``ExperimentExecutionAgent.run`` stand-in: writes metrics for what the host asked it to run."""

    def run(agent, inputs):
        variants = []
        for v in inputs.implementation.variants:
            ran.append(v.name)
            payload = outcomes.get(v.name)
            if payload is None:
                variants.append(SimpleNamespace(name=v.name, metrics=None, process_status="failed", exit_code=1))
                continue
            (results / f"{v.name}.metrics.json").write_text(json.dumps(payload), encoding="utf-8")
            variants.append(SimpleNamespace(name=v.name, metrics=payload, process_status="completed", exit_code=0))
        return SimpleNamespace(result=SimpleNamespace(variants=variants, notes="", status="completed"))

    return run


def inputs_for(names: list[str], code: Path):
    plan = SimpleNamespace(run_id="demo-r1", metrics=[], design_path="experiments/demo-r1/design/experiment_design.md")
    return SimpleNamespace(plan=plan, implementation=SimpleNamespace(
        workspace_dir=str(code), variants=[SimpleNamespace(name=n, invocation=["python", "run.py", "--method", n])
                                           for n in names]))


def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=None, help="run directory to create (default: a temporary one)")
    args = parser.parse_args(argv)

    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.common.workspace import set_project_root

    from jiuwenswarm.agents.harness.common.paper_pipeline import evidence, execution_audit, revision_state, runner
    from jiuwenswarm.agents.harness.common.paper_pipeline.evidence_rail import EvidenceService
    from jiuwenswarm.agents.harness.common.paper_pipeline.latex_check import FinalCheck

    run_dir = Path(args.out or tempfile.mkdtemp(prefix="paper-evidence-demo-")).resolve()
    exp = run_dir / "experiments" / "demo-r1"
    results, code = exp / "results", exp / "generated_code"
    for folder in (results, code, exp / "design", exp / "manager", exp / "paper"):
        folder.mkdir(parents=True, exist_ok=True)
    design = exp / "design" / "experiment_design.md"
    design.write_text(DESIGN, encoding="utf-8")
    (exp / "manager" / "state.json").write_text("{}", encoding="utf-8")
    (code / "run.py").write_text("# experiment code (demo)\n", encoding="utf-8")
    (code / "item_ids.json").write_text(json.dumps([f"q{i:03d}" for i in range(N_ITEMS)]), encoding="utf-8")
    set_project_root(run_dir)

    def step(title: str) -> None:
        say(f"\n== {title}")

    def show(notes: str, prefixes: tuple[str, ...]) -> None:
        say("\n".join(line for line in notes.splitlines() if line.startswith(prefixes)))

    step("1. first execution: protocol frozen by the host (item ids, gate), then run, audit, evidence")
    base = ["proposed_T1", "baseline_T1"]
    ran: list[str] = []
    out = execution_audit.run_audited(executor(results, {"proposed_T1": cell("proposed_T1", 0.62),
                                                         "baseline_T1": cell("baseline_T1", 0.48)}, ran),
                                      None, inputs_for(base, code))
    show(out.result.notes, ("AUDIT VERDICT", "EVIDENCE"))
    protocol = evidence.load_protocol(results)
    say(f"protocol {protocol['protocol_id']}: {protocol['item_sets']['']['n']} declared items "
        f"({protocol['item_sets']['']['source']}), gates {protocol['tier_gates']} ({protocol['tier_gates_source']})")
    say("verify:", evidence.summary_line(evidence.verify(results)))

    step("2. revision: existing results frozen; the design adds a second-model replication; one new cell fails")
    opts = runner.PaperRunOptions(run_dir=run_dir, topic="demo", budget_hard=13.0)
    settings = revision_state.settings_of(opts)
    state = revision_state.RevisionState(index=1, start_round=10, max_new_cells=2, settings=settings,
                                         identity=revision_state.identity_of(settings),
                                         review_items=[{"id": "R-demo0001",
                                                        "text": "Replicate the comparison with a second model."}])
    import time as _time
    _time.sleep(1.1)  # the revision opens strictly after the first execution
    state.created_at = runner.now_iso()
    revision_state.build_manifest(results, code, state.folder(run_dir))
    revision_state.save(state, run_dir)
    design.write_text(REVISION_DESIGN, encoding="utf-8")
    names = base + ["m2__proposed_T1", "m2__baseline_T1"]
    revision_state.install_execution_guard(
        revision_state.ExecutionGuard(run_dir, state, revision_state.load_manifest(state.folder(run_dir))))
    ran = []
    out = execution_audit.run_audited(executor(results, {"m2__proposed_T1": None,  # this new cell fails
                                                         "m2__baseline_T1": cell("m2__baseline_T1", 0.50, model="m2")},
                                               ran), None, inputs_for(names, code))
    say("executed:", ran)
    show(out.result.notes, ("REVISION", "AUDIT VERDICT", "AUDIT ERROR CELL_FAILED", "EVIDENCE review"))
    record = runner.write_acceptance(run_dir, results, pipeline_status="complete", paper_check=FinalCheck("passed"),
                                     revision=state)
    say("acceptance deliverable:", record["deliverable"], "| revision status:", state.status)
    say("Rail check:", evidence.summary_line(EvidenceService(run_dir).check()))
    say("repair follow-up for the manager:\n" + runner.repair_followup(state))

    step("3. restart: the persisted revision is found again; the good cell is reused, the failed one re-run")
    revision_state.install_execution_guard(None)
    reopened = revision_state.find_open(run_dir)
    resumed = runner.PaperRunOptions(run_dir=run_dir, topic="demo")  # no budget argument given
    revision_state.restore_settings(resumed, reopened, run_dir)
    say(f"status {reopened.status}; budget_hard restored = {resumed.budget_hard}")
    revision_state.install_execution_guard(
        revision_state.ExecutionGuard(run_dir, reopened, revision_state.load_manifest(reopened.folder(run_dir))))
    ran = []
    out = execution_audit.run_audited(executor(results, {"m2__proposed_T1": cell("m2__proposed_T1", 0.60, model="m2"),
                                                         "m2__baseline_T1": cell("m2__baseline_T1", 0.50, model="m2")},
                                               ran), None, inputs_for(names, code))
    say("executed:", ran, "| executions per cell:", reopened.cell_executions)
    show(out.result.notes, ("REVISION", "AUDIT VERDICT", "EVIDENCE"))
    ledger = evidence.load_ledger(results)["cells"]
    say("evidence versions:", {n: [f"v{e['version']}:{'valid' if e['valid'] else 'invalid'}" for e in v]
                               for n, v in sorted(ledger.items())})
    (exp / "paper" / "main.tex").write_text("\\section{Replication with a second model}\\label{sec:rep}",
                                            encoding="utf-8")
    (exp / "paper" / "revision_response.json").write_text(json.dumps({"items": [
        {"id": "R-demo0001", "disposition": "new_experiment", "evidence": ["m2__proposed_T1 vs m2__baseline_T1"],
         "conditions": {"model": "m2", "setting": "m2"}, "paper_location": "sec:rep"}]}), encoding="utf-8")
    result = EvidenceService(run_dir).check()
    say("Rail check:", evidence.summary_line(result))
    say(evidence.writing_brief(result))
    record = runner.write_acceptance(run_dir, results, pipeline_status="complete", paper_check=FinalCheck("passed"),
                                     revision=reopened)
    say("acceptance deliverable:", record["deliverable"], "| revision status:", reopened.status,
        "| verified-resolved review items:", record["revision"]["verified_resolved_items"])
    revision_state.install_execution_guard(None)
    say(f"\nrun directory: {run_dir}")
    return run_dir


if __name__ == "__main__":
    main()
