"""Regression tests for the evidence gate (no model calls): successful cells are reused instead of
re-run, the re-run cap never hides valid evidence, retired cells are stated, the current design is
always checked, activation gates and item sets come from the host protocol, and a review item is
resolved by evidence only through the comparison the protocol requires for it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.paper_pipeline import (
    evidence,
    execution_audit,
    experiment_protocol,
    revision,
    revision_state,
    rigor_stats,
    runner,
)
from jiuwenswarm.agents.harness.common.paper_pipeline.latex_check import FinalCheck
from test_paper_pipeline_evidence import (  # noqa: E402  (sibling test module: shared evidence fixtures)
    DESIGN,
    _audit,
    _cell,
    _codes,
    _pattern,
    _records,
    _revision,
    _revision_execution,
)

GOOD = {"proposed_T1": _cell(_pattern(40)), "baseline_T1": _cell(_pattern(30))}


@pytest.fixture(autouse=True)
def _strict_defaults(monkeypatch):
    monkeypatch.setattr(evidence, "_CONFIG", {"missing_primary_rule": None, "delivery_policy": None, "tier_gates": None})
    monkeypatch.setattr(revision_state, "_ACTIVE", None)


def _design(block: dict) -> str:
    return DESIGN + "\n```experiment-protocol\n" + json.dumps(block) + "\n```\n"


def _manifest(results: Path) -> dict:
    return evidence.load_manifest(results)


# --------------------------------------------------------------------------- 1. reuse + re-run limit
def test_successful_cell_is_reused_while_only_the_failed_one_is_repaired(tmp_path: Path):
    state, results = _revision(tmp_path)
    ran: list[str] = []
    _revision_execution(tmp_path, state, results, {"abl_a_T1": _cell(_pattern(35)), "abl_b_T1": None}, ran=ran)
    assert sorted(ran) == ["abl_a_T1", "abl_b_T1"]
    assert not revision_state.evidence_status(tmp_path, state)[0]
    ran.clear()
    notes = _revision_execution(tmp_path, state, results, {"abl_a_T1": _cell(_pattern(99)), "abl_b_T1": _cell(_pattern(33))},
                                ran=ran)
    assert ran == ["abl_b_T1"]  # A is referenced from its verified version, not executed again
    assert "reused 1 verified evidence versions" in notes and "abl_a_T1 (v1)" in notes
    cell_a = _manifest(results)["cells"]["abl_a_T1"]
    assert cell_a["role"] == "reused" and cell_a["version"] == 1
    assert all(cell_a.get(key) is not None for key in ("model", "dataset", "budget"))  # conditions stay checkable
    assert json.loads((results / "abl_a_T1.metrics.json").read_text())["answer_em"] == pytest.approx(35 / 60)
    assert state.cell_executions == {"abl_a_T1": 1, "abl_b_T1": 2}
    ok, detail = revision_state.evidence_status(tmp_path, state)
    assert ok, detail


def test_rerun_limit_bounds_new_executions_but_never_hides_valid_evidence(tmp_path: Path):
    state, results = _revision(tmp_path, cap=5)
    state.max_cell_executions = 1
    _revision_execution(tmp_path, state, results, {"abl_a_T1": _cell(_pattern(35)), "abl_b_T1": None})
    notes = _revision_execution(tmp_path, state, results, {"abl_a_T1": _cell(_pattern(35)), "abl_b_T1": _cell(_pattern(33))})
    assert "CELL_RERUN_LIMIT" in notes and "abl_b_T1" in notes  # B has no valid version and no executions left
    assert _manifest(results)["cells"]["abl_a_T1"]["role"] == "reused"  # A is at its limit and still counts
    result = revision_state.evidence_check(tmp_path, state)
    assert "abl_a_T1" not in json.dumps([b for b in result["blocking"] if b["code"] == "CELL_NOT_COMPLETED"])
    # once B is retired, the valid evidence alone carries the revision
    evidence.retire_cell(results, "abl_b_T1", reason="its implementation cannot run within the execution limit",
                         affected_comparisons=["proposed_T1 vs abl_b_T1"], affected_claims=["component B matters"])
    _revision_execution(tmp_path, state, results, {"abl_a_T1": _cell(_pattern(35))})
    ok, detail = revision_state.evidence_status(tmp_path, state)
    assert ok, detail


def test_reuse_after_an_unflagged_code_change_is_reported(tmp_path: Path):
    state, results = _revision(tmp_path)
    code = str(results.parent / "generated_code")
    _revision_execution(tmp_path, state, results, {"abl_a_T1": _cell(_pattern(35)), "abl_b_T1": None}, code_dir=code)
    (results.parent / "generated_code" / "run.py").write_text("print('v2')", encoding="utf-8")
    notes = _revision_execution(tmp_path, state, results, {"abl_a_T1": _cell(_pattern(35)),
                                                           "abl_b_T1": _cell(_pattern(33))}, code_dir=code)
    assert "REUSED_AFTER_CODE_CHANGE" in notes and "abl_a_T1" in notes.split("REUSED_AFTER_CODE_CHANGE")[1][:200]


def test_changed_cell_spec_requires_a_rerun_and_keeps_old_versions(tmp_path: Path):
    state, results = _revision(tmp_path)
    _revision_execution(tmp_path, state, results, {"abl_a_T1": _cell(_pattern(35))})
    ran: list[str] = []
    notes = _revision_execution(tmp_path, state, results, {"abl_a_T1": _cell(_pattern(36))}, ran=ran,
                                design=_design({"cells": {"abl_a_T1": {"implementation": "v2"}}}))
    assert ran == ["abl_a_T1"] and "re-run required for abl_a_T1: spec changed since v1" in notes
    versions = evidence.load_ledger(results)["cells"]["abl_a_T1"]
    assert [v["version"] for v in versions] == [1, 2] and versions[0]["spec_sha256"] != versions[1]["spec_sha256"]
    assert Path(versions[0]["version_path"]).is_file()  # the old version is kept
    assert revision_state.evidence_status(tmp_path, state)[0]


def test_retired_failed_cell_is_a_stated_limitation_and_the_rest_is_rechecked(tmp_path: Path):
    state, results = _revision(tmp_path)
    _revision_execution(tmp_path, state, results, {"abl_a_T1": _cell(_pattern(35)), "abl_b_T1": None})
    with pytest.raises(ValueError, match="substantive"):
        evidence.retire_cell(results, "abl_b_T1", reason="broken", affected_comparisons=[], affected_claims=[])
    evidence.retire_cell(results, "abl_b_T1", reason="the component cannot be isolated in this code base",
                         affected_comparisons=["proposed_T1 vs abl_b_T1"], affected_claims=["component B drives the gain"])
    # offline re-check under the new protocol (`jiuwenswarm-paper audit`): nothing is executed
    execution_audit.reaudit(results, design_text=DESIGN, design_path=results.parent / "design" / "experiment_design.md",
                            code_dir=results.parent / "generated_code", revision=state, run_dir=tmp_path)
    result = revision_state.evidence_check(tmp_path, state)
    assert result["ok"], result["blocking"]
    assert [r["cell"] for r in result["retired"]] == ["abl_b_T1"]
    assert any("abl_b_T1 was retired" in item and "component B drives the gain" in item for item in result["limitations"])
    assert "Retired cells" in evidence.writing_brief(result)
    assert evidence.load_protocol(results)["required_cells"] == ["abl_a_T1", "baseline_T1", "proposed_T1"]
    # a review response may not cite the retired cell as evidence
    paper = results.parent / "paper"
    paper.mkdir()
    (paper / "main.tex").write_text("\\section{Ablations}", encoding="utf-8")
    (paper / revision.RESPONSE_FILE).write_text(json.dumps({"items": [
        {"id": "R-1", "disposition": "new_experiment", "evidence": ["abl_b_T1"], "paper_location": "Ablations"}]}))
    check = revision.check_response([{"id": "R-1", "text": "x"}], paper, results, revision=state)
    assert "retired" in check["unresolved"][0]["why"]


# --------------------------------------------------------------------------- 2. the current design is always checked
def test_design_changed_after_the_audit_is_refused_by_verify_rail_and_acceptance(tmp_path: Path):
    from jiuwenswarm.agents.harness.common.paper_pipeline.evidence_rail import EvidenceService

    exp = tmp_path / "experiments" / "r1"
    (exp / "manager").mkdir(parents=True)
    (exp / "manager" / "state.json").write_text("{}", encoding="utf-8")
    results = exp / "results"
    assert _audit(results, GOOD, design=_design({"tier_gates": {"t1": 0.3}})) == "passed"
    service = EvidenceService(tmp_path)
    assert service.check()["ok"] and evidence.verify(results)["ok"]
    design = exp / "design" / "experiment_design.md"
    # rewording the prose of a structured design does not invalidate the evidence
    design.write_text(design.read_text(encoding="utf-8") + "\nA clearer sentence about the setup.\n", encoding="utf-8")
    assert evidence.verify(results)["ok"]
    # changing an experimental condition does: no caller passes the design, verify reads it itself
    design.write_text(_design({"tier_gates": {"t1": 0.6}}), encoding="utf-8")
    assert "DESIGN_CHANGED" in _codes(evidence.verify(results))
    assert "DESIGN_CHANGED" in _codes(service.check(fresh=True))
    record = runner.write_acceptance(tmp_path, results, pipeline_status="complete", paper_check=FinalCheck("passed"))
    assert record["deliverable"] is False and record["checks"]["evidence_passed"] is False
    # a design that cannot be found is unverified, never a pass
    design.rename(design.with_suffix(".bak"))
    assert "DESIGN_UNVERIFIED" in _codes(evidence.verify(results))
    design.with_suffix(".bak").rename(design)
    # a new protocol for the new design: the old results were recorded under the old gate, so they
    # are refused under it (a re-audit cannot move a gate after the fact) ...
    execution_audit.reaudit(results, design_text=design.read_text(encoding="utf-8"), design_path=design,
                            code_dir=exp / "generated_code")
    assert "CELL_SPEC_CHANGED" in _codes(evidence.verify(results))
    # ... and executing under it restores acceptance
    assert _audit(results, GOOD, design=design.read_text(encoding="utf-8")) == "passed"
    after = evidence.verify(results)
    assert after["ok"], after["blocking"]
    assert service.check(fresh=True)["ok"]
    # a change that touches no cell's condition (a review comparison) needs only the re-audit
    design.write_text(_design({"tier_gates": {"t1": 0.6}, "review_comparisons": [
        {"item": "R-1", "a": "proposed_T1", "b": "baseline_T1"}]}), encoding="utf-8")
    assert "DESIGN_CHANGED" in _codes(evidence.verify(results))
    execution_audit.reaudit(results, design_text=design.read_text(encoding="utf-8"), design_path=design,
                            code_dir=exp / "generated_code")
    assert evidence.verify(results)["ok"]


def test_reaudit_under_a_changed_cell_condition_refuses_the_old_result(tmp_path: Path):
    results = tmp_path / "experiments" / "r1" / "results"
    assert _audit(results, GOOD) == "passed"
    design = results.parent / "design" / "experiment_design.md"
    changed = _design({"cells": {"baseline_T1": {"implementation": "v2"}}})
    design.write_text(changed, encoding="utf-8")
    execution_audit.reaudit(results, design_text=changed, design_path=design, code_dir=results.parent / "generated_code")
    result = evidence.verify(results)
    assert not result["ok"] and "CELL_SPEC_CHANGED" in _codes(result)
    assert "baseline_T1" in json.dumps(result["blocking"])


# --------------------------------------------------------------------------- 3. gates from the host protocol
def test_activation_gate_is_part_of_the_protocol_identity_and_range_checked(tmp_path: Path):
    with pytest.raises(ValueError, match="not a rate"):
        evidence.configure(tier_gates={"t1": 1.5})
    with pytest.raises(ValueError, match="not a rate"):
        experiment_protocol.parse_gate_options(["T1=-0.1"])
    base = dict(declared=["answer_em"], planned=["proposed_T1", "baseline_T1"], design_text=DESIGN)
    ids = {evidence.protocol_id(evidence.build_protocol(**base, operator_gates={"t1": g})) for g in (0.5, 0.6)}
    assert len(ids) == 2
    bad = evidence.build_protocol(declared=["answer_em"], planned=["proposed_T1"],
                                  design_text=_design({"tier_gates": {"t1": 2}}))
    assert any("not a rate in [0, 1]" in p for p in bad["problems"])

    def bound(rate_good: int) -> dict:
        records = _records(_pattern(40))
        for i, r in enumerate(records):
            r["constraint_active"] = i < rate_good
        return _cell([], records=records, tier_gates={"T1": 0.0})  # the output lowers its own gate to 0

    results = tmp_path / "results"
    verdict = _audit(results, {"proposed_T1": bound(30), "baseline_T1": bound(30)},
                     design=_design({"tier_gates": {"t1": 0.6}}))
    audit = json.loads((results / "audit.json").read_text(encoding="utf-8"))
    assert verdict == "failed" and "CONSTRAINT_INACTIVE" in json.dumps(audit["blocking"])
    assert "protocol design tier_gates[t1]" in json.dumps(audit["findings"])
    assert evidence.load_protocol(results)["tier_gates"] == {"t1": 0.6}


def test_gate_not_pre_registered_is_reported_as_a_limitation(tmp_path: Path):
    results = tmp_path / "results"
    assert _audit(results, GOOD) == "passed"
    result = evidence.verify(results)
    assert result["ok"] and any("no activation gate was pre-registered" in item for item in result["limitations"])


# --------------------------------------------------------------------------- 4. pre-declared item set
def test_items_both_sides_omit_are_reported_and_never_verified(tmp_path: Path):
    results = tmp_path / "five"
    four = {"proposed_T1": _cell([1, 1, 0, 1]), "baseline_T1": _cell([0, 1, 0, 0])}
    _audit(results, four, items=[f"q{i}" for i in range(5)])
    (entry,) = _manifest(results)["required_comparisons"]
    assert entry["status"] == "unverified" and "1 of 5 declared items have no record" in entry["reason"]
    result = evidence.verify(results)
    assert not result["ok"] and not result["primary_hypothesis_verified"]

    results = tmp_path / "hundred_fifty"
    shared = {"proposed_T1": _cell(_pattern(90, 140)), "baseline_T1": _cell(_pattern(70, 140))}
    _audit(results, shared, items=[f"q{i}" for i in range(150)])
    cell = _manifest(results)["cells"]["proposed_T1"]["item_coverage"]
    assert cell["declared_n"] == 150 and cell["missing_records"] == 10 and not cell["complete"]
    assert "10 of 150 declared items have no record" in evidence.verify(results)["unverified_comparisons"][0]["reason"]


def test_missing_values_are_counted_by_kind_and_the_rule_keeps_the_denominator(tmp_path: Path):
    records = _records(_pattern(40))
    for i, kind in ((0, "unanswered"), (1, "api_error"), (2, "parse_error")):
        del records[i]["answer_em"]
        if kind == "unanswered":
            records[i]["predicted"] = ""
        else:
            records[i][kind] = "x"
    proposed = _cell([], records=records, top=sum(r.get("answer_em", 0) for r in records) / 60)
    strict = tmp_path / "strict"
    _audit(strict, {"proposed_T1": proposed, "baseline_T1": _cell(_pattern(30))})
    coverage = _manifest(strict)["cells"]["proposed_T1"]["item_coverage"]
    assert coverage["missing_metric"] == {"unanswered": 1, "api_error": 1, "parse_error": 1}
    assert not evidence.verify(strict)["ok"]
    evidence.configure(missing_primary_rule="score_zero")
    counted = tmp_path / "counted"
    assert _audit(counted, {"proposed_T1": proposed, "baseline_T1": _cell(_pattern(30))}) == "passed"
    coverage = _manifest(counted)["cells"]["proposed_T1"]["item_coverage"]
    assert coverage["scored_zero"] == 3 and coverage["declared_n"] == 60 and coverage["complete"]
    (entry,) = evidence.verify(counted)["verified_comparisons"]
    assert entry["n_paired"] == 60 and entry["n_declared"] == 60  # nothing dropped, denominator stated
    # a missing record is never scored, whatever the rule
    gone = _cell([], records=_records(_pattern(40))[1:], top=40 / 59)
    _audit(tmp_path / "gone", {"proposed_T1": gone, "baseline_T1": _cell([], records=_records(_pattern(30))[1:],
                                                                         top=29 / 59)},
           items=[f"q{i}" for i in range(60)])
    assert not evidence.verify(tmp_path / "gone")["ok"]


def test_undeclared_item_set_blocks_a_confirmatory_delivery(tmp_path: Path):
    results = tmp_path / "results"
    _audit(results, GOOD, items=[])  # the code writes an empty declaration: nothing pre-declared
    result = evidence.verify(results)
    assert not result["ok"] and {"ITEM_SET_UNDECLARED", "PROTOCOL_INVALID"} <= _codes(result)


# --------------------------------------------------------------------------- 5. review comparisons
REVIEW = _design({
    "item_sets": {"m2": {"same_as": ""}},
    "review_comparisons": [
        {"item": "R-abl", "a": "proposed_T1", "b": "abl_x_T1"},  # the ablation that will fail
        {"item": "R-rep", "a": "m2__proposed_T1", "b": "m2__baseline_T1", "conditions": {"model": "qwen-plus"}},
        {"item": "R-neg", "a": "proposed_T1", "b": "abl_y_T1"},  # a valid negative result
    ]})


def test_review_item_is_resolved_only_by_the_comparison_the_protocol_requires(tmp_path: Path):
    state, results = _revision(tmp_path, cap=6)
    _revision_execution(tmp_path, state, results, {
        "abl_x_T1": None,  # single-sided ablation: the ablated arm failed
        "m2__proposed_T1": _cell(_pattern(41), model="qwen-plus"),  # replication without its baseline
        "abl_y_T1": _cell(_pattern(55)),  # ablation outperforms the proposed method: a negative result
    }, design=REVIEW)
    review = {r["item"]: r for r in _manifest(results)["review_comparisons"]}
    assert review["R-abl"]["status"] == "failed" and review["R-rep"]["status"] == "failed"
    assert review["R-neg"]["status"] == "verified" and review["R-neg"]["outcome"] == "a_worse"
    paper = results.parent / "paper"
    paper.mkdir()
    (paper / "main.tex").write_text("\\section{Ablations}\\section{Replication}\\section{Limitations}", encoding="utf-8")
    entries = [{"id": i, "disposition": "new_experiment", "paper_location": "Ablations"}
               for i in ("R-abl", "R-rep", "R-neg", "R-none")]
    entries.append({"id": "R-narrow", "disposition": "narrowed_claim", "paper_location": "Limitations"})
    (paper / revision.RESPONSE_FILE).write_text(json.dumps({"items": entries}), encoding="utf-8")
    items = [{"id": i, "text": i} for i in ("R-abl", "R-rep", "R-neg", "R-none", "R-narrow")]
    check = revision.check_response(items, paper, results, revision=state)
    states = {r["id"]: r["state"] for r in check["resolved"]}
    assert states == {"R-neg": "verified_resolved", "R-narrow": "addressed"}  # the negative result answers it
    why = {u["id"]: u["why"] for u in check["unresolved"]}
    assert "abl_x_T1 failed" in why["R-abl"] and "m2__baseline_T1 not run" in why["R-rep"]
    assert "requires no comparison for this item" in why["R-none"]
    brief = evidence.writing_brief(evidence.verify(results, revision=state))
    assert "`R-neg`" in brief and "a_worse" in brief


def test_review_comparison_checks_the_conditions_it_was_declared_with(tmp_path: Path):
    state, results = _revision(tmp_path)
    _revision_execution(tmp_path, state, results, {"m2__proposed_T1": _cell(_pattern(41), model="qwen-max"),
                                                   "m2__baseline_T1": _cell(_pattern(31), model="qwen-max")},
                        design=REVIEW)
    review = {r["item"]: r for r in _manifest(results)["review_comparisons"]}
    assert review["R-rep"]["status"] == "unverified" and "requires 'qwen-plus'" in review["R-rep"]["reason"]


def test_item_sets_may_differ_by_setting_but_a_comparison_needs_one(tmp_path: Path):
    protocol = evidence.build_protocol(
        declared=["answer_em"], planned=["proposed_T1", "ds2__proposed_T1"],
        design_text=_design({"item_sets": {"": {"ids": ["q0", "q1"]}, "ds2": {"ids": ["z0", "z1"]}}}))
    assert protocol["item_sets"][""]["sha256"] != protocol["item_sets"]["ds2"]["sha256"]
    cells = {n: {"status": "completed", "item_coverage": evidence.item_coverage(protocol, n, m)}
             for n, m in (("proposed_T1", _cell([1, 0])),
                          ("ds2__proposed_T1", _cell([], records=[{**r, "qid": f"z{i}"} for i, r in
                                                                   enumerate(_records([1, 1]))])))}
    assert all(c["item_coverage"]["complete"] for c in cells.values())
    match = [{"metric": "answer_em", "a": "proposed_T1", "b": "ds2__proposed_T1", "status": "verified",
              "outcome": "a_worse"}]
    entry = evidence._comparison_status({"metric": "answer_em", "a": "proposed_T1", "b": "ds2__proposed_T1"},
                                        protocol, match, cells)
    assert entry["status"] == "unverified" and "different item sets" in entry["reason"]
    assert rigor_stats.split_setting("ds2__proposed_T1")[0] == "ds2"


# --------------------------------------------------------------------------- review follow-ups (#7729)
def _ledger_entry(results: Path, name: str, version: int, spec: str) -> dict:
    archive = results.parent / "evidence" / "cells" / name / f"v{version}.metrics.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(json.dumps({"answer_em": 0.5, "version": version}), encoding="utf-8")
    entry = {"version": version, "valid": True, "spec_sha256": spec, "version_path": str(archive),
             "metrics_sha256": evidence.sha256_file(archive)}
    return {**entry, "digest": evidence.sha256_json(entry)}


def test_damaged_newer_version_does_not_hide_an_intact_older_one(tmp_path: Path):
    results = tmp_path / "results"
    results.mkdir()
    older, newer = _ledger_entry(results, "abl_a_T1", 1, "S"), _ledger_entry(results, "abl_a_T1", 2, "S")
    ledger = results.parent / "evidence" / "ledger.json"
    ledger.write_text(json.dumps({"cells": {"abl_a_T1": [older, newer]}}), encoding="utf-8")
    Path(newer["version_path"]).unlink()
    entry, note = evidence.reusable_version(results, "abl_a_T1", "S")
    assert entry is not None and entry["version"] == 1 and "newer v2" in note
    Path(older["version_path"]).write_text("{}", encoding="utf-8")
    entry, note = evidence.reusable_version(results, "abl_a_T1", "S")
    assert entry is None and "no intact version" in note and "re-run required" in note


def test_unreadable_evidence_file_blocks_instead_of_crashing(tmp_path: Path):
    results = tmp_path / "results"
    results.mkdir()
    folder = results.parent / "evidence"
    folder.mkdir()
    (folder / "manifest.json").write_text("{not json", encoding="utf-8")
    result = evidence.verify(results)
    assert not result["ok"] and result["blocking"][0]["code"] == "EVIDENCE_UNREADABLE"
    assert any(t.startswith("EVIDENCE_UNREADABLE") for t in result["pending_tasks"])
