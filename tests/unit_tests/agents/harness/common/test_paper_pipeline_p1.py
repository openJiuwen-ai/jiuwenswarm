"""Regression tests for the P1 items (no model calls): statistics roles / Holm families and their
wording downstream, spend accounting and budget guards, review-item closure and candidate choice."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.paper_pipeline import (
    budget_guard,
    revision,
    revision_state,
    rigor_stats,
    runner,
)


def _cell(correct: list[int], *, post_hoc=False, budget=325, model="m1", answers=True) -> dict:
    records = [{"qid": f"q{i}", "answer_em": c, "model": model, "budget_tokens": budget, "cost": 10.0 + i * c,
                **({"predicted": f"a{c}"} if answers else {})} for i, c in enumerate(correct)]
    return {"answer_em": sum(correct) / len(correct), "cost": sum(r["cost"] for r in records) / len(records),
            "budget_tokens": budget, "post_hoc": post_hoc, "config": {"model": model, "item_list_hash": "h"},
            "per_question": records}


def _pattern(good: int, n: int = 60) -> list[int]:
    return [1] * good + [0] * (n - good)


# --------------------------------------------------------------------------- 6. statistics
def test_roles_and_holm_families_are_separate():
    variants = {"proposed_T1": _cell(_pattern(40)), "base_T1": _cell(_pattern(30)),
                "ablate_T1": _cell(_pattern(39), post_hoc=True),
                "m2__proposed_T1": _cell(_pattern(42), model="m2"), "m2__base_T1": _cell(_pattern(30), model="m2")}
    stats = rigor_stats.compute(variants, primary=["answer_em"])
    roles = {(p.a, p.b): p.role for p in stats.pairs}
    assert roles == {("proposed_T1", "base_T1"): "confirmatory", ("proposed_T1", "ablate_T1"): "exploratory",
                     ("m2__proposed_T1", "m2__base_T1"): "exploratory"}
    confirm = next(p for p in stats.pairs if p.role == "confirmatory")
    assert confirm.p_holm == confirm.p_value  # a family of one is not inflated by exploratory pairs
    data = stats.to_dict()
    assert data["families"] == {"answer_em/confirmatory": 1, "answer_em/exploratory": 2}
    assert data["method"]["bootstrap_resamples"] == rigor_stats.DEFAULT_RESAMPLES
    assert "not an exact test" in data["method"]["paired_test_continuous"]
    assert "not a formal equivalence" in data["method"]["bounded_verdict"]


def test_continuous_test_is_named_monte_carlo_and_holm_reaches_metrics():
    variants = {"proposed": _cell(_pattern(40)), "base": _cell(_pattern(30))}
    stats = rigor_stats.compute(variants, primary=["answer_em", "cost"])
    tests = {p.metric: p.test for p in stats.pairs}
    assert tests == {"answer_em": "exact_mcnemar", "cost": rigor_stats.SIGN_FLIP_TEST}
    rigor_stats.annotate(variants, stats)
    assert "answer_em_diff_vs_base_p_holm" in variants["proposed"]
    md = rigor_stats.markdown(stats)
    assert "p (Holm, family)" in md and "not an exact test" in md and "confirmatory" in md


def test_unknown_unanswered_is_not_zero():
    variants = {"proposed": _cell(_pattern(40), answers=False), "base": _cell(_pattern(30))}
    stats = rigor_stats.compute(variants, primary=["answer_em"])
    assert stats.unanswered == {"proposed": None, "base": 0}
    assert stats.to_dict()["unanswered"]["proposed"] == "unknown"
    assert "`proposed` unknown" in rigor_stats.markdown(stats)
    rigor_stats.annotate(variants, stats)
    assert "unanswered_items" not in variants["proposed"] and variants["base"]["unanswered_items"] == 0


def test_downstream_prompts_name_holm_and_roles():
    from jiuwenswarm.agents.harness.common.paper_pipeline import research_protocol

    for text in (research_protocol.REFLECTION_PROTOCOL, research_protocol.REPORTING_PROTOCOL):
        assert "p_holm" in text and "exploratory" in text
    assert "never \"exact\"" in research_protocol.REPORTING_PROTOCOL


# --------------------------------------------------------------------------- 7. spend + budget
def _ledger(path: Path, rows: list[dict]) -> Path:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps({"ts": "2026-09-28T12:00:00+00:00", "model_call": row}) + "\n")
    return path


def test_ledger_keeps_unknown_and_unpriced_apart(tmp_path: Path):
    ledger = _ledger(tmp_path / "model_calls.jsonl", [
        {"model": "deepseek-flash", "status": "succeeded", "tokens": {"input": 1_000_000, "cache_hit": 0, "output": 0}},
        {"model": "deepseek-flash", "status": "failed", "tokens": {"input": None, "output": None, "cache_hit": None}},
        {"model": "deepseek-flash", "status": "failed", "tokens": {"input": 1_000_000, "cache_hit": 0, "output": 0}},
        {"model": "qwen-x", "status": "succeeded", "tokens": {"input": 1_000_000, "cache_hit": 0, "output": 0}},
    ])
    cost, tokens = budget_guard.ledger_cost(ledger)
    assert tokens["unknown_calls"] == 1 and tokens["failed_calls_with_usage"] == 1
    assert tokens["unpriced_models"] == ["qwen-x"]
    assert tokens["estimated_yuan"] == pytest.approx(4.5)  # off-peak, priced as the dearest model
    assert cost == pytest.approx(1.0 + 1.0 + 4.5)  # the call without usage is not priced as 0 — it is excluded


def test_spend_report_labels_every_source(tmp_path: Path):
    _ledger(tmp_path / "model_calls.jsonl", [
        {"model": "deepseek-flash", "status": "failed", "tokens": {"input": None, "output": None}}])
    results = tmp_path / "experiments" / "r1" / "results"
    results.mkdir(parents=True)
    (results / "a.metrics.json").write_text(json.dumps({"per_question": [
        {"qid": "q0", "prompt_tokens": 100, "completion_tokens": 5, "model": "qwen-flash"}]}), encoding="utf-8")
    (results / "b.metrics.json").write_text(json.dumps({"per_question": [{"qid": "q0"}]}), encoding="utf-8")
    panel = tmp_path / "experiments" / "r1" / "paper" / "review_panel"
    panel.mkdir(parents=True)
    (panel / "review_panel.json").write_text(json.dumps({"reviews": [
        {"model": "k", "prompt_tokens": 10, "completion_tokens": 2}, {"model": "k", "prompt_tokens": None}]}))
    report = budget_guard.spend_report(tmp_path, balance_before=20.0, balance_after=18.5)
    usage = report["experiments"]["results_dirs"][str(results)]
    assert usage["prompt_tokens"] == 100 and usage["variants_without_token_fields"] == ["b"]
    assert report["review_panels"][0]["unknown_calls"] == 1
    assert report["account_balance"]["delta"] == 1.5 and "not this task's exact cost" in report["account_balance"]["note"]
    assert report["pipeline_ledger"]["unknown_cost_calls"] == 1
    assert (tmp_path / "spend_report.json").is_file()


def _opts(**kw):
    return runner.PaperRunOptions(run_dir=Path("."), topic="t", **kw)


def test_any_single_budget_option_installs_the_guard():
    assert runner.budget_limits(_opts()) is None
    assert runner.budget_limits(_opts(budget_hard=13.0)) == (float("inf"), 13.0, None, None)
    assert runner.budget_limits(_opts(balance_hard_floor=13.0)) == (float("inf"), float("inf"), None, 13.0)
    assert runner.budget_limits(_opts(budget_soft=10.0)) == (10.0, 13.0, None, None)
    assert runner.budget_limits(_opts(balance_floor=20.0)) == (float("inf"), float("inf"), 20.0, 10.0)
    with pytest.raises(runner.PaperRunError):
        runner.budget_limits(_opts(budget_soft=10.0, budget_hard=5.0))
    with pytest.raises(runner.PaperRunError):
        runner.budget_limits(_opts(balance_floor=10.0, balance_hard_floor=12.0))


def test_hard_limit_is_checked_before_each_variant(tmp_path: Path, monkeypatch):
    pytest.importorskip("openjiuwen.rsi.artifact_rsi.paper_opt.auto_research")
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.experiment_execution import agent as module

    cls = module.ExperimentExecutionAgent
    monkeypatch.setattr(cls, "_run_variant", cls.__dict__["_run_variant"])  # restored after the test
    calls = []
    monkeypatch.setattr(cls, "_run_variant", classmethod(lambda klass, variant, **kw: calls.append(variant) or "ran"))
    balance = {"now": 20.0}
    guard = budget_guard.BudgetGuard(tmp_path / "none.jsonl", balance_reader=lambda: balance["now"], hard_floor=13.0)
    budget_guard.install_execution_budget_check(guard)
    budget_guard.install_execution_budget_check(guard)  # idempotent: one check, not two layers
    assert cls._run_variant("v1") == "ran"
    balance["now"] = 12.0
    with pytest.raises(budget_guard.BudgetExceeded):
        cls._run_variant("v2")
    assert calls == ["v1"]


def test_soft_notice_says_when_it_is_an_estimate(tmp_path: Path):
    ledger = _ledger(tmp_path / "l.jsonl", [
        {"model": "qwen-x", "status": "succeeded", "tokens": {"input": 2_000_000, "cache_hit": 0, "output": 0}},
        {"model": "deepseek-flash", "status": "failed", "tokens": {"input": None, "output": None}}])
    notice = budget_guard.BudgetGuard(ledger, soft_yuan=1.0).notice()
    assert "1 calls without usage are not included" in notice and "qwen-x" in notice


# --------------------------------------------------------------------------- 5. review closure
REVIEW = revision.ReviewInput("panel", 4.2, {"Claims_Support": 0.0},
                              ["The fixed-quota ablation matches the full method at all tiers.",
                               "Only one dataset and one model are evaluated."],
                              questions="1. Can you replicate on a second dataset?")


def test_review_items_have_stable_ids_and_reach_the_brief():
    items = revision.review_items(REVIEW)
    assert len(items) == 3 and items[0]["id"] == revision.item_id("the FIXED-quota ablation matches the full "
                                                                  "method at all tiers")
    brief = revision.build_brief(REVIEW, max_new_cells=4)
    assert all(f"`{i['id']}`" in brief for i in items) and revision.RESPONSE_FILE in brief
    assert "No new variants" in revision.build_brief(REVIEW, max_new_cells=0, writing_only=True)


def test_response_check_requires_disposition_location_and_evidence(tmp_path: Path):
    items = revision.review_items(REVIEW)
    paper, results = tmp_path / "paper", tmp_path / "results"
    paper.mkdir()
    results.mkdir()
    (paper / "main.tex").write_text("\\section{Ablations}\\label{sec:abl} \\section{Limitations}", encoding="utf-8")
    (results / "ablate_quota_T1.metrics.json").write_text("{}", encoding="utf-8")
    assert len(revision.check_response(items, paper, results)["unresolved"]) == 3  # no response file
    (paper / revision.RESPONSE_FILE).write_text(json.dumps({"items": [
        {"id": items[0]["id"], "disposition": "new_experiment", "evidence": ["ablate_quota_T1"],
         "paper_location": "sec:abl"},
        {"id": items[1]["id"], "disposition": "limitation", "paper_location": "Limitations"},
        {"id": items[2]["id"], "disposition": "new_experiment", "evidence": ["ds2__proposed_T1"],
         "paper_location": "sec:abl"},
        {"id": "R-unknown", "disposition": "rewritten", "paper_location": "Limitations"},
    ]}), encoding="utf-8")
    check = revision.check_response(items, paper, results)
    assert [r["id"] for r in check["resolved"]] == [items[0]["id"], items[1]["id"]]
    assert "no metrics file" in check["unresolved"][0]["why"] and check["unknown_ids"] == ["R-unknown"]


def test_candidate_choice_is_not_by_mean_alone():
    before = {"overall_mean": 5.0, "dimension_mean": {"Claims_Support": 0.33, "Writing_Clarity": 0.67}, "reviews": []}
    after = {"overall_mean": 5.5, "dimension_mean": {"Claims_Support": 0.33, "Writing_Clarity": -0.33},
             "reviews": [{"ok": True, "top_weaknesses": ["Still only one dataset and one model are evaluated here."]}]}
    decision = revision.compare_reviews(before, after, items=revision.review_items(REVIEW), deliverable=True)
    assert decision["prefer"] == "previous" and "Writing_Clarity dropped" in decision["reasons"][0]
    assert [s["text"] for s in decision["still_raised"]] == ["Only one dataset and one model are evaluated."]
    after["dimension_mean"]["Writing_Clarity"] = 0.67
    assert revision.compare_reviews(before, after, deliverable=False)["prefer"] == "previous"
    assert revision.compare_reviews(before, after, deliverable=True,
                                    response={"unresolved": [{"id": "x"}]})["prefer"] == "previous"
    assert revision.compare_reviews(before, after, deliverable=True, response={"unresolved": []})["prefer"] == "revised"


def test_rollback_keeps_both_candidates(tmp_path: Path):
    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "main.tex").write_text("new", encoding="utf-8")
    (paper / "main.pdf").write_text("pdf", encoding="utf-8")
    snapshot = revision.snapshot_paper(paper)
    (paper / "main.tex").write_text("revised", encoding="utf-8")
    rejected = revision.rollback_paper(paper, snapshot)
    assert (paper / "main.tex").read_text() == "new" and (rejected / "main.tex").read_text() == "revised"


def test_writing_only_revision_runs_nothing_and_needs_only_a_report(tmp_path: Path):
    state = revision_state.RevisionState(index=1, start_round=5, max_new_cells=0, mode="writing_only")
    guard = revision_state.ExecutionGuard(tmp_path, state, {"cells": [{"name": "proposed_T1"}]})
    assert guard.plan(["proposed_T1", "new_T1"]).refused == ["new_T1"]
    rows = revision.writing_only_rows("reporting:7:1", (True, "unchanged"))
    assert rows[revision.REQ_REPORT][1] == "reporting:7:1" and revision.REQ_EXECUTE not in rows
    assert revision.writing_only_rows("reporting:7:1", (False, "changed"))[revision.REQ_REPORT][1] is None
    folder = state.folder(tmp_path)
    folder.mkdir(parents=True)
    (folder / revision_state.MANIFEST_FILE).write_text(json.dumps({"cells": [], "results_dir": str(tmp_path)}))
    assert revision_state.evidence_status(tmp_path, state)[0]


def test_writing_only_gate_with_agent_core(monkeypatch):
    pytest.importorskip("openjiuwen.rsi.artifact_rsi.paper_opt.auto_research")
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.pipeline import manager as manager_mod
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.pipeline import transitions

    monkeypatch.setattr(transitions, "sync_host_requirements", lambda state: None)
    monkeypatch.setattr(manager_mod, "sync_host_requirements", transitions.sync_host_requirements)
    revision.install_revision_gate(5, evidence=lambda: (True, "ok"), writing_only=True)
    report = SimpleNamespace(module="reporting", outcome="succeeded", round_index=6, report_id="reporting:6:1")
    state = SimpleNamespace(reports=[report],
                            task_state=SimpleNamespace(requirements=[], latest_execution_status="failed"))
    manager_mod.sync_host_requirements(state)
    assert {r.id: r.status for r in state.task_state.requirements} == {revision.REQ_REPORT: "completed",
                                                                       revision.REQ_EVIDENCE: "completed"}


def test_acceptance_needs_resolved_review_items(tmp_path: Path):
    from jiuwenswarm.agents.harness.common.paper_pipeline.latex_check import FinalCheck

    exp = tmp_path / "experiments" / "r1"
    results, paper = exp / "results", exp / "paper"
    results.mkdir(parents=True)
    paper.mkdir()
    (paper / "main.tex").write_text("\\section{Limitations}", encoding="utf-8")
    state = revision_state.RevisionState(index=1, start_round=1, max_new_cells=0, mode="writing_only",
                                         review_items=revision.review_items(REVIEW), paper_snapshot="prev")
    revision_state.build_manifest(results, None, state.folder(tmp_path))
    revision_state.save(state, tmp_path)
    (results / "audit.json").write_text(json.dumps({"verdict": "passed", "audited_at": time.time() + 5}))
    record = runner.write_acceptance(tmp_path, results, pipeline_status="complete",
                                     paper_check=FinalCheck("passed"), revision=state)
    assert record["deliverable"] is False and record["checks"]["review_items_resolved"] is False
    assert record["revision"]["candidates"] == {"previous": "prev", "revised": str(paper)}
    assert (state.folder(tmp_path) / "response_check.json").is_file()


def test_optional_paper_steps_never_skip_the_final_check(tmp_path: Path, monkeypatch):
    import subprocess

    from jiuwenswarm.agents.harness.common.paper_pipeline import iclr_template, latex_check, results_table

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("pdflatex", 300)

    monkeypatch.setattr(iclr_template, "convert_to_iclr", timeout)
    monkeypatch.setattr(results_table, "compact_paper_tables", timeout)
    monkeypatch.setattr(latex_check, "final_check", lambda *a, **k: latex_check.FinalCheck("passed"))
    paper = tmp_path / "paper"
    paper.mkdir()
    check = runner.post_process_paper(tmp_path, paper, runner.PaperRunOptions(run_dir=tmp_path, topic="t"))
    assert check.status == "passed"
    events = [json.loads(line) for line in (tmp_path / "resume_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in events] == ["iclr_template", "compact_tables", "latex_final_check"]
    assert "TimeoutExpired" in events[0]["error"] and "TimeoutExpired" in events[1]["error"]
