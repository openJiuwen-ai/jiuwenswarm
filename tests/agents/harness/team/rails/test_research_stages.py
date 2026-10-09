# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Build- and analyze-stage steps shared by every research run. Offline."""

from jiuwenswarm.agents.harness.team.rails import research_stages as rs
from jiuwenswarm.agents.harness.common.tools import jev_decision as jev


def test_openalex_lookup_uses_the_doi_or_the_arxiv_doi():
    assert rs.openalex_url({"doi": "10.1214/18-PS321"}).startswith(
        "https://api.openalex.org/works/doi:10.1214/18-PS321?select=")
    assert "doi:10.48550/arXiv.2410.13787" in rs.openalex_url({"arxiv_id": "2410.13787"})
    assert rs.openalex_url({"title": "no identifier"}) is None


def test_abstract_is_rebuilt_in_word_order():
    assert rs.abstract_text({"We": [0], "show": [1], "gains": [2]}) == "We show gains"
    assert rs.abstract_text(None) == ""


def test_candidate_sentences_keep_result_sentences_in_order():
    abstract = ("Reviewers are widely used. We find that agreement drops to 40%. "
                "Our method uses sequential tests. Results show the error rate is bounded.")
    assert rs.candidate_sentences(abstract) == [
        "We find that agreement drops to 40%.",
        "Results show the error rate is bounded.",
    ]


def test_cocitation_counts_only_works_outside_the_pool():
    works = [
        {"id": "W1", "referenced_works": ["W9", "W8", "W2"]},
        {"id": "W2", "referenced_works": ["W9", "W8"]},
        {"id": "W3", "referenced_works": ["W9", "W2"]},
    ]
    # W2 is cited twice but is in the pool; W9 three times, W8 twice
    assert rs.cocitation_candidates(works) == [("W9", 3), ("W8", 2)]


def test_topic_clusters_group_by_primary_topic_largest_first():
    works = [
        {"id": "W1", "cited_by_count": 5, "primary_topic": {"display_name": "Sequential testing"}},
        {"id": "W2", "cited_by_count": 9, "primary_topic": {"display_name": "Sequential testing"}},
        {"id": "W3", "cited_by_count": 1, "primary_topic": None},
    ]
    clusters = rs.topic_clusters(works, {"W2": 0.7})
    assert [c["topic"] for c in clusters] == ["Sequential testing", "unclassified"]
    assert clusters[0]["representative"] == "W2"
    assert clusters[0]["papers"][0]["relevance"] == 0.7


def test_screen_paper_sends_one_call_and_keeps_every_decision():
    work = {"id": "W1", "title": "T", "publication_year": 2025,
            "abstract_inverted_index": {"We": [0], "find": [1], "a": [2], "bias.": [3]}}
    sent = []

    def decide(state, questions):
        sent.append(questions)
        return {"relevant": {"noul": 0.8}, "finding_1": {"noul": 0.9},
                "stance_1": {"choice": "a", "probabilities": {"a": 0.7, "b": 0.3}}}

    out = rs.screen_paper(work, "Q?", {"a": "x", "b": "y"}, jev, decide)
    assert len(sent) == 1 and set(sent[0]) == {"relevant", "finding_1", "stance_1"}
    assert out["relevance"] == 0.8
    assert out["claims"][0]["stance"] == "a" and out["claims"][0]["finding_probability"] == 0.9
    assert rs.screen_paper({"id": "W2"}, "Q?", {"a": "x", "b": "y"}, jev, decide) is None


def test_kept_findings_need_a_finding_and_a_stance():
    claims = [
        {"finding_probability": 0.9, "stance": "supports"},
        {"finding_probability": 0.9, "stance": "unrelated"},
        {"finding_probability": 0.2, "stance": "supports"},
    ]
    assert rs.kept_findings(claims) == [claims[0]]


def test_explicit_results_outrank_background_that_shares_the_topic_word():
    abstract = ("Bias in LLM judges is widely discussed. Judge bias affects rankings. "
                "Prior work studied bias in rubrics. Bias may come from familiarity. "
                "We find that judges prefer their own outputs in 71% of comparisons.")
    picked = rs.candidate_sentences(abstract)
    assert picked[-1] == "We find that judges prefer their own outputs in 71% of comparisons."
    # the other two slots go to the latest background sentences, not the first ones
    assert picked[0] == "Prior work studied bias in rubrics."
