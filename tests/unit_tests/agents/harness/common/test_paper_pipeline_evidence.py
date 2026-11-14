"""Regression tests for the evidence contract (no model calls): primary comparisons that cannot be
verified block a confirmatory delivery, the primary metric never silently shrinks its sample,
revision resumes keep every control, and acceptance re-checks the complete, hashed evidence."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.paper_pipeline import (
    evidence,
    execution_audit,
    revision,
    revision_state,
    rigor_stats,
    runner,
)
from jiuwenswarm.agents.harness.common.paper_pipeline.latex_check import FinalCheck

DESIGN = "- Metric answer_em (primary)\n- Metric prompt_tokens\n"


@pytest.fixture(autouse=True)
def _strict_defaults(monkeypatch):
    """Every test starts from the strict defaults and no installed guard."""
    monkeypatch.setattr(evidence, "_CONFIG", {"missing_primary_rule": None, "delivery_policy": None})
    monkeypatch.setattr(revision_state, "_ACTIVE", None)


def _records(correct: list, *, budget=325, model="qwen-flash", n=None, **extra) -> list[dict]:
    out = []
    for i, c in enumerate(correct):
        record = {"qid": f"q{i}", "predicted": f"a{i}-{c}", "model": model, "budget_tokens": budget,
                  "prompt_tokens": 100 + i + (7 if c else 0), **extra}
        if c is not None:
            record["answer_em"] = c
        out.append(record)
    return out


def _cell(correct: list, *, budget=325, model="qwen-flash", records=None, top=None, **kw) -> dict:
    records = records or _records(correct, budget=budget, model=model)
    scored = [r["answer_em"] for r in records if "answer_em" in r]
    return {"answer_em": top if top is not None else sum(scored) / len(records), "tier": "T1",
            "budget_tokens": budget, "config": {"model": model, "item_list_hash": "hash-1"},
            "prompt_tokens": sum(r["prompt_tokens"] for r in records) / len(records), "per_question": records, **kw}


def _pattern(good: int, n: int = 60) -> list[int]:
    return [1] * good + [0] * (n - good)


def _declare(results: Path, cells: dict[str, dict | None], design: str = DESIGN, items: list[str] | None = None
             ) -> Path:
    """What a run fixes before execution: the design file (where ``verify`` looks for it) and the item
    ids the code will score (``item_ids.json``; default: the ids the given cells carry)."""
    design_file = results.parent / "design" / "experiment_design.md"
    design_file.parent.mkdir(parents=True, exist_ok=True)
    design_file.write_text(design, encoding="utf-8")
    code_dir = results.parent / "generated_code"
    code_dir.mkdir(parents=True, exist_ok=True)
    if items is None and not (code_dir / "item_ids.json").is_file():
        items = sorted({str(r["qid"]) for c in cells.values() if c for r in rigor_stats.records_of(c) if "qid" in r})
    if items is not None:
        (code_dir / "item_ids.json").write_text(json.dumps(items), encoding="utf-8")
    return code_dir


def _audit(results: Path, cells: dict[str, dict | None], *, design: str = DESIGN, frozen=(), executed=None,
           revision_index=None, code_dir: Path | None = None, items: list[str] | None = None) -> str:
    """One host execution + audit: ``None`` = the cell ran and failed."""
    results.mkdir(parents=True, exist_ok=True)
    code_dir = code_dir or _declare(results, cells, design, items)
    started = time.time() - 0.5
    variants = []
    for name, payload in cells.items():
        if payload is None:
            variants.append(SimpleNamespace(name=name, metrics=None, process_status="failed", exit_code=1))
            continue
        if name not in frozen:
            (results / f"{name}.metrics.json").write_text(json.dumps(payload), encoding="utf-8")
        variants.append(SimpleNamespace(name=name, metrics=payload, process_status="completed", exit_code=0))
    inputs = SimpleNamespace(plan=SimpleNamespace(metrics=[], run_id="r1"),
                             implementation=SimpleNamespace(workspace_dir=str(code_dir or ""), variants=[]))
    output = SimpleNamespace(result=SimpleNamespace(variants=variants, notes="", status="completed"))
    verdict = execution_audit.post_process(inputs, output, started, frozen=set(frozen), results=results,
                                           design_text=design, planned=list(cells), executed=executed,
                                           revision=revision_index,
                                           design_path=results.parent / "design" / "experiment_design.md")
    return verdict


def _codes(result: dict) -> set[str]:
    return {b["code"] for b in result["blocking"]}


# --------------------------------------------------------------------------- task 1: primary comparisons
def test_budget_mismatch_625_vs_650_cannot_pass_the_primary_comparison(tmp_path: Path):
    results = tmp_path / "results"
    verdict = _audit(results, {"proposed_T1": _cell(_pattern(40), budget=625),
                               "baseline_T1": _cell(_pattern(30), budget=650)})
    assert verdict == "failed"
    stats = json.loads((results / "statistics.json").read_text(encoding="utf-8"))
    (comparison,) = [c for c in stats["comparisons"] if c["metric"] == "answer_em"]
    assert comparison["status"] == "unverified" and "625" in comparison["reason"] and "650" in comparison["reason"]
    result = evidence.verify(results)
    assert not result["ok"] and "PRIMARY_COMPARISON_UNVERIFIED" in _codes(result)
    assert not result["primary_hypothesis_verified"]
    # a waiver written by the experiment code cannot lower the bar
    (results / execution_audit.EXCEPTIONS_FILE).write_text(json.dumps({"exceptions": [
        {"code": "PRIMARY_COMPARISON_UNVERIFIED", "variants": ["proposed_T1", "baseline_T1"],
         "reason": "budgets are close enough, we think", "affected_comparisons": ["x"], "affected_claims": ["y"]}]}))
    assert _audit(results, {"proposed_T1": _cell(_pattern(40), budget=625),
                            "baseline_T1": _cell(_pattern(30), budget=650)}) == "failed"


def test_missing_duplicate_ids_and_item_mismatch_are_structured():
    base = _cell(_pattern(30))
    no_ids = _cell([], records=[{k: v for k, v in r.items() if k != "qid"} for r in _records(_pattern(40))])
    dup = _cell([], records=[{**r, "qid": "q0"} if i == 1 else r for i, r in enumerate(_records(_pattern(40)))])
    other = _cell([], records=[{**r, "qid": f"z{i}"} for i, r in enumerate(_records(_pattern(40)))])
    for proposed, needle in ((no_ids, "carry no item id"), (dup, "duplicate item ids"), (other, "item sets differ")):
        stats = rigor_stats.compute({"proposed_T1": proposed, "baseline_T1": base}, primary=["answer_em"])
        (entry,) = stats.comparisons
        assert entry["status"] == "unverified" and needle in entry["reason"], entry
        assert not stats.pairs


def test_a_negative_result_is_verified_evidence_and_deliverable(tmp_path: Path):
    results = tmp_path / "results"
    assert _audit(results, {"proposed_T1": _cell(_pattern(20)), "baseline_T1": _cell(_pattern(45))}) == "passed"
    result = evidence.verify(results)
    assert result["ok"] and result["primary_hypothesis_verified"]
    (entry,) = result["verified_comparisons"]
    assert entry["outcome"] == "a_worse" and entry["ci95"][1] < 0  # proposed is worse: still evidence
    assert "outcome `a_worse`" in evidence.writing_brief(result)


def test_descriptive_policy_delivers_without_claiming_the_hypothesis(tmp_path: Path):
    results = tmp_path / "results"
    evidence.configure(delivery_policy="descriptive")
    assert _audit(results, {"proposed_T1": _cell(_pattern(40), budget=625),
                            "baseline_T1": _cell(_pattern(30), budget=650)}) == "passed"  # warn, not error
    result = evidence.verify(results)
    assert result["ok"] and not result["primary_hypothesis_verified"]
    assert any("missing evidence is not a null result" in item for item in result["limitations"])
    assert "NOT verified" in evidence.writing_brief(result)


# --------------------------------------------------------------------------- task 2: complete primary metric
def test_one_missing_primary_value_does_not_shrink_the_sample():
    gap = _records([1, 1, 0, 1, 0])
    del gap[2]["answer_em"]  # 5 items, one with no primary value and no recorded reason
    proposed = _cell([], records=gap)
    baseline = _cell([0, 1, 0, 0, 0])
    stats = rigor_stats.compute({"proposed": proposed, "baseline": baseline}, primary=["answer_em"])
    (entry,) = stats.comparisons
    assert entry["status"] == "unverified" and "1 of 5 items lack `answer_em`" in entry["reason"]
    assert stats.coverage["proposed"]["answer_em"] == {"field": "answer_em", "n_items": 5, "n_scored": 4,
                                                       "scored_zero": 0, "missing": {"missing": 1}, "complete": False}
    assert not stats.pairs  # never computed over the 4 scored items
    # even the protocol's score-zero rule does not cover an unexplained gap
    stats = rigor_stats.compute({"proposed": proposed, "baseline": baseline}, primary=["answer_em"],
                                missing_rule="score_zero")
    assert stats.comparisons[0]["status"] == "unverified"


def test_unanswered_items_are_scored_by_the_protocol_rule_not_dropped():
    records = _records([1, 1, None, 1, 0])
    records[2]["predicted"] = ""  # unanswered: no primary value, by design
    proposed = _cell([], records=records, top=3 / 5)
    baseline = _cell([0, 1, 0, 0, 1])
    strict = rigor_stats.compute({"proposed": proposed, "baseline": baseline}, primary=["answer_em"])
    assert strict.comparisons[0]["status"] == "unverified" and "{'unanswered': 1}" in strict.comparisons[0]["reason"]
    counted = rigor_stats.compute({"proposed": proposed, "baseline": baseline}, primary=["answer_em"],
                                  missing_rule="score_zero")
    assert counted.comparisons[0]["status"] == "verified" and counted.pairs[0].n_paired == 5
    assert counted.coverage["proposed"]["answer_em"]["scored_zero"] == 1
    md = rigor_stats.markdown(counted)
    assert "4 scored of 5 items, 1 scored 0 by the protocol rule" in md


def test_missing_reasons_are_told_apart():
    assert rigor_stats.missing_reason({"predicted": "", "status": "turn_cap"}) == "unanswered"
    assert rigor_stats.missing_reason({"predicted": "x", "api_error": "HTTP 500"}) == "api_error"
    assert rigor_stats.missing_reason({"predicted": "x", "parse_error": "bad json"}) == "parse_error"
    assert rigor_stats.missing_reason({"predicted": "x"}) == "missing"


def test_protocol_rule_reaches_the_audit(tmp_path: Path):
    results = tmp_path / "results"
    records = _records(_pattern(40))
    records[0]["predicted"], records[0]["api_error"] = "", "timeout"
    del records[0]["answer_em"]
    proposed = _cell([], records=records, top=39 / 60)
    evidence.configure(missing_primary_rule="score_zero")
    assert _audit(results, {"proposed_T1": proposed, "baseline_T1": _cell(_pattern(30))}) == "passed"
    assert evidence.load_protocol(results)["missing_primary_rule"] == "score_zero"
    assert json.loads((results / "statistics.json").read_text(encoding="utf-8"))["coverage"]["proposed_T1"]["answer_em"][
        "missing"] == {"api_error": 1}


# --------------------------------------------------------------------------- task 4: evidence contract
def test_metrics_edited_after_the_audit_fail_acceptance(tmp_path: Path):
    results = tmp_path / "results"
    assert _audit(results, {"proposed_T1": _cell(_pattern(40)), "baseline_T1": _cell(_pattern(30))}) == "passed"
    assert evidence.verify(results)["ok"]
    path = results / "proposed_T1.metrics.json"
    data = json.loads(path.read_text())
    data["answer_em"] = 0.99
    path.write_text(json.dumps(data))
    result = evidence.verify(results)
    assert not result["ok"] and "RESULT_CHANGED" in _codes(result)
    record = runner.write_acceptance(tmp_path, results, pipeline_status="complete", paper_check=FinalCheck("passed"))
    assert record["deliverable"] is False and record["checks"]["evidence_passed"] is False


def test_an_audit_under_an_old_protocol_does_not_accept_a_new_one(tmp_path: Path):
    results = tmp_path / "results"
    assert _audit(results, {"proposed_T1": _cell(_pattern(40)), "baseline_T1": _cell(_pattern(30))}) == "passed"
    old = evidence.load_protocol(results)["protocol_id"]
    new = execution_audit.freeze_protocol(results, design_text=DESIGN + "- Metric f1\n", plan_metrics=[],
                                          planned=["proposed_T1", "baseline_T1", "ablate_T1"])
    assert new["supersedes"] == old and (evidence.evidence_dir(results) / "protocols" / f"{old}.json").is_file()
    result = evidence.verify(results)
    assert not result["ok"] and "PROTOCOL_CHANGED" in _codes(result)
    # a design changed after its protocol was frozen is caught too
    assert "DESIGN_CHANGED" in _codes(evidence.verify(results, design_text="- Metric something_else"))


def test_reruns_are_new_versions_and_history_is_kept(tmp_path: Path):
    results = tmp_path / "results"
    cells = {"proposed_T1": _cell(_pattern(40)), "baseline_T1": _cell(_pattern(30))}
    _audit(results, cells)
    _audit(results, {**cells, "proposed_T1": _cell(_pattern(41))})
    folder = evidence.evidence_dir(results) / "cells" / "proposed_T1"
    assert sorted(p.name for p in folder.glob("v*.metrics.json")) == ["v1.metrics.json", "v2.metrics.json"]
    history = [json.loads(line) for line in (folder / "history.jsonl").read_text().splitlines()]
    assert [h["version"] for h in history] == [1, 2] and history[0]["metrics_sha256"] != history[1]["metrics_sha256"]
    assert len(list((evidence.evidence_dir(results) / "executions").glob("*.json"))) == 2
    assert evidence.verify(results)["ok"]


def test_a_crashed_audit_invalidates_earlier_evidence(tmp_path: Path):
    results = tmp_path / "results"
    _audit(results, {"proposed_T1": _cell(_pattern(40)), "baseline_T1": _cell(_pattern(30))})
    execution_audit.write_unverified(SimpleNamespace(plan=None), RuntimeError("boom"), results=results)
    result = evidence.verify(results)
    assert not result["ok"] and "AUDIT_NOT_PASSED" in _codes(result)


def test_failed_required_cell_blocks_even_if_other_cells_pass(tmp_path: Path):
    results = tmp_path / "results"
    verdict = _audit(results, {"proposed_T1": _cell(_pattern(40)), "baseline_T1": _cell(_pattern(30)),
                               "ablate_T1": None})
    assert verdict == "failed"
    result = evidence.verify(results)
    assert {"CELL_NOT_COMPLETED", "AUDIT_NOT_PASSED"} <= _codes(result)
    assert "CELL_FAILED" in json.dumps(json.loads((results / "audit.json").read_text(encoding="utf-8"))["blocking"])


# --------------------------------------------------------------------------- revisions
def _revision(tmp_path: Path, *, cap: int = 3, **opt_kw) -> tuple[revision_state.RevisionState, Path]:
    """A finished run with verified evidence, then an open revision on it."""
    exp = tmp_path / "experiments" / "r1"
    results, code = exp / "results", exp / "generated_code"
    code.mkdir(parents=True)
    (code / "run.py").write_text("print('v1')", encoding="utf-8")
    assert _audit(results, {"proposed_T1": _cell(_pattern(40)), "baseline_T1": _cell(_pattern(30))}) == "passed"
    opts = runner.PaperRunOptions(run_dir=tmp_path, topic="t", **opt_kw)
    settings = revision_state.settings_of(opts)
    time.sleep(1.1)  # the revision opens strictly after the pre-revision execution
    state = revision_state.RevisionState(index=1, start_round=10, max_new_cells=cap, settings=settings,
                                         identity=revision_state.identity_of(settings))
    revision_state.build_manifest(results, code, state.folder(tmp_path))
    revision_state.save(state, tmp_path)
    return state, results


def _design_of(results: Path) -> tuple[str, Path]:
    path = results.parent / "design" / "experiment_design.md"
    return path.read_text(encoding="utf-8"), path


def _revision_execution(tmp_path: Path, state, results: Path, new: dict[str, dict | None], *,
                        ran: list[str] | None = None, design: str | None = None, code_dir: str = "") -> str:
    """One execution inside the revision, through the real guard + audit wrapper. ``ran`` collects
    the cells the stand-in executor was actually asked to run; ``design`` replaces the design first."""
    if design is not None:
        (results.parent / "design" / "experiment_design.md").write_text(design, encoding="utf-8")
    manifest = revision_state.load_manifest(state.folder(tmp_path))
    guard = revision_state.ExecutionGuard(tmp_path, state, manifest)
    revision_state.install_execution_guard(guard)
    names = ["proposed_T1", "baseline_T1", *new]

    def original(agent, inputs):
        variants = []
        for v in inputs.implementation.variants:
            if ran is not None:
                ran.append(v.name)
            payload = new[v.name]
            if payload is None:
                variants.append(SimpleNamespace(name=v.name, metrics=None, process_status="failed", exit_code=1))
                continue
            (results / f"{v.name}.metrics.json").write_text(json.dumps(payload), encoding="utf-8")
            variants.append(SimpleNamespace(name=v.name, metrics=payload, process_status="completed", exit_code=0))
        return SimpleNamespace(result=SimpleNamespace(variants=variants, notes="", status="completed"))

    inputs = SimpleNamespace(plan=SimpleNamespace(metrics=[], run_id="r1"),
                             implementation=SimpleNamespace(workspace_dir=code_dir, variants=[
                                 SimpleNamespace(name=n, invocation=["x"]) for n in names]))
    import unittest.mock as mock

    with mock.patch.object(execution_audit, "_results_dir", lambda plan: results), \
            mock.patch.object(execution_audit, "_read_design", lambda plan: _design_of(results)):
        output = execution_audit.run_audited(original, None, inputs)
    return output.result.notes


def test_failed_cell_a_then_successful_cell_b_does_not_accept_the_revision(tmp_path: Path):
    state, results = _revision(tmp_path)
    notes = _revision_execution(tmp_path, state, results, {"abl_a_T1": None, "abl_b_T1": _cell(_pattern(35))})
    assert "CELL_FAILED" in notes
    # the next execution plans only B (A silently dropped from the design after it failed)
    _revision_execution(tmp_path, state, results, {"abl_b_T1": _cell(_pattern(36))})
    assert json.loads((results / "audit.json").read_text(encoding="utf-8"))["verdict"] == "passed"  # the latest audit alone passes
    ok, detail = revision_state.evidence_status(tmp_path, state)
    assert not ok and "REVISION_CELL_DROPPED" in detail and "abl_a_T1" in detail
    # repairing A (re-run until it passes) accepts it
    _revision_execution(tmp_path, state, results, {"abl_a_T1": _cell(_pattern(33)), "abl_b_T1": _cell(_pattern(36))})
    assert revision_state.evidence_status(tmp_path, state)[0]


def test_pre_revision_evidence_does_not_count_for_the_revision(tmp_path: Path):
    state, results = _revision(tmp_path)
    state.executed_new_cells = ["abl_T1"]  # claimed, but no execution under this revision
    ok, detail = revision_state.evidence_status(tmp_path, state)
    assert not ok and "NOT_THIS_REVISION" in detail


def test_rerun_limit_is_separate_from_the_new_cell_cap(tmp_path: Path):
    state, results = _revision(tmp_path, cap=5)
    state.max_cell_executions = 2
    for _ in range(2):
        _revision_execution(tmp_path, state, results, {"abl_T1": None})
    notes = _revision_execution(tmp_path, state, results, {"abl_T1": _cell(_pattern(33))})
    assert "CELL_RERUN_LIMIT" in notes and state.cell_executions == {"abl_T1": 2}
    assert "abl_T1" in state.refused_cells and len(state.executed_new_cells) <= state.max_new_cells


# --------------------------------------------------------------------------- task 3: resume keeps controls
def _no_installs(monkeypatch, seen: dict) -> None:
    from jiuwenswarm.agents.harness.common.paper_pipeline import budget_guard, code_agent_budget, research_protocol

    monkeypatch.setattr(code_agent_budget, "install_code_agent_iteration_fix", lambda n: 80)
    monkeypatch.setattr(research_protocol, "install_research_protocol", lambda: [])
    monkeypatch.setattr(revision, "install_revision_protocol", lambda: [])
    monkeypatch.setattr(budget_guard, "install_budget_guard", lambda g: seen.setdefault("guards", []).append(g))
    monkeypatch.setattr(budget_guard, "install_execution_budget_check", lambda g: None)
    monkeypatch.setattr(budget_guard, "spend_report", lambda *a, **k: {})


def test_hard_budget_survives_an_interrupted_run_resumed_without_it(tmp_path: Path, monkeypatch):
    seen: dict = {}
    _no_installs(monkeypatch, seen)

    async def interrupted(opts):
        raise RuntimeError("process killed")

    async def resumed(opts):
        seen["resumed"] = True

    monkeypatch.setattr(runner, "fresh_run", interrupted)
    monkeypatch.setattr(runner, "resume_run", resumed)
    with pytest.raises(RuntimeError):
        asyncio.run(runner.run(runner.PaperRunOptions(run_dir=tmp_path, topic="t", budget_hard=13.0,
                                                      budget_soft=10.0), resume=False))
    asyncio.run(runner.run(runner.PaperRunOptions(run_dir=tmp_path, topic="t"), resume=True))  # no budget args
    assert [(g.hard_yuan, g.soft_yuan) for g in seen["guards"]] == [(13.0, 10.0), (13.0, 10.0)]
    # an explicit override is applied and recorded
    asyncio.run(runner.run(runner.PaperRunOptions(run_dir=tmp_path, topic="t", budget_hard=20.0), resume=True))
    assert seen["guards"][-1].hard_yuan == 20.0
    saved = json.loads((tmp_path / runner.RUN_SETTINGS_FILE).read_text())
    assert saved["overrides"][-1]["from"] == 13.0 and saved["overrides"][-1]["to"] == 20.0


def test_revision_resume_restores_budgets_and_evidence_options(tmp_path: Path):
    state, _ = _revision(tmp_path, budget_hard=13.0, budget_soft=10.0, delivery_policy="descriptive")
    fresh = runner.PaperRunOptions(run_dir=tmp_path, topic="t")
    assert revision_state.restore_settings(fresh, revision_state.find_open(tmp_path), tmp_path) == []
    assert (fresh.budget_hard, fresh.budget_soft, fresh.delivery_policy) == (13.0, 10.0, "descriptive")


def test_legacy_revision_without_recorded_budget_is_refused(tmp_path: Path):
    state, _ = _revision(tmp_path)
    for key in revision_state.BUDGET_SETTINGS + revision_state.EVIDENCE_SETTINGS:
        state.settings.pop(key)
    revision_state.save(state, tmp_path)
    with pytest.raises(runner.PaperRunError, match="budget limits were persisted"):
        revision_state.restore_settings(runner.PaperRunOptions(run_dir=tmp_path, topic="t"),
                                        revision_state.find_open(tmp_path), tmp_path)
    explicit = runner.PaperRunOptions(run_dir=tmp_path, topic="t", budget_hard=12.0)
    changes = revision_state.restore_settings(explicit, revision_state.find_open(tmp_path), tmp_path)
    assert {"setting": "budget_hard", "from": "unrecorded", "to": 12.0} in [
        {k: c[k] for k in ("setting", "from", "to")} for c in changes]
    # recorded now: the next resume restores it without arguments
    again = runner.PaperRunOptions(run_dir=tmp_path, topic="t")
    revision_state.restore_settings(again, revision_state.find_open(tmp_path), tmp_path)
    assert again.budget_hard == 12.0


def test_failed_acceptance_keeps_the_revision_and_its_guard_active(tmp_path: Path):
    state, results = _revision(tmp_path, cap=1)
    _revision_execution(tmp_path, state, results, {"abl_T1": _cell(_pattern(35))})
    record = runner.write_acceptance(tmp_path, results, pipeline_status="complete",
                                     paper_check=FinalCheck("failed", ["pdflatex exited with 1"]), revision=state)
    assert record["deliverable"] is False and record["checks"]["evidence_passed"]
    reopened = revision_state.find_open(tmp_path)
    assert reopened is not None and reopened.status == revision_state.NEEDS_REPAIR
    assert revision_state.active_guard() is not None  # not released
    # the cap and frozen cells still bind after a restart
    guard = revision_state.ExecutionGuard(tmp_path, reopened, revision_state.load_manifest(reopened.folder(tmp_path)))
    plan = guard.plan(["proposed_T1", "baseline_T1", "abl_T1", "extra_T1"])
    assert plan.reference == ["proposed_T1", "baseline_T1"] and plan.refused == ["extra_T1"]
    assert "final PDF check failed" in runner.repair_followup(reopened)
    record = runner.write_acceptance(tmp_path, results, pipeline_status="complete", paper_check=FinalCheck("passed"),
                                     revision=reopened)
    assert record["deliverable"] and revision_state.find_open(tmp_path) is None


def test_accepted_rolled_back_and_abandoned_revisions_are_not_resumed(tmp_path: Path):
    state, _ = _revision(tmp_path)
    for status in (revision_state.ACCEPTED, revision_state.ROLLED_BACK, revision_state.ABANDONED):
        state.set_status(status, "test")
        revision_state.save(state, tmp_path)
        assert revision_state.find_open(tmp_path) is None
    state.status = revision_state.NOT_ACCEPTED  # written automatically by older versions
    revision_state.save(state, tmp_path)
    assert revision_state.find_open(tmp_path) is not None  # read as needing repair
    state.acceptance["rolled_back"] = {"restored_from": "x"}
    revision_state.save(state, tmp_path)
    assert revision_state.find_open(tmp_path) is None


# --------------------------------------------------------------------------- task 5: review responses
REPLICATION_DESIGN = DESIGN + """
```experiment-protocol
{"item_sets": {"m2": {"same_as": ""}},
 "review_comparisons": [{"item": "R-0", "a": "m2__proposed_T1", "b": "m2__baseline_T1"},
                        {"item": "R-1", "a": "m2__proposed_T1", "b": "m2__baseline_T1"},
                        {"item": "R-2", "a": "proposed_T1", "b": "baseline_T1"}]}
```
"""


def test_review_response_needs_verified_evidence_from_this_revision(tmp_path: Path):
    state, results = _revision(tmp_path)
    _revision_execution(tmp_path, state, results, {"m2__proposed_T1": _cell(_pattern(41), model="qwen-plus"),
                                                   "m2__baseline_T1": _cell(_pattern(31), model="qwen-plus")},
                        design=REPLICATION_DESIGN)
    paper = results.parent / "paper"
    paper.mkdir()
    (paper / "main.tex").write_text("\\section{Replication}\\label{sec:rep}\\section{Limitations} "
                                    "we mention Results in passing", encoding="utf-8")
    items = [{"id": f"R-{i}", "text": f"item {i}"} for i in range(5)]

    def respond(*entries):
        (paper / revision.RESPONSE_FILE).write_text(json.dumps({"items": list(entries)}), encoding="utf-8")
        return revision.check_response(items, paper, results, revision=state)

    check = respond(
        {"id": "R-0", "disposition": "new_experiment", "evidence": ["m2__proposed_T1", "m2__baseline_T1"],
         "conditions": {"model": "qwen-plus", "setting": "m2"}, "paper_location": "sec:rep"},
        {"id": "R-1", "disposition": "new_experiment", "evidence": ["m2__proposed_T1", "ds2__proposed_T1"],
         "paper_location": "Replication"},  # one cited cell does not exist
        {"id": "R-2", "disposition": "new_experiment", "evidence": ["proposed_T1"], "paper_location": "sec:rep"},
        {"id": "R-3", "disposition": "limitation", "paper_location": "Limitations"},
        {"id": "R-4", "disposition": "rewritten", "paper_location": "Results"},  # only a phrase in the text
    )
    states = {r["id"]: r["state"] for r in check["resolved"]}
    assert states == {"R-0": "verified_resolved", "R-3": "addressed"}
    why = {u["id"]: u["why"] for u in check["unresolved"]}
    assert "ds2__proposed_T1" in why["R-1"] and "not executed in this revision" in why["R-2"]
    assert "not a \\label or section" in why["R-4"]
    assert check["counts"] == {"verified_resolved": 1, "addressed": 1, "unresolved": 3}
    wrong = respond({"id": "R-0", "disposition": "new_experiment", "evidence": ["m2__proposed_T1"],
                     "conditions": {"model": "qwen-max"}, "paper_location": "sec:rep"})
    assert "claims 'qwen-max'" in wrong["unresolved"][0]["why"]


# --------------------------------------------------------------------------- resources + candidates
def test_spend_report_counts_failed_attempts_reruns_and_reused_cells(tmp_path: Path):
    from jiuwenswarm.agents.harness.common.paper_pipeline import budget_guard

    state, results = _revision(tmp_path)
    _revision_execution(tmp_path, state, results, {"abl_T1": None})
    _revision_execution(tmp_path, state, results, {"abl_T1": _cell(_pattern(33))})
    report = budget_guard.spend_report(tmp_path)
    history = report["experiments"]["results_dirs"][str(results)]["executions"]
    assert history["cells"]["abl_T1"] == {"executions": 2, "failed": 1}
    assert history["failed_attempts"] == 1 and history["reruns"] == 1
    assert history["reused_cells"] == ["baseline_T1", "proposed_T1"]
    assert "1 failed experiment attempts: usage unknown" in report["totals"]["unknown"]
    assert "not interrupted" in report["enforcement"] and report["totals"]["price_quality"]["experiments"] == "unknown"


def test_candidate_choice_keeps_deliverable_and_better_apart():
    before = {"overall_mean": 5.0, "dimension_mean": {}, "reviews": []}
    worse = revision.compare_reviews(before, {"overall_mean": 4.5, "dimension_mean": {}, "reviews": []},
                                     deliverable=True)
    assert worse["prefer"] == "previous" and worse["deliverable"] is True and worse["improved"] is False
    blind = revision.compare_reviews(before, {"overall_mean": None, "dimension_mean": {}, "reviews": []},
                                     deliverable=True)
    assert blind["prefer"] == "previous" and blind["improved"] is None and "no valid comparison" in blind["decision_basis"]


def test_model_free_demo_runs_the_full_repair_cycle(tmp_path: Path):
    import importlib.util

    script = Path(__file__).resolve().parents[5] / "scripts" / "paper_evidence_demo.py"
    spec = importlib.util.spec_from_file_location("paper_evidence_demo", script)
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    run_dir = demo.main(["--out", str(tmp_path / "demo")])
    acceptance = json.loads((run_dir / "acceptance.json").read_text(encoding="utf-8"))
    assert acceptance["deliverable"] and acceptance["revision"]["verified_resolved_items"] == ["R-demo0001"]
    states = [h["to"] for h in revision_state.all_revisions(run_dir)[0].status_history]
    assert states == [revision_state.NEEDS_REPAIR, revision_state.ACCEPTED]
