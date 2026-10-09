# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The back half's synthesis, offline: exact statistics, paper stance, printed-literal registry."""

import importlib.util
import re
import sys
from collections import Counter
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[5] / "scripts/run_research_back.py"
spec = importlib.util.spec_from_file_location("run_research_back", SCRIPT)
rrb = importlib.util.module_from_spec(spec)
sys.modules["run_research_back"] = rrb
spec.loader.exec_module(rrb)


def test_exact_binomial_test_and_interval_match_known_values():
    assert abs(rrb.binom_test_two_sided(7, 9) - 0.1797) < 1e-4
    assert rrb.binom_test_two_sided(5, 10) == 1.0
    lo, hi = rrb.clopper_pearson(7, 9)
    assert (round(lo, 4), round(hi, 4)) == (0.3999, 0.9719)
    assert rrb.clopper_pearson(0, 5)[0] == 0.0 and rrb.clopper_pearson(5, 5)[1] == 1.0


def test_a_paper_takes_the_majority_of_its_directional_findings():
    assert rrb.paper_stance(Counter(supports=2, challenges=1)) == "supports"
    assert rrb.paper_stance(Counter(supports=1, challenges=1)) == "mixed"
    assert rrb.paper_stance(Counter(method=3)) == "method"


def _paper(year, **counts):
    return {"year": year, "counts": Counter(counts), "findings": []}


def test_registry_holds_the_literals_the_paper_prints():
    papers = {f"w{i}": _paper(2020 + i % 4, supports=1) for i in range(8)}
    papers["w8"] = _paper(2024, challenges=2)
    papers["w9"] = _paper(2023, challenges=1)
    result, registry = rrb.run_synthesis(papers)
    assert (result["supporting"], result["directional"]) == (8, 10)
    assert result["share"] == "0.80" and result["ci95"][0] == "0.44"
    assert "0.80" in registry and "0.44" in registry and "95" in registry
    assert registry["10"].startswith("experiment_result.json#")



def test_result_figure_shows_only_registered_values():
    papers = {f"w{i}": _paper(2020 + i % 4, supports=1) for i in range(8)}
    papers["w8"] = _paper(2024, challenges=2)
    papers["w9"] = _paper(2023, method=1)
    result, registry = rrb.run_synthesis(papers)
    style = rrb.rf.resolve_style("ICLR", "")
    figure = rrb.result_figure(result, registry, style)
    assert [r["label"] for r in figure["panels"][0]["rows"]] == ["All (9)", "Before 2022 (4)", "2022 on (5)"]
    assert set(rrb.rf.printed_tokens(figure)) <= set(registry)
    assert figure["panels"][1]["series"] == ["supports", "challenges", "method"]
    assert result["by_year"]["later"]["stance_counts"]["method"] == 1


def test_an_arxiv_paper_takes_its_title_and_authors_from_arxiv(tmp_path):
    import json
    oid = "https://openalex.org/W1"
    (tmp_path / "openalex_cache.json").write_text(json.dumps({"u": {
        "id": oid, "doi": "https://doi.org/10.48550/arxiv.2404.13076", "title": "An Unrelated Title",
        "publication_year": 2024}}))
    (tmp_path / "paper_search.json").write_text(json.dumps({"papers": [{
        "title": "LLM Evaluators Recognize and Favor Their Own Generations", "authors": ["Arjun Panickssery"],
        "arxiv_id": "2404.13076", "doi": "", "venue": "arXiv", "sources": ["arxiv", "openreview"]}]}))
    (tmp_path / "evidence_pack.json").write_text(json.dumps({"by_stance": {"supports": [
        {"openalex_id": oid, "title": "An Unrelated Title", "sentence": "We find self-preference."}]}}))
    papers, retracted = rrb.papers_from_run(tmp_path)
    p = papers[oid]
    assert retracted == {}
    assert p["title"] == "LLM Evaluators Recognize and Favor Their Own Generations"
    assert p["authors"] == ["Arjun Panickssery"] and p["doi"] == "10.48550/arxiv.2404.13076"


def test_the_appendix_lists_every_paper_and_registers_its_counts():
    papers = {"w1": dict(_paper(2021, supports=2, method=1), key="a2021x"),
              "w2": dict(_paper(2023, challenges=1), key="b2023y")}
    registry = {}
    tex = rrb.studies_appendix(papers, registry)
    assert "\\citet{a2021x} & 2 & 0 & 1 & supports" in tex and "\\citet{b2023y} & 0 & 1 & 0 & challenges" in tex
    assert {"0", "1", "2"} <= set(registry) and f"\\label{{{rrb.STUDIES_LABEL}}}" in tex


def test_the_search_protocol_registers_the_values_it_states(tmp_path):
    import json
    (tmp_path / "manifest.json").write_text(json.dumps({
        "generated_at": "2026-10-06T14:45:37+00:00", "pool": 37, "fulltext_retrieved": 20, "claims": 115,
        "kept": 71}))
    (tmp_path / "acquisition_report.json").write_text(json.dumps({"fulltext": {"by_route": {"pmc": 7}}}))
    registry = {}
    text = rrb.search_protocol(tmp_path, {"queries": ["q one", "q two"]},
                               {"papers": [{}] * 40, "providers_answered": ["arxiv", "openreview"]},
                               {"papers": 23, "findings": 70}, registry, {"RETRACTED ARTICLE: X": 1})
    assert "2026-10-06" in text and "arXiv, OpenReview" in text and '"q one"; "q two"' in text
    assert "1 paper(s) whose title marks a retraction" in text and "leaving 70 findings from 23" in text
    # "0.5", the paper and finding counts are registered by run_synthesis
    for tok in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", text):
        assert tok in registry or tok in ("0.5", "23", "70"), tok


def test_a_doi_loses_its_resolver_prefix_and_page_view():
    assert rrb._doi("https://doi.org/10.1051/itmconf/20268401001/pdf") == "10.1051/itmconf/20268401001"
    assert rrb._doi("10.48550/arXiv.2404.13076") == "10.48550/arxiv.2404.13076"


def test_the_writer_is_told_each_papers_coded_stance():
    papers = {"w1": dict(_paper(2024, supports=1), key="divi2024auditing", title="T", venue=""),
              "w2": dict(_paper(2024, method=2), key="liu2024evaluating", title="U", venue="")}
    layout = {"sections": rrb.DEFAULT_SECTIONS, "abstract_elements": [], "domain": "stem", "guidance": ""}
    prompt = rrb.writer_prompt({"question": "q"}, {}, {}, {"1": "x"}, papers, [], layout, "- protocol")
    assert "\\citep{divi2024auditing} T (n/a); coded stance supports (1 supporting" in prompt
    assert "\\citep{liu2024evaluating} U (n/a); coded stance method (0 supporting" in prompt


def test_a_retracted_paper_is_left_out_of_the_synthesis(tmp_path):
    import json
    (tmp_path / "openalex_cache.json").write_text(json.dumps({}))
    (tmp_path / "paper_search.json").write_text(json.dumps({"papers": []}))
    (tmp_path / "evidence_pack.json").write_text(json.dumps({"by_stance": {"supports": [
        {"openalex_id": "W1", "title": "RETRACTED ARTICLE: A study", "sentence": "s"},
        {"openalex_id": "W2", "title": "A sound study", "sentence": "s"}]}}))
    papers, retracted = rrb.papers_from_run(tmp_path)
    assert list(papers) == ["W2"] and retracted == {"RETRACTED ARTICLE: A study": 1}


def test_an_author_list_keeps_people_only():
    assert rrb._person("{'fullname': 'Sidhaarth Murali', 'username': '~Sidhaarth_Murali1'}") == "Sidhaarth Murali"
    assert rrb._person("Association for Computational Linguistics 2026") is None
    assert rrb._person("MVC Team") == "MVC Team" and rrb._person("Bohao Chu") == "Bohao Chu"
    assert rrb.RETRACTED.match("Correction: Effectiveness of an 8-week course")


def test_a_hyphenated_given_name_keeps_its_surname():
    assert rrb._bib_name("Po-hao Li") == "Li, Po-hao"
    assert rrb._bib_name("MVC Team") == "Team, MVC" and rrb._bib_name("Plato") == "Plato"
