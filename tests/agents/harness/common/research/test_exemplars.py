# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exemplar anchoring: field profile, copy guard, exemplar choice, offline replay."""

import json
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.research import exemplars as ex
from jiuwenswarm.agents.harness.common.research import gateway as gw

SKILLS = Path(__file__).resolve().parents[5] / "jiuwenswarm/resources/agent/workspace/skills"
WORKS = [{"id": f"W{i}", "title": f"Review {i}", "cited_by_count": 100 - i} for i in range(4)]
FORM = {"sections": [{"name": "Methods", "role": "method", "key_moves": ["state eligibility before search"]},
                    {"name": "Results", "role": "results", "key_moves": ["report the flow of studies"]}],
        "abstract_elements": ["objective", "method", "result"], "quality": 8}


def test_the_discipline_picks_the_writing_profile():
    assert ex.resolve_domain("clinical psychology") == "biomedical"
    assert ex.resolve_domain("educational psychology") == "social-science"
    assert ex.resolve_domain("machine learning") == "stem"
    if (SKILLS / "domain-writing-stem").exists():
        assert "Rhetorical Contract" in ex.domain_profile("", SKILLS)["guidance"]


def test_copy_guard_measures_repeated_wording():
    source = "we searched five databases from inception to march and screened every record in duplicate"
    assert ex.max_ngram_overlap(source, [source]) == 1.0
    assert ex.max_ngram_overlap("state the eligibility rules before describing the search", [source]) == 0.0


def test_search_ranks_by_relevance_and_widens_a_sparse_query():
    urls = []

    def get_json(url):
        urls.append(url)
        return {"results": WORKS if len(urls) > 1 else WORKS[:1]}

    assert [w["id"] for w in ex.search(["a b c"], get_json)] == ["W0", "W1", "W2", "W3"]
    # Citation order would put loosely matched classics ahead of the topic's own reviews.
    assert len(urls) == 2 and all("sort=relevance_score:desc" in u for u in urls)
    assert "search%3Aa%20b%2C" in urls[1]


def test_anchor_keeps_clean_exemplars_and_replays_offline(tmp_path, monkeypatch):
    answers = {"W0": dict(FORM, quality=3), "W1": FORM, "W2": FORM}

    def post(url, key, body, timeout=600):
        user = body["messages"][1]["content"]
        if user.startswith("Paper: "):
            work = next(w for w in WORKS if user.startswith(f"Paper: {w['title']} "))
            return {"choices": [{"message": {"content": json.dumps(answers[work["id"]])}}], "usage": {"total_tokens": 5}}
        return {"choices": [{"message": {"content": json.dumps({"sections": FORM["sections"]})}}],
                "usage": {"total_tokens": 9}}

    monkeypatch.setattr(gw, "_post", post)

    def fetch(work):
        if work["id"] == "W3":
            return {"route": None}
        return {"route": "pmc", "sections": [{"title": "Methods", "text": "eligible trials were pooled"}]}

    rows = []
    live = ex.anchor(["q"], "target", gw.CachedGateway(tmp_path / "c.jsonl", False, rows.append),
                     tmp_path / "sources.json", False, fetch, want=2, get_json=lambda url: {"results": WORKS})
    assert [x["id"] for x in live["exemplars"]] == ["W1", "W2"]          # W0's form scored too low
    assert live["plan"]["sections"][0]["role"] == "method"

    monkeypatch.setattr(gw, "_post", None)
    replay_rows = []
    replay = ex.anchor(["q"], "target", gw.CachedGateway(tmp_path / "c.jsonl", True, replay_rows.append),
                       tmp_path / "sources.json", True, fetch=None, want=2)
    assert replay == live and replay_rows == rows
    with pytest.raises(SystemExit):
        ex.anchor(["q"], "target", gw.CachedGateway(tmp_path / "c.jsonl", True), tmp_path / "none.json", True, None)
