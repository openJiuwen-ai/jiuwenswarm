# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""paper_search follows the Research Harness retrieval rules; every test here is offline."""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
import urllib.error

import pytest

from jiuwenswarm.agents.harness.common.tools import paper_search_tools as pst

_REAL_PACE = pst.pace


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Record clock names instead of sleeping; start every test with fresh breakers and key pools."""
    clocks: list[tuple[str, float]] = []
    monkeypatch.setattr(pst, "pace", lambda name, interval: clocks.append((name, interval)))
    monkeypatch.setattr(pst, "_breakers", {})
    monkeypatch.setattr(pst, "_pools", {})
    return clocks


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("https://example.test", code, "error", headers, io.BytesIO(b""))


def _s2_body(*items: dict) -> str:
    return json.dumps({"data": list(items)})


_S2_ITEM = {"paperId": "s2a", "title": "Sequential Testing for Automated Reviewers", "year": 2025,
            "externalIds": {"ArXiv": "2501.00001"}, "authors": [{"name": "A. Author"}]}


class _FakeProvider:
    """A source that returns fixed rows, optionally blocking or failing."""

    def __init__(self, name, papers=(), gate: threading.Event | None = None, error: Exception | None = None):
        self.name, self._gate, self._error = name, gate, error
        self._papers = papers if callable(papers) else list(papers)

    def search(self, query, limit, year_from=None, year_to=None):
        if self._gate is not None:
            self._gate.wait(5)
        if self._error is not None:
            raise self._error
        rows = self._papers(query) if callable(self._papers) else self._papers
        return [pst.Paper(**row) for row in rows]


# --- Rule 2: per-key cooldown and rotation; Rule 1: one clock per S2 key ----------------------


def _clock(key: str) -> str:
    return "semantic_scholar-" + hashlib.sha256(key.encode()).hexdigest()[:12]


def test_s2_429_cools_the_key_and_retries_on_the_next_key(_isolated):
    seen_keys = []

    def fetcher(url, headers):
        seen_keys.append(headers.get("x-api-key"))
        if headers.get("x-api-key") == "k1":
            raise _http_error(429)
        return _s2_body(_S2_ITEM)

    provider = pst.SemanticScholarProvider(fetcher, keys=["k1", "k2"])
    papers = provider.search("sequential testing", 5)

    assert [p.s2_id for p in papers] == ["s2a"]
    assert seen_keys == ["k1", "k2"]
    assert [name for name, _ in _isolated] == [_clock("k1"), _clock("k2")]
    assert all(interval == 1.05 for _, interval in _isolated)
    assert provider.pool.get() == "k2"  # k1 is cooling down


def test_s2_429_with_no_other_live_key_raises():
    def fetcher(url, headers):
        raise _http_error(429)

    with pytest.raises(urllib.error.HTTPError):
        pst.SemanticScholarProvider(fetcher, keys=["only"]).search("q", 5)


def test_keyless_s2_uses_the_shared_host_clock(_isolated):
    pst.SemanticScholarProvider(lambda url, headers: _s2_body(), keys=[]).search("q", 5)
    assert _isolated == [("semantic_scholar", 1.05)]


def test_openalex_403_cools_the_key_for_an_hour_and_switches(monkeypatch):
    used = []

    def fetcher(url, headers):
        used.append("api_key=bad" in url)
        if "api_key=bad" in url:
            raise _http_error(403)
        return json.dumps({"results": [{"id": "https://openalex.org/W1", "title": "A paper", "publication_year": 2024}]})

    provider = pst.OpenAlexProvider(fetcher, keys=["bad", "good"])
    assert [p.openalex_id for p in provider.search("q", 5)] == ["W1"]
    assert used == [True, False]
    assert provider.pool._cooling_until["bad"] - time.monotonic() > 3500


def test_s2_keys_are_read_from_env_comma_separated(monkeypatch):
    monkeypatch.setenv("S2_API_KEY", "k1,k2")
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEYS", raising=False)
    assert pst.SemanticScholarProvider(lambda u, h: "").pool.keys == ("k1", "k2")


# --- Rule 1: request spacing on one clock -------------------------------------------------------


class _FakeClock:
    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


@pytest.mark.parametrize("file_lock", [True, False], ids=["flock", "in-process"])
def test_pace_spaces_requests_on_one_clock_only(monkeypatch, tmp_path, file_lock):
    clock = _FakeClock()
    monkeypatch.setattr(pst, "time", clock)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(pst, "_local_last", {})
    if not file_lock:
        monkeypatch.setattr(pst, "fcntl", None)

    _REAL_PACE("arxiv", 3.1)
    _REAL_PACE("arxiv", 3.1)
    _REAL_PACE("pubmed", 0.34)  # another clock does not wait on arXiv's
    _REAL_PACE("arxiv", 3.1)

    assert clock.sleeps == [3.1, 3.1]
    assert (tmp_path / "jiuwenswarm" / "pace" / "arxiv.pace").exists() is file_lock


def test_pace_refuses_a_symlinked_clock_directory(monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (tmp_path / "jiuwenswarm").mkdir()
    (tmp_path / "jiuwenswarm" / "pace").symlink_to(elsewhere)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert pst._open_pace_file("arxiv") is None
    assert not list(elsewhere.iterdir())


def test_pace_creates_a_private_clock_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fd = pst._open_pace_file("arxiv")
    assert fd is not None
    pst.os.close(fd)
    assert (tmp_path / "jiuwenswarm" / "pace").stat().st_mode & 0o777 == 0o700


# --- Rule 3: bounded retry and per-source circuit breaker ---------------------------------------


def test_fetch_retries_429_three_times_with_capped_retry_after(monkeypatch):
    sleeps, calls = [], []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        raise _http_error(429, retry_after="120")

    monkeypatch.setattr(pst.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(pst.time, "sleep", sleeps.append)
    with pytest.raises(urllib.error.HTTPError):
        pst.urllib_fetch("https://example.test/x", {})
    assert len(calls) == 4 and sleeps == [15.0, 15.0, 15.0]


def test_fetch_does_not_retry_a_400(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append(1)
        raise _http_error(400)

    monkeypatch.setattr(pst.urllib.request, "urlopen", urlopen)
    with pytest.raises(urllib.error.HTTPError):
        pst.urllib_fetch("https://example.test/x", {})
    assert calls == [1]


def test_breaker_refuses_a_source_after_three_failures():
    calls = []

    def fetcher(url, headers):
        calls.append(url)
        raise _http_error(503)

    provider = pst.CrossrefProvider(fetcher)
    for _ in range(3):
        with pytest.raises(urllib.error.HTTPError):
            provider.search("q", 5)
    with pytest.raises(pst.CircuitOpenError, match="crossref"):
        provider.search("q", 5)
    assert len(calls) == 3


# --- Rule 4: fan-out under a time budget --------------------------------------------------------


def test_fan_out_returns_partial_results_when_a_source_misses_the_budget():
    gate = threading.Event()
    providers = [
        _FakeProvider("crossref", [{"title": "Fast paper", "doi": "10.1/fast"}]),
        _FakeProvider("arxiv", [{"title": "Slow paper", "arxiv_id": "2501.00002"}], gate=gate),
    ]
    try:
        result = pst.search_papers("fast paper", providers=providers, time_budget_s=0.2)
    finally:
        gate.set()
    assert [p["title"] for p in result["papers"]] == ["Fast paper"]
    assert result["providers_answered"] == ["crossref"]
    assert len(result["provider_errors"]) == 1
    assert result["provider_errors"][0].startswith("arxiv: did not finish within 0s search time budget")


def test_a_failing_source_is_an_error_not_a_lost_search():
    providers = [
        _FakeProvider("crossref", [{"title": "Kept", "doi": "10.1/kept"}]),
        _FakeProvider("pubmed", error=_http_error(500)),
    ]
    result = pst.search_papers("kept", providers=providers)
    assert result["providers_answered"] == ["crossref"]
    assert result["provider_errors"] == ["pubmed: HTTP Error 500: error"]


# --- Rule 5: dedupe fingerprint -----------------------------------------------------------------


def test_one_paper_from_three_sources_is_one_row():
    providers = [
        _FakeProvider("arxiv", [{"title": "Sequential Testing", "arxiv_id": "2501.00001v2", "year": 2025}]),
        _FakeProvider("semantic_scholar", [{"title": "Sequential testing.", "arxiv_id": "arXiv:2501.00001",
                                            "s2_id": "s2a", "year": 2025}]),
        _FakeProvider("crossref", [{"title": "Sequential Testing", "doi": "10.1/seq", "year": 2025}]),
    ]
    result = pst.search_papers("sequential testing", providers=providers)
    assert len(result["papers"]) == 1
    row = result["papers"][0]
    assert row["arxiv_id"] == "2501.00001" and row["s2_id"] == "s2a" and row["doi"] == "10.1/seq"
    assert row["sources"] == ["arxiv", "semantic_scholar", "crossref"]


def test_fingerprint_priority_and_arxiv_normalization():
    assert pst.fingerprint(pst.Paper(doi="10.1/X", arxiv_id="2501.00001", s2_id="s")) == "10.1/x"
    assert pst.fingerprint(pst.Paper(arxiv_id="https://arxiv.org/abs/2501.00001v3", s2_id="s")) == "2501.00001"
    assert pst.fingerprint(pst.Paper(s2_id="S", openalex_id="W1", pmid="9")) == "s"
    assert pst.fingerprint(pst.Paper(openalex_id="W1", pmid="9")) == "w1"
    assert pst.fingerprint(pst.Paper(pmid="9")) == "9"
    assert pst.fingerprint(pst.Paper(title="A  Title!", year=2024)) == "title:a title:2024"
    assert pst.normalize_arxiv_id("math/0309136v2") == "math/0309136"
    # Two different ids in one field is corrupt input: kept raw, not silently cut to the first.
    assert pst.normalize_arxiv_id("2501.00001 2502.00002") == "2501.00001 2502.00002"


def test_untitled_rows_of_one_year_are_not_merged():
    providers = [_FakeProvider("crossref", [{"title": "", "doi": "10.1/a", "year": 2025},
                                            {"title": "", "doi": "10.1/b", "year": 2025}])]
    assert len(pst.search_papers("q", providers=providers)["papers"]) == 2


# --- Rule 6: relevance first, source as prior ---------------------------------------------------


def test_relevance_outranks_source_priority():
    providers = [
        _FakeProvider("openalex", [{"title": "Unrelated survey of databases", "openalex_id": "W1",
                                    "citation_count": 50000}]),
        _FakeProvider("crossref", [{"title": "Sequential testing for automated reviewers", "doi": "10.1/x"}]),
    ]
    result = pst.search_papers("sequential testing automated reviewers", providers=providers)
    assert [p["doi"] or p["title"] for p in result["papers"]][0] == "10.1/x"


# --- Rule 7: query hygiene ----------------------------------------------------------------------


def test_arxiv_query_forms_union_phrase_and_token_and():
    assert pst.arxiv_query_forms("  sequential   testing ") == [
        'all:"sequential testing"',
        "all:sequential testing",
        'abs:"sequential testing"',
        "all:sequential AND all:testing",
        "abs:sequential AND abs:testing",
    ]
    assert pst.arxiv_query_forms("bandits") == ['all:"bandits"', "all:bandits"]


_ATOM = """<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
<entry><id>http://arxiv.org/abs/2501.00001v2</id><published>2025-01-02T00:00:00Z</published>
<title>Sequential Testing
  for Automated Reviewers</title><summary>We test.</summary><author><name>A. Author</name></author></entry>
</feed>"""


def test_arxiv_sends_every_form_and_keeps_one_row_per_entry(_isolated):
    urls = []

    def fetcher(url, headers):
        urls.append(url)
        return _ATOM

    papers = pst.ArxivProvider(fetcher).search("sequential testing", 10)
    assert len(urls) == 5
    assert [(p.arxiv_id, p.title) for p in papers] == [("2501.00001v2", "Sequential Testing for Automated Reviewers")]
    assert _isolated == [("arxiv", 3.1)] * 5


def test_a_paragraph_is_not_sent_as_a_query():
    paragraph = "word " * 60  # 300 characters
    assert pst.is_query_shaped("x" * 240) and not pst.is_query_shaped("x" * 241)
    with pytest.raises(ValueError, match="1-240"):
        pst.search_papers(paragraph, providers=[_FakeProvider("crossref")])
    seen = []
    provider = _FakeProvider("crossref", lambda q: seen.append(q) or [])
    result = pst.search_papers("short query", extra_queries=[paragraph], providers=[provider])
    assert seen == ["short query"]
    assert result["queries"] == ["short query"] and result["queries_rejected"] == [paragraph.strip()]


async def test_tool_reports_an_unsearchable_query_as_error():
    out = await pst.paper_search.invoke({"query": "x" * 300})
    assert out.startswith("[ERROR]") and "240" in out


# --- Rule 8: PubMed DOI lookups -----------------------------------------------------------------


def _pubmed_article(pmid: str, doi: str, title: str) -> str:
    return (f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article><ArticleTitle>{title}</ArticleTitle>"
            f"<Journal><Title>J</Title><JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue></Journal>"
            f"</Article></MedlineCitation><PubmedData><ArticleIdList>"
            f'<ArticleId IdType="doi">{doi}</ArticleId></ArticleIdList></PubmedData></PubmedArticle>')


def _pubmed_fetcher(articles: str, terms: list):
    def fetcher(url, headers):
        if "esearch" in url:
            terms.append(url)
            return "<eSearchResult><IdList><Id>1</Id><Id>2</Id></IdList></eSearchResult>"
        return f"<PubmedArticleSet>{articles}</PubmedArticleSet>"
    return fetcher


def test_pubmed_doi_query_keeps_only_the_exact_doi(_isolated):
    terms = []
    articles = _pubmed_article("1", "10.1000/other", "Another paper") + _pubmed_article("2", "10.1000/TARGET", "The paper")
    papers = pst.PubMedProvider(_pubmed_fetcher(articles, terms)).search("https://doi.org/10.1000/target", 5)
    assert [(p.pmid, p.title) for p in papers] == [("2", "The paper")]
    assert "10.1000%2Ftarget%5BAID%5D" in terms[0]
    assert _isolated == [("pubmed", 0.34)] * 2


def test_pubmed_doi_query_rejects_a_fuzzy_match():
    articles = _pubmed_article("1", "10.1000/other", "Another paper")
    assert pst.PubMedProvider(_pubmed_fetcher(articles, [])).search("10.1000/target", 5) == []


def test_pubmed_keyword_query_keeps_every_record():
    articles = _pubmed_article("1", "10.1/a", "A") + _pubmed_article("2", "10.1/b", "B")
    assert len(pst.PubMedProvider(_pubmed_fetcher(articles, [])).search("sepsis", 5)) == 2


# --- Rule 9: round-robin across queries ---------------------------------------------------------


def test_round_robin_gives_every_query_a_slot_first():
    assert pst.round_robin_merge([["a", "b", "c"], ["d"], ["a", "e"]], 4) == ["a", "d", "e", "b"]
    assert pst.round_robin_merge([["a"], []], 5) == ["a"]


def test_a_narrow_query_is_not_buried_under_a_dense_one():
    def rows(query):
        if query == "generic methods":
            return [{"title": f"Generic methods {i}", "doi": f"10.1/g{i}"} for i in range(10)]
        return [{"title": "Cohort named exactly", "doi": "10.1/cohort"}]

    result = pst.search_papers("generic methods", extra_queries=["cohort named exactly"],
                               max_results=3, providers=[_FakeProvider("crossref", rows)])
    # Ranked by score alone, three generic hits would fill the page; round-robin puts the cohort second.
    assert [p["doi"] for p in result["papers"]] == ["10.1/g9", "10.1/cohort", "10.1/g8"]


# --- Rule 10: does the read stand ---------------------------------------------------------------

_FOUR = ["local", "arxiv", "crossref", "openalex", "semantic_scholar", "multi"]
_ROWS = [{"title": "Prior work", "doi": "10.1/prior"}]


@pytest.mark.parametrize(
    "queried, answered, errors, rows, stands",
    [
        pytest.param(_FOUR, _FOUR, [], _ROWS, True, id="clean"),
        pytest.param(_FOUR, ["local", "crossref", "openalex", "semantic_scholar", "multi"],
                     ["arxiv: HTTP Error 429: Unknown Error",
                      "arxiv: Circuit breaker for 'arxiv' is open; retry after 54s"],
                     _ROWS, True, id="a0096f-only-arxiv-throttled"),
        pytest.param(_FOUR, ["local", "crossref", "semantic_scholar", "multi"],
                     ["arxiv: provider did not finish within 45s search time budget; partial results returned",
                      "openalex: HTTP Error 503"],
                     _ROWS, False, id="two-of-four-out"),
        pytest.param(["local", "cache", "arxiv", "crossref", "openalex", "semantic_scholar", "multi"],
                     ["local", "cache", "crossref", "semantic_scholar", "multi"],
                     ["arxiv: HTTP Error 429: Unknown Error", "openalex: HTTP Error 503"],
                     _ROWS, False, id="cache-does-not-stand-in"),
        pytest.param(["local", "crossref", "openalex", "semantic_scholar", "multi"],
                     ["local", "crossref", "openalex", "semantic_scholar", "multi"],
                     ["semantic_scholar: <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING]>"],
                     _ROWS, True, id="a0113-lost-one-query-is-not-down"),
        pytest.param(["local", "crossref", "openalex", "semantic_scholar", "multi"],
                     ["local", "crossref", "openalex", "multi"],
                     ["semantic_scholar: <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING]>"],
                     _ROWS, False, id="answered-no-query-is-down"),
        pytest.param(_FOUR, _FOUR, [], _ROWS + [{"title": "", "doi": "10.20944/preprints202501.0811.v1"}],
                     True, id="a0124-untitled-row-does-not-void"),
        pytest.param(_FOUR, _FOUR, [], [{"title": "An unrelated record", "year": 2025}], False, id="no-id"),
        pytest.param(_FOUR, _FOUR, [], [{"title": "", "doi": "10.1/prior"}], False, id="no-title"),
        pytest.param(["local", "multi"], ["local", "multi"], [], _ROWS, False, id="local-only"),
    ],
)
def test_prior_art_read_stands(queried, answered, errors, rows, stands):
    assert pst.prior_art_read_stands(queried, answered, errors, rows) is stands


async def test_tool_returns_json_with_the_read_verdict(monkeypatch):
    providers = [_FakeProvider(name, [{"title": "Prior work", "doi": "10.1/prior"}])
                 for name in ("arxiv", "openalex", "crossref")]
    monkeypatch.setattr(pst, "default_providers", lambda: providers)
    out = json.loads(await pst.paper_search.invoke({"query": "prior work"}))
    assert out["read_stands"] is True
    assert out["providers_answered"] == ["arxiv", "openalex", "crossref"]
    assert out["papers"][0]["sources"] == ["arxiv", "openalex", "crossref"]


# --- OpenReview and Google Scholar ------------------------------------------------------------

def test_openreview_reads_v2_notes_and_asks_with_term(_isolated):
    sent = []
    note = {"forum": "abc", "pdate": 1714521600000,   # 2024-05-01
            "content": {"title": {"value": "Self-Preference Bias in LLM-as-a-Judge"},
                        "authors": {"value": ["A. Author"]}, "venue": {"value": "ICLR 2025 Poster"},
                        "abstract": {"value": "Judges favour their own outputs."}}}

    def fetcher(url, headers):
        sent.append(url)
        return json.dumps({"notes": [note, {"forum": "untitled", "content": {}}]})

    papers = pst.OpenReviewProvider(fetcher).search("self-preference", 5, year_from=2024)
    assert "term=self-preference" in sent[0] and "type=terms" in sent[0]
    assert [(p.title, p.year, p.venue, p.url) for p in papers] == [
        ("Self-Preference Bias in LLM-as-a-Judge", 2024, "ICLR 2025 Poster", "https://openreview.net/forum?id=abc")]
    assert pst.OpenReviewProvider(fetcher).search("self-preference", 5, year_to=2023) == []
    assert ("openreview", 1.0) in _isolated


def test_google_scholar_is_used_only_when_configured(monkeypatch):
    monkeypatch.delenv("GOOGLE_SCHOLAR_API_URL", raising=False)
    assert "google_scholar" not in [p.name for p in pst.default_providers(lambda u, h: "")]
    monkeypatch.setenv("GOOGLE_SCHOLAR_API_URL", "https://scholar.example/search")
    assert "google_scholar" in [p.name for p in pst.default_providers(lambda u, h: "")]

    item = {"title": "Mindfulness for student anxiety", "link": "https://x.org/p", "snippet": "A trial.",
            "publication_info": {"summary": "A Author, B Author - Journal of Psychology, 2021 - x.org",
                                 "authors": [{"name": "A Author"}]},
            "inline_links": {"cited_by": {"total": 42}}, "resources": [{"file_format": "PDF"}]}
    provider = pst.GoogleScholarProvider(lambda url, headers: json.dumps({"organic_results": [item]}), keys=["k"])
    [paper] = provider.search("mindfulness", 5)
    assert (paper.year, paper.venue, paper.citation_count, paper.open_pdf) == (2021, "Journal of Psychology", 42, True)


def test_a_doi_read_off_a_landing_page_loses_the_page_view():
    assert pst.normalize_doi("https://doi.org/10.1051/itmconf/20268401001/pdf") == "10.1051/itmconf/20268401001"
    assert pst.normalize_doi("doi:10.3389/fpsyg.2020.01234/full") == "10.3389/fpsyg.2020.01234"
    assert pst.normalize_doi("10.1145/3442188.3445922") == "10.1145/3442188.3445922"


def test_openreview_author_records_and_non_person_authors_become_names():
    row = pst._row(pst.Paper(title="T", authors=["Association for Computational Linguistics 2026", "Bohao Chu"]))
    assert row["authors"] == ["Bohao Chu"]
    provider = pst.OpenReviewProvider(fetcher=lambda *a, **k: json.dumps({"notes": [{"forum": "f", "cdate": 1.7e12,
        "content": {"title": {"value": "T"}, "authors": {"value": [{"fullname": "A B", "username": "~A_B1"}, "C D"]}}}]}))
    assert provider.search("q", 5)[0].authors == ["A B", "C D"]
