# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Full text: route order, section parsing, results sections, failure reasons. Offline."""

import json
import urllib.error

from jiuwenswarm.agents.harness.common.research import fulltext as ft

LONG = "We find the effect holds in every cohort. " * 60
HTML = (f"<html><nav>skip me</nav><h2>1 Introduction</h2><p>Setting.</p><h2>4 Results</h2><p>{LONG}</p>"
        "<h3>4.1 Ablation</h3><p>Removing retrieval doubles errors.</p><h2>5 Discussion</h2><p>Talk.</p></html>")
JATS = (f"<article><body><sec><title>Methods</title><p>Design.</p></sec><sec><title>Results</title>"
        f"<p>{LONG}</p></sec></body></article>").encode()


def test_routes_follow_the_harness_order():
    work = {"doi": "https://doi.org/10.48550/arxiv.2401.00001", "ids": {"pmcid": "https://ncbi/pmc/PMC123"},
            "best_oa_location": {"pdf_url": "https://x.org/a.pdf"}}
    assert [r for r, _ in ft.routes(work)] == ["arxiv", "arxiv", "pmc", "openalex"]
    assert ft.routes({"doi": "10.1/x"})[0][1].startswith(ft.EUROPE_PMC + "/search?")


def test_results_sections_include_their_subsections():
    sections = ft.html_sections(HTML)
    assert [s["title"] for s in sections] == ["Introduction", "Results", "Ablation", "Discussion"]
    assert "skip me" not in json.dumps(sections)
    text = ft.results_text(sections)
    assert "doubles errors" in text and "Talk" not in text and "Setting" not in text
    assert ft.results_text(ft.jats_sections(JATS)).startswith("We find")


def test_first_route_with_text_wins_and_failures_say_why():
    def get(url):
        if "search?" in url:
            return json.dumps({"resultList": {"result": [{"pmcid": "PMC9"}]}}).encode(), "application/json"
        if url.endswith("PMC9/fullTextXML"):
            return JATS, "application/xml"
        raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)

    got = ft.fetch({"doi": "10.1/x", "best_oa_location": {"pdf_url": "https://x.org/a.pdf"}}, get=get)
    assert got["route"] == "pmc" and got["url"].endswith("PMC9/fullTextXML")
    missed = ft.fetch({"doi": "10.1/y", "best_oa_location": {"pdf_url": "https://x.org/b.pdf"}},
                      get=lambda url: (b'{"resultList": {"result": []}}', "json") if "search?" in url
                      else (_ for _ in ()).throw(urllib.error.HTTPError(url, 403, "Forbidden", None, None)))
    assert missed["route"] is None
    assert missed["attempts"][0] == "pmc: no open-access copy" and "403" in missed["attempts"][1]
