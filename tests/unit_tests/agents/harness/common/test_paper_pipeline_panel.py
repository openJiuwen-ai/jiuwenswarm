"""Tests for the independent reviewer panel (no model calls): the paper is never cut silently, the
host evidence reaches every reviewer, coverage / layout are recorded, claims are parsed."""

from __future__ import annotations

import json
from pathlib import Path

from jiuwenswarm.agents.harness.common.paper_pipeline import review_panel, rigor_stats
from test_paper_pipeline_p1 import _cell, _pattern  # noqa: E402  (sibling test module: shared fixtures)


def _paper(tmp_path: Path) -> Path:
    paper = tmp_path / "paper"
    paper.mkdir()
    body = "".join(f"\\section{{S{i}}}" + "x" * 1000 for i in range(5))
    (paper / "main.tex").write_text("\\documentclass{x}\\begin{document}" + body + "\\end{document}", encoding="utf-8")
    (paper / "main.log").write_text("Overfull \\hbox (25.3pt too wide) in paragraph\nOverfull \\hbox (2.0pt too wide)",
                                    encoding="utf-8")
    return paper


def test_paper_is_never_cut_silently(tmp_path: Path):
    text, coverage = review_panel.prepare_paper(_paper(tmp_path), max_chars=2500)
    assert coverage["truncated"] and coverage["sections_omitted"] == ["S2", "S3", "S4"]
    assert coverage["sections_sent"][-2:] == ["S0", "S1"] and "omitted for length, not reviewed: S2" in text
    full, coverage = review_panel.prepare_paper(tmp_path / "paper")
    assert not coverage["truncated"] and full.count("\\section") == 5


def test_evidence_pack_carries_stats_and_audit(tmp_path: Path):
    variants = {"proposed_T1": _cell(_pattern(40)), "base_T1": _cell(_pattern(30))}
    stats = rigor_stats.compute(variants, primary=["answer_em"])
    (tmp_path / "statistics.json").write_text(json.dumps(stats.to_dict()), encoding="utf-8")
    (tmp_path / "audit.json").write_text(json.dumps({"verdict": "failed", "blocking": ["X"], "findings": []}))
    pack, coverage = review_panel.evidence_pack(tmp_path)
    assert '"role": "confirmatory"' in pack and '"p_holm"' in pack and '"verdict": "failed"' in pack
    assert coverage["omitted"] == []
    _, small = review_panel.evidence_pack(tmp_path, max_chars=200)
    assert small["omitted"]  # what did not fit is listed


def test_panel_records_coverage_evidence_and_layout(tmp_path: Path, monkeypatch):
    paper = _paper(tmp_path)
    seen = {}

    def fake_review(member, text, *, timeout=600.0, evidence=""):
        seen["evidence"] = evidence
        return review_panel.Review(member=member.label, model=member.model, ok=True, overall=5.0,
                                   scores={d: 0 for d in review_panel.DIMENSIONS}, prompt_tokens=1, completion_tokens=1)

    monkeypatch.setattr(review_panel, "review_one", fake_review)
    monkeypatch.setattr(review_panel, "check_evidence", lambda m, p, e: {
        "member": m.label, "model": m.model, "ok": True, "claims": [{"claim": "A beats B", "status": "overstated",
                                                                       "location": "abstract", "note": "raw p"}],
        "status_counts": {"supported": 0, "overstated": 1, "contradicted": 0, "no_source": 0}, "error": ""})
    (tmp_path / "statistics.json").write_text(json.dumps({"pairs": [], "method": {}}), encoding="utf-8")
    member = review_panel.Member("m", "http://x", "k", "env:m")
    summary = review_panel.run_panel(paper, [member], tmp_path / "out", evidence_dir=tmp_path, checker=member,
                                     max_chars=2500)
    assert "statistics.pairs" in seen["evidence"]
    assert summary["coverage"]["truncated"] and summary["layout"]["overfull_over_10pt"] == 1
    assert "not performed" in summary["visual_review"]
    md = (tmp_path / "out" / "review_panel.md").read_text()
    assert "Partial review" in md and "[overstated] A beats B" in md


def test_parse_claims():
    assert review_panel.parse_claims('blah CLAIMS_JSON: [{"claim": "x", "status": "supported"}]') == [
        {"claim": "x", "status": "supported"}]
    assert review_panel.parse_claims("no marker") is None
