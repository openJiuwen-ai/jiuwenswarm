# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Delivery refuses a draft whose citations, numbers, or residue do not check out."""

from jiuwenswarm.agents.harness.team.rails.delivery_gate import delivery_ready


def test_dangling_citation_is_a_finding():
    findings = delivery_ready(r"see \cite{missing}", {}, {})
    assert any(f.check == "dangling_citation" and "missing" in f.detail for f in findings)


def test_entry_without_doi_or_arxiv_cannot_be_rechecked():
    findings = delivery_ready(
        r"see \cite{smith2024}",
        {"smith2024": "Smith. A paper. 2024."},
        {},
    )
    assert any(f.check == "unverified_reference" for f in findings)


def test_doi_resolves_and_traced_number_passes():
    findings = delivery_ready(
        r"recall was 0.857 (\cite{lee2025})",
        {"lee2025": "Lee. doi:10.1000/xyz"},
        {"0.857": "data/audit_results.json#recall"},
    )
    assert findings == []


def test_printed_number_absent_from_the_registry_is_untraced():
    findings = delivery_ready("the score was 0.42", {}, {})
    assert any(f.check == "untraced_number" and "0.42" in f.detail for f in findings)


def test_residue_blocks_delivery():
    findings = delivery_ready("TODO: fill this in", {}, {})
    assert any(f.check == "residue" for f in findings)


def test_identifier_prefers_the_doi_and_turns_arxiv_ids_into_the_arxiv_doi():
    from jiuwenswarm.agents.harness.team.rails.delivery_gate import reference_identifier

    assert reference_identifier("Lee. doi:10.1000/xyz.") == "10.1000/xyz"
    assert reference_identifier("Lee. arXiv:2410.13787v2") == "10.48550/arXiv.2410.13787"
    assert reference_identifier("Lee. Some workshop, 2024.") is None


def test_a_well_formed_doi_that_names_nothing_is_refused():
    findings = delivery_ready(
        r"as shown by \cite{fake2025}",
        {"fake2025": "Fake. doi:10.9999/invented"},
        {},
        resolve=lambda identifier: False,
    )
    assert [f.check for f in findings] == ["unresolvable_reference"]


def test_a_reference_that_could_not_be_checked_is_not_waved_through():
    findings = delivery_ready(
        r"\cite{lee2025}", {"lee2025": "Lee. doi:10.1000/xyz"}, {}, resolve=lambda identifier: None,
    )
    assert [f.check for f in findings] == ["reference_not_checked"]


def test_registry_resolver_reads_200_and_404_and_caches_them():
    from jiuwenswarm.agents.harness.team.rails.delivery_gate import doi_registry_resolver

    asked = []
    codes = {"real": 200, "fake": 404}

    def status(url):
        asked.append(url)
        return codes["real" if "10.1000" in url else "fake"]

    cache = {}
    resolve = doi_registry_resolver(status=status, cache=cache)
    assert resolve("10.1000/xyz") is True
    assert resolve("10.9999/invented") is False
    assert resolve("10.1000/xyz") is True and len(asked) == 2   # answered from the cache
    assert asked[0] == "https://doi.org/api/handles/10.1000/xyz"
    assert cache == {"10.1000/xyz": True, "10.9999/invented": False}


def test_a_key_cited_twice_is_reported_once():
    findings = delivery_ready(r"\cite{ville1939} and again \cite{ville1939}",
                              {"ville1939": "Ville. Etude critique. 1939."}, {})
    assert [f.check for f in findings] == ["unverified_reference"]


def test_escaped_percent_in_the_registry_traces_the_printed_number():
    # The registry is written from LaTeX ("0.28\%"); the prose token is "0.28".
    # Found by certifying this gate: before the fix every escaped percentage was
    # reported untraced, a false accusation on a genuine value.
    findings = delivery_ready(r"coverage was 0.28\% of claims", {}, {r"0.28\%": "records/coverage.json"})
    assert findings == []


def test_uncertified_auditor_blocks_delivery():
    cert = {"auditor": "reviewer", "certified": False, "e_final": 3.1, "rounds": 10, "alpha": 0.05}
    findings = delivery_ready("no numbers here", {}, {}, certificate=cert)
    assert [f.check for f in findings] == ["uncertified_auditor"]


def test_certified_auditor_with_a_high_false_accusation_bound_is_capped():
    cert = {"auditor": "reviewer", "certified": True, "false_accusation_upper": 0.4}
    assert delivery_ready("text", {}, {}, certificate=cert) == []
    findings = delivery_ready("text", {}, {}, certificate=cert, max_false_accusation=0.2)
    assert [f.check for f in findings] == ["false_accusation_bound"]


def test_a_writer_talking_about_its_instructions_is_residue():
    findings = delivery_ready("The current revision omits the snippets because the prompt supplied counts.", {}, {})
    assert sum(f.check == "residue" for f in findings) == 2
    assert any(f.check == "residue" for f in delivery_ready("The text ends here.\n\nGate decision: ADVANCE.", {}, {}))
    assert not delivery_ready("The judge sees the prompt and the supplied answer.", {}, {})


def test_figure_panels_must_exist_and_be_described():
    figures = [{"label": "fig:method", "caption": "Overview of the study.", "panels": 1},
               {"label": "fig:result", "caption": "Synthesis. (a) Shares with intervals. (b) Papers by stance.",
                "panels": 2}]
    ok = "Figure~\\ref{fig:result}(a) shows the shares; panels a and b split by year; Panel (b) counts papers."
    assert delivery_ready(ok, {}, {}, figures=figures) == []

    bad = "Figure~\\ref{fig:result}(c) and panel d; Figure~\\ref{fig:method}a is the pipeline."
    found = sorted(f.detail for f in delivery_ready(bad, {}, {}, figures=figures))
    assert found == ["the prose cites fig:method(a); the figure has no lettered panels",
                     "the prose cites fig:result(c); the figure has 2 panels",
                     "the prose names panel d; no figure has one"]

    figures[1]["caption"] = "Synthesis. (a) Shares with intervals. (c) Papers by stance."
    checks = sorted(f.check for f in delivery_ready("", {}, {}, figures=figures))
    assert checks == ["missing_panel_description", "nonexistent_panel"]
