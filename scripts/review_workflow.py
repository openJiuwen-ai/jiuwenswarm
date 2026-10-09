#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run the research workflow gates with no model and no install.

A reviewer executes this file. It writes one JSON line per decision to
``review_workflow.jsonl`` in the working directory and exits 0 only when every
planted defect was refused and the clean draft was accepted.

    python3 scripts/review_workflow.py

The stage order is the formal six-stage registry. A stage cannot be left while
an artifact it requires is absent. Delivery then checks the draft the write
stage would ship: a citation key with no entry, a printed number with no
registry row, or a TODO left in the text is a refusal. A self-graded pass does
not enter the experience bank unless the correlation gate recorded ``accept``.

The gate's own number check is then certified the way the paper certifies any
auditor: values are planted by a seeded coin, the check reads the planted draft,
and its hits are tested exactly. A certified check lets the draft through; an
auditor that accuses every number is not certified, and delivery refuses its
clean report.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    rails = Path(__file__).resolve().parents[1] / "jiuwenswarm/agents/harness/team/rails"
    log_mod = _load("workflow_run_log", rails / "workflow_run_log.py")
    gate_mod = _load("delivery_gate", rails / "delivery_gate.py")
    cert_mod = _load("auditor_certificate", rails / "auditor_certificate.py")

    out = Path("review_workflow.jsonl")
    out.unlink(missing_ok=True)
    log = log_mod.WorkflowRunLog(out, topic_id="review")

    refused = log.advance()
    if refused.decision != "refused" or "topic_brief" not in refused.missing_artifacts:
        print("FAIL: init advanced without topic_brief")
        return 1

    for stage, artifacts in (
        ("init", ("topic_brief",)),
        ("build", (
            "literature_map", "paper_pool_snapshot",
            "citation_expansion_report", "acquisition_report",
        )),
        ("analyze", ("evidence_pack", "claim_candidate_set", "direction_proposal")),
        ("propose", ("adversarial_resolution", "study_spec")),
        ("experiment", ("experiment_code", "experiment_result", "verified_registry")),
    ):
        for kind in artifacts:
            log.record_artifact(kind, stage=stage, ref=f"records/{kind}.json")
        decision = log.advance()
        if decision.decision != "advanced":
            print(f"FAIL: {stage} refused after its artifacts were recorded")
            return 1

    # A crash in the middle of a write leaves half a line. The run continues from
    # its own log, at the stage it had reached, with nothing recorded twice.
    with out.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "crash", "topic_id": "review", "event": "artifact_rec')
    resumed = log_mod.WorkflowRunLog.resume(out, topic_id="review")
    if resumed.stage != log.stage:
        print(f"FAIL: resume came back at {resumed.stage}, the run was at {log.stage}")
        return 1
    log = resumed

    draft = r"recall was 0.857 (\cite{lee2025}). TODO: fill the table."
    findings = gate_mod.delivery_ready(
        draft,
        {"lee2025": "Lee. doi:10.1000/xyz"},
        {"0.857": "records/verified_registry.json#recall"},
    )
    if log.record_delivery(findings):
        print("FAIL: a draft with TODO was marked ready")
        return 1
    if not any(f.check == "residue" for f in findings):
        print("FAIL: residue check did not fire")
        return 1

    dangling = gate_mod.delivery_ready(r"see \cite{missing}", {}, {})
    if not any(f.check == "dangling_citation" for f in dangling):
        print("FAIL: dangling citation was accepted")
        return 1

    invented = gate_mod.delivery_ready(
        r"as shown by \cite{fake2025}",
        {"fake2025": "Fake. A paper that does not exist. doi:10.9999/invented"},
        {},
        resolve=lambda identifier: False,   # the index answers: no such record
    )
    if not any(f.check == "unresolvable_reference" for f in invented):
        print("FAIL: a well-formed DOI that names no record was accepted")
        return 1

    clean = gate_mod.delivery_ready(
        r"recall was 0.857 (\cite{lee2025})",
        {"lee2025": "Lee. doi:10.1000/xyz"},
        {"0.857": "records/verified_registry.json#recall"},
    )
    if clean:
        print(f"FAIL: clean draft produced findings: {clean}")
        return 1

    capped = log_mod.WorkflowRunLog(out, topic_id="review-budget", token_budget=1000)
    capped.record_artifact("topic_brief", stage="init", ref="records/topic_brief.json")
    capped.record_usage(stage="init", total_tokens=1001, source_record="usage.jsonl:1")
    over = capped.advance()
    if over.decision != "refused" or over.reason != "token_budget_exceeded":
        print("FAIL: a run over its token budget advanced")
        return 1

    # Certify the number check before its silence counts. Twelve registered
    # results; each round plants two perturbed values the registry never saw.
    universe = {f"r{i}": f"{0.5 + 0.031 * i:.3f}" for i in range(12)}
    registry = {v: f"records/verified_registry.json#r{i}" for i, v in enumerate(universe.values())}
    render = lambda facts: " ".join(f"result {s} was {v}." for s, v in facts.items())
    trace = lambda text: [f.detail.split(" ", 1)[0] for f in gate_mod.numbers_trace_to_registry(text, registry)]
    certified = cert_mod.certify(universe, render, trace, name="registry_trace",
                                 rounds=6, null_rounds=10, seed=1).to_record()
    everything = cert_mod.certify(universe, render, cert_mod.NUMBER.findall, name="accuse_all",
                                  rounds=6, seed=1).to_record()
    Path("review_workflow_certificates.json").write_text(
        json.dumps({"registry_trace": certified, "accuse_all": everything}, indent=1), encoding="utf-8")
    log.record_artifact("auditor_certificate", stage="write", ref="review_workflow_certificates.json")
    if not certified["certified"]:
        print("FAIL: the registry check found planted values and was not certified")
        return 1
    if everything["certified"]:
        print("FAIL: an auditor that accuses every number was certified")
        return 1
    blocked = gate_mod.delivery_ready(r"recall was 0.857 (\cite{lee2025})", {"lee2025": "Lee. doi:10.1000/xyz"},
                                      {"0.857": "records/verified_registry.json#recall"}, certificate=everything)
    if log.record_delivery(blocked) or blocked[0].check != "uncertified_auditor":
        print("FAIL: delivery accepted a clean report from an uncertified auditor")
        return 1
    passed = gate_mod.delivery_ready(r"recall was 0.857 (\cite{lee2025})", {"lee2025": "Lee. doi:10.1000/xyz"},
                                     {"0.857": "records/verified_registry.json#recall"}, certificate=certified)
    if not log.record_delivery(passed):
        print(f"FAIL: certified auditor still refused: {passed}")
        return 1

    print(f"refused init without topic_brief")
    print(f"advanced init -> build -> analyze -> propose -> experiment -> write")
    print(f"resumed at {log.stage} after a crash cut the last log line")
    print(f"refused delivery: {findings[0].render()}")
    print(f"refused dangling citation")
    print(f"refused a well-formed DOI that names no record")
    print(f"accepted the clean draft")
    print(f"refused a run that spent 1001 tokens against a budget of 1000")
    print(f"certified the registry check at round {certified['certified_at_round']} "
          f"(e = {certified['e_final']:.3g}, false-accusation bound {certified['false_accusation_upper']:.3f})")
    print(f"refused delivery on a clean report from an auditor that accuses every number "
          f"(e = {everything['e_final']:.3g}, {everything['rejections']}/6 rounds rejected)")
    print(f"log: {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
