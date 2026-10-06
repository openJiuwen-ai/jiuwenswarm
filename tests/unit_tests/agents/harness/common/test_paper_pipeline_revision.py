"""Unit tests for review-driven revision (``jiuwenswarm-paper revise``) and the finishing/answering
statistics it relies on (no model calls)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.paper_pipeline import research_protocol, revision, rigor_stats
from jiuwenswarm.agents.harness.common.paper_pipeline.cli import build_parser

AR_REVIEW = {
    "success": True,
    "numerical_score": 5.8,
    "sections": {
        "weaknesses": (
            "- Technical limitations or concerns\n"
            "  - The credited mechanism (pressure-proportional quotas) appears unnecessary: the fixed-quota "
            "ablation is statistically indistinguishable at all tiers.\n"
            "  - Single dataset (150 sampled HotpotQA distractor items), single model, and temperature 0 limit "
            "generality.\n"
        ),
        "questions": "1. Can you replicate the main comparison on a second dataset?",
        "assessment": "I lean toward a borderline rejection.",
        "binary_scores": (
            "- Claims_Support: [+1]  # ...\n- Experimental_Soundness: [0]  # ...\n"
            "- Originality: [-1]  # ...\n- Value_to_Community: [0]  # ..."
        ),
    },
}

PANEL_REVIEW = {
    "overall_mean": 4.17,
    "dimension_mean": {"Claims_Support": 0, "Experimental_Soundness": -0.33},
    "reviews": [
        {"model": "m1", "ok": True, "top_weaknesses": ["Primary contrast confounds ranking and fidelity."]},
        {"model": "m2", "ok": False, "top_weaknesses": ["ignored: failed member"]},
    ],
}


def test_load_review_agentic_reviewer(tmp_path: Path):
    path = tmp_path / "review.json"
    path.write_text(json.dumps(AR_REVIEW), encoding="utf-8")
    review = revision.load_review(path)
    assert review.source == "agentic_reviewer" and review.score == 5.8
    assert review.dimensions == {"Claims_Support": 1.0, "Experimental_Soundness": 0.0, "Originality": -1.0,
                                 "Value_to_Community": 0.0}
    assert len(review.weaknesses) == 2 and "fixed-quota ablation" in review.weaknesses[0]
    assert "second dataset" in review.questions


def test_load_review_panel_skips_failed_members(tmp_path: Path):
    path = tmp_path / "review_panel.json"
    path.write_text(json.dumps(PANEL_REVIEW), encoding="utf-8")
    review = revision.load_review(path)
    assert review.source == "panel" and review.score == 4.17
    assert review.weaknesses == ["[m1] Primary contrast confounds ranking and fidelity."]


def test_brief_carries_review_limits_and_replication_model(tmp_path: Path):
    path = tmp_path / "review.json"
    path.write_text(json.dumps(AR_REVIEW), encoding="utf-8")
    brief = revision.build_brief(revision.load_review(path), max_new_cells=18, replication_model="qwen-plus",
                                 note="Keep the HotpotQA item list.")
    assert brief.startswith("# Revision request")
    assert "At most **18 new variants**" in brief
    assert "REPLICATION_MODEL_NAME=qwen-plus" in brief
    assert "Originality (-1)" in brief and "Claims_Support" not in brief.split("Dimensions not yet positive")[1].splitlines()[0]
    assert "fixed-quota ablation" in brief and "single-change ablation" in brief
    assert "Keep the HotpotQA item list." in brief


def _report(module: str, round_index: int, outcome: str = "succeeded"):
    return SimpleNamespace(module=module, outcome=outcome, round_index=round_index,
                           report_id=f"{module}:{round_index}:1")


def test_revision_gate_needs_new_execution_then_new_report():
    old = [_report("experiment_execution", 5), _report("reporting", 7)]
    assert revision.revision_status(old, 8, "completed") == (None, None)  # pre-revision paper does not count
    after_exec = old + [_report("experiment_execution", 11)]
    assert revision.revision_status(after_exec, 8, "completed") == ("experiment_execution:11:1", None)
    assert revision.revision_status(after_exec, 8, "failed") == (None, None)
    report_before_exec = after_exec + [_report("reporting", 10)]
    assert revision.revision_status(report_before_exec, 8, "completed")[1] is None
    done = after_exec + [_report("reporting", 12, "failed"), _report("reporting", 13)]
    assert revision.revision_status(done, 8, "completed") == ("experiment_execution:11:1", "reporting:13:1")


def test_install_revision_gate_rederives_requirement_rows(monkeypatch):
    pytest.importorskip("openjiuwen.rsi.artifact_rsi.paper_opt.auto_research")
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.pipeline import manager as manager_mod
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.pipeline import transitions

    monkeypatch.setattr(transitions, "sync_host_requirements", lambda state: None)
    monkeypatch.setattr(manager_mod, "sync_host_requirements", transitions.sync_host_requirements)
    revision.install_revision_gate(8)
    state = SimpleNamespace(reports=[_report("experiment_execution", 5)],
                            task_state=SimpleNamespace(requirements=[], latest_execution_status="completed"))
    manager_mod.sync_host_requirements(state)
    rows = {r.id: r for r in state.task_state.requirements}
    assert rows[revision.REQ_EXECUTE].status == "pending" and rows[revision.REQ_REPORT].status == "pending"
    rows[revision.REQ_REPORT].status = "completed"  # a manager state change cannot open the gate
    manager_mod.sync_host_requirements(state)
    assert rows[revision.REQ_REPORT].status == "pending"
    state.reports += [_report("experiment_execution", 10), _report("reporting", 11)]
    manager_mod.sync_host_requirements(state)
    assert all(r.status == "completed" for r in state.task_state.requirements)
    assert revision.gate_start_round(state) == 8


def test_revision_protocol_appends_once(monkeypatch):
    for name in ("DESIGN_PROTOCOL", "CODE_PROTOCOL", "REFLECTION_PROTOCOL", "MANAGER_PROTOCOL", "REPORTING_PROTOCOL"):
        monkeypatch.setattr(research_protocol, name, getattr(research_protocol, name))
    revision.install_revision_protocol()
    revision.install_revision_protocol()
    assert research_protocol.DESIGN_PROTOCOL.count("Revision mode") == 1
    assert "Rigor protocol" in research_protocol.DESIGN_PROTOCOL  # rigor text kept, revision appended
    assert research_protocol.REPORTING_PROTOCOL.count("Revision mode") == 1


def test_snapshot_and_open_revision_in_log(tmp_path: Path):
    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "main.tex").write_text("x", encoding="utf-8")
    (paper / "main.aux").write_text("aux", encoding="utf-8")
    snap = revision.snapshot_paper(paper)
    assert snap is not None and (snap / "main.tex").is_file() and not (snap / "main.aux").exists()
    log = tmp_path / "resume_log.jsonl"
    assert not revision.revision_open_in_log(tmp_path)
    log.write_text(json.dumps({"event": "revise"}) + "\n", encoding="utf-8")
    assert revision.revision_open_in_log(tmp_path)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": "manager_done", "status": "incomplete"}) + "\n")
    assert revision.revision_open_in_log(tmp_path)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": "manager_done", "status": "complete"}) + "\n")
    assert not revision.revision_open_in_log(tmp_path)


def test_cli_parses_revise():
    args = build_parser().parse_args(["revise", "--run-dir", "runs/bl4", "--review", "r.json",
                                      "--max-new-cells", "12", "--replication-model", "qwen-plus"])
    assert (args.command, args.review, args.max_new_cells, args.replication_model) == ("revise", "r.json", 12, "qwen-plus")
    with pytest.raises(SystemExit):
        build_parser().parse_args(["revise", "--run-dir", "runs/bl4"])  # --review is required


def _variant(correct: list[int], preds: list[str]) -> dict:
    records = [{"qid": f"q{i}", "predicted": preds[i], "correct": c} for i, c in enumerate(correct)]
    return {"accuracy": sum(correct) / len(correct), "per_question": records}


def test_stats_split_finishing_from_answering_and_holm():
    # note policy answers everything; verbatim leaves q0-q9 unanswered (scored 0)
    note = _variant([1] * 30 + [0] * 20, ["a"] * 50)
    verbatim = _variant([0] * 10 + [1] * 20 + [0] * 20, [""] * 10 + ["a"] * 40)
    stats = rigor_stats.compute({"proposed": note, "verbatim": verbatim}, primary=["accuracy"],
                                metric_fields={"accuracy": "correct"})
    assert stats.unanswered == {"proposed": 0, "verbatim": 10}
    (pair,) = stats.pairs
    assert (pair.wins, pair.losses) == (10, 0)
    assert (pair.wins_vs_unanswered, pair.losses_vs_unanswered) == (10, 0)  # the whole gap is finishing
    assert pair.verdict == "detectable"
    rigor_stats.annotate({"proposed": note, "verbatim": verbatim}, stats)
    assert verbatim["unanswered_items"] == 10
    md = rigor_stats.markdown(stats)
    assert "of which vs unanswered" in md and "`verbatim` 10" in md


def test_holm_and_bounded_verdict():
    assert rigor_stats.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    pair = rigor_stats.PairStats("acc", "a", "b", 150, 0.0, -0.03, 0.04, 0.9, "t")
    assert pair.verdict == "bounded within +/-0.05"
    assert rigor_stats.PairStats("acc", "a", "b", 150, 0.0, -0.06, 0.04, 0.9, "t").verdict == "inconclusive"


def test_parse_module_models():
    from jiuwenswarm.agents.harness.common.paper_pipeline.module_models import parse_module_models

    assert parse_module_models("") == {}
    assert parse_module_models("reporting=deepseek-v4-pro, reflection = m2") == {
        "reporting": "deepseek-v4-pro", "reflection": "m2"}
    with pytest.raises(ValueError):
        parse_module_models("manager=deepseek-v4-pro")  # manager does not resolve via _setting
    with pytest.raises(ValueError):
        parse_module_models("reporting")


def test_module_model_overrides_only_the_model_of_that_module(monkeypatch):
    pytest.importorskip("openjiuwen.rsi.artifact_rsi.paper_opt.auto_research")
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reflection.agent import ReflectionAgent
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting.agent import ReportingAgent

    from jiuwenswarm.agents.harness.common.paper_pipeline.module_models import install_module_models

    monkeypatch.setattr(ReportingAgent, "_setting", ReportingAgent.__dict__["_setting"])
    monkeypatch.setattr(ReflectionAgent, "_setting", ReflectionAgent.__dict__["_setting"])
    monkeypatch.setenv("MODEL_NAME", "deepseek-flash")
    monkeypatch.setenv("API_BASE", "https://api.deepseek.com")
    install_module_models({"reporting": "deepseek-v4-flash-old"})
    install_module_models({"reporting": "deepseek-v4-pro"})  # re-install replaces, does not stack
    agent = ReportingAgent.__new__(ReportingAgent)
    agent.config, agent._pw_config = {}, {}
    assert agent._setting("model", "MODEL_NAME", default="default") == "deepseek-v4-pro"
    assert agent._setting("base_url", "API_BASE", required=True) == "https://api.deepseek.com"
    other = ReflectionAgent.__new__(ReflectionAgent)
    other.config = {}
    assert other._setting("model", "MODEL_NAME", default="default") == "deepseek-flash"  # untouched


def test_cli_rejects_bad_module_model(monkeypatch):
    from jiuwenswarm.agents.harness.common.paper_pipeline import cli

    for key in ("API_KEY", "API_BASE", "MODEL_NAME"):
        monkeypatch.setenv(key, "x")
    with pytest.raises(SystemExit, match="unsupported module"):
        cli.main(["resume", "--run-dir", "runs/x", "--module-model", "manager=deepseek-v4-pro"])


def test_unanswered_ids_none_without_answer_fields():
    assert rigor_stats.unanswered_ids([{"qid": "q1", "correct": 1}]) is None
    assert rigor_stats.unanswered_ids([{"qid": "q1", "status": "no_answer", "correct": 0}]) == {"q1"}


def test_locate_matches_titles_with_nested_braces():
    tex = ("\section{Analysis of \textbf{Model} Performance}\label{sec:a}\n"
           "\subsection*{Plain}\n\caption{Accuracy on $\mathcal{D}$}\n")
    assert revision.locate("Analysis of \textbf{Model} Performance", tex)
    assert revision.locate("plain", tex)
    assert revision.locate("Accuracy on $\mathcal{D}$", tex)
    assert revision.locate("\label{sec:a}", tex)
    assert not revision.locate("Analysis of \textbf", tex)
    assert not revision.locate("Model", tex)
