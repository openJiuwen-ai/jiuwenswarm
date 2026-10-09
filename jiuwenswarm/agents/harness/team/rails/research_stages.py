# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Build- and analyze-stage steps shared by every research run in this repository.

Two entry points use these steps: scripts/run_research_front.py runs a new
topic forward from a research question, and scripts/complete_research_run.py
fills the stages a recorded run left without artifacts. Both go through the
same functions, so a stage behaves the same way whichever script drives it.

build
    OpenAlex metadata for each pool paper (found by DOI, or by its arXiv DOI);
    papers grouped by OpenAlex primary topic (literature_map); works cited by
    at least two pool papers but absent from the pool (citation expansion).

analyze
    One decision call per paper: is the paper relevant to the question, which
    abstract sentences report a finding, how each finding bears on the question.
    Candidate sentences are picked by a fixed pattern; the decisions are Jev's.

Every network answer goes through a cache kept next to the run's outputs, so a
run can be replayed offline and reproduces its artifacts byte for byte. The
module is stdlib only; the network fetch is borrowed from paper_search_tools
the first time a live request is needed.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Sequence

TOOLS = Path(__file__).resolve().parents[2] / "common/tools"

OPENALEX_FIELDS = ("id,doi,title,publication_year,cited_by_count,referenced_works,"
                   "primary_topic,abstract_inverted_index")

# Sentences that report an outcome tend to carry one of these; the rest of an
# abstract is usually setting and method. Jev then decides for each candidate.
RESULT_MARKERS = re.compile(
    r"\b(we (show|find|found|observe|prove|demonstrate)|results? (show|indicate|suggest)|"
    r"outperform|improv|reduc|achiev|increas|decreas|fail|significant|accuracy|precision|"
    r"recall|error rate|bias|agree|correlat)\w*|\d+(\.\d+)?\s*%",
    re.IGNORECASE,
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ build stage

def openalex_url(paper: dict, fields: str = OPENALEX_FIELDS) -> str | None:
    """OpenAlex lookup URL for a paper, by DOI, else by its arXiv DOI."""
    doi = (paper.get("doi") or "").strip()
    if not doi and paper.get("arxiv_id"):
        doi = f"10.48550/arXiv.{paper['arxiv_id'].strip()}"
    if not doi:
        return None
    quoted = urllib.parse.quote(doi, safe="/")
    return f"https://api.openalex.org/works/doi:{quoted}?select={fields}"


def abstract_text(inverted: dict | None) -> str:
    """Rebuild an abstract from OpenAlex's word -> positions index."""
    if not inverted:
        return ""
    slots: dict[int, str] = {}
    for word, positions in inverted.items():
        for pos in positions:
            slots[pos] = word
    return " ".join(slots[i] for i in sorted(slots))


# An explicit report of an outcome. These outrank RESULT_MARKERS, whose words
# ("bias", "accuracy", "improve") also fill the background sentences of papers
# whose topic is that very word.
REPORTING = re.compile(
    r"\b(we (show|find|found|observe|prove|demonstrate|report|reveal)|"
    r"(results?|experiments?|analys[ie]s|findings) (show|indicate|suggest|reveal|confirm)|"
    r"outperform\w*|significantly)\b|\d+(\.\d+)?\s*%",
    re.IGNORECASE,
)


def candidate_sentences(abstract: str, limit: int = 3) -> list[str]:
    """Abstract sentences most likely to report a finding, at most `limit`, in abstract order.

    Sentences that explicitly report an outcome come first. Remaining slots go
    to sentences with a weaker result marker, taken from the end of the
    abstract backwards, because an abstract states its results after its
    setting. On a run about LLM-judge self-preference, taking the first marked
    sentences instead picked background sentences that merely mention "bias",
    and few candidates were findings at all.
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", abstract.strip())]
    strong = [i for i, s in enumerate(sentences) if REPORTING.search(s)][:limit]
    weak = [i for i, s in enumerate(sentences)
            if i not in strong and RESULT_MARKERS.search(s)]
    picked = strong + sorted(weak, reverse=True)[: limit - len(strong)]
    return [sentences[i] for i in sorted(picked)]


def cocitation_candidates(works: list[dict], min_citing: int = 2) -> list[tuple[str, int]]:
    """Works cited by at least `min_citing` pool papers and not in the pool, most cited first."""
    in_pool = {w["id"] for w in works}
    counts = Counter(ref for w in works for ref in set(w.get("referenced_works") or []))
    ranked = [(ref, n) for ref, n in counts.items() if n >= min_citing and ref not in in_pool]
    return sorted(ranked, key=lambda item: (-item[1], item[0]))


def topic_clusters(works: list[dict], relevance: dict[str, float]) -> list[dict]:
    """Pool papers grouped by OpenAlex primary topic, largest group first."""
    groups: dict[str, list[dict]] = defaultdict(list)
    fields: dict[str, str] = {}
    for w in works:
        topic = (w.get("primary_topic") or {})
        name = topic.get("display_name") or "unclassified"
        fields[name] = ((topic.get("field") or {}).get("display_name")) or ""
        groups[name].append(w)
    clusters = []
    for name, members in groups.items():
        members = sorted(members, key=lambda w: -(w.get("cited_by_count") or 0))
        clusters.append({
            "topic": name,
            "field": fields[name],
            "size": len(members),
            "representative": members[0]["id"],
            "papers": [{
                "openalex_id": w["id"],
                "title": w.get("title"),
                "year": w.get("publication_year"),
                "cited_by_count": w.get("cited_by_count"),
                "relevance": relevance.get(w["id"]),
            } for w in members],
        })
    return sorted(clusters, key=lambda c: (-c["size"], c["topic"]))


def expansion_report(works: list[dict], alex: "OpenAlexCache", top_n: int = 25) -> dict:
    """Citation expansion: co-cited works outside the pool, with titles for the top ones."""
    expansion = cocitation_candidates(works)
    top = expansion[:top_n]
    titles = {}
    if top:
        ids = "|".join(w.rsplit("/", 1)[-1] for w, _ in top)
        listing = alex.get("https://api.openalex.org/works?per-page=50&select=id,title,"
                           f"publication_year,cited_by_count&filter=openalex_id:{ids}") or {}
        titles = {r["id"]: r for r in listing.get("results", [])}
    return {
        "seeds_resolved": len(works),
        "distinct_references": len({r for w in works for r in (w.get("referenced_works") or [])}),
        "cocited_not_in_pool": len(expansion),
        "top_candidates": [{
            "openalex_id": wid, "cited_by_pool_papers": n,
            "title": (titles.get(wid) or {}).get("title"),
            "year": (titles.get(wid) or {}).get("publication_year"),
        } for wid, n in top],
    }


class OpenAlexCache:
    """URL -> response JSON, stored in one file so a run can be replayed offline."""

    def __init__(self, path: Path, replay: bool) -> None:
        self.path = path
        self.replay = replay
        self.data = json.loads(path.read_text()) if path.exists() else {}
        self._fetch = None

    def get(self, url: str) -> dict | None:
        if url in self.data:
            return self.data[url]
        if self.replay:
            raise RuntimeError(f"not in the OpenAlex cache: {url}")
        if self._fetch is None:
            tools = _load("paper_search_tools", TOOLS / "paper_search_tools.py")
            self._fetch = (tools.urllib_fetch, tools.pace)
        fetch, pace = self._fetch
        pace("openalex-completion", 0.15)
        try:
            self.data[url] = json.loads(fetch(url, {"User-Agent": "jiuwenswarm-research"}))
        except Exception as exc:  # 404 for a DOI OpenAlex does not index is an answer
            if "404" not in str(exc):
                raise
            self.data[url] = None
        return self.data[url]

    def save(self) -> None:
        if not self.replay:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1) + "\n",
                                 encoding="utf-8")


# ---------------------------------------------------------------- analyze stage

def screen_paper(work: dict, question: str, stance: dict[str, str], jev,
                 decide: Callable[[str, dict], dict], results: Sequence[str] = ()) -> dict | None:
    """One decision call for one paper.

    Returns the paper's relevance to `question` and, for each candidate
    sentence, the probability that it reports a finding and its stance among
    `stance`. Candidates come from the abstract and, when the paper's full text
    was retrieved, from its results sections (`results`); each claim records
    which. None for a paper without an abstract. `jev` supplies the question
    builders (noul, choice); `decide(state, questions)` sends the call.
    """
    abstract = abstract_text(work.get("abstract_inverted_index"))
    if not abstract:
        return None
    sentences = candidate_sentences(abstract)
    sources = ["abstract"] * len(sentences) + ["full text"] * len(results)
    sentences += list(results)
    state = (f"Research question: {question}\n\nPaper: {work.get('title')} "
             f"({work.get('publication_year')})\nAbstract: {abstract}\n\nCandidate sentences:\n"
             + "\n".join(f"{k}. {s}" + (" (from the results section of the full text)" if src == "full text" else "")
                         for k, (s, src) in enumerate(zip(sentences, sources), 1)))
    questions = {"relevant": jev.noul("Is this paper relevant evidence for the research question?")}
    for k in range(1, len(sentences) + 1):
        questions[f"finding_{k}"] = jev.noul(
            f"Does candidate sentence {k} report a finding the authors observed or proved, "
            "rather than background or a description of what they did?")
        questions[f"stance_{k}"] = jev.choice(
            f"How does candidate sentence {k} bear on the research question?", stance)
    answers = decide(state, questions)
    relevance = answers["relevant"]["noul"]
    claims = []
    for k, (sentence, source) in enumerate(zip(sentences, sources), 1):
        verdict = answers[f"stance_{k}"]
        claims.append({
            "openalex_id": work["id"],
            "title": work.get("title"),
            "sentence": sentence,
            "source": source,
            "paper_relevance": relevance,
            "finding_probability": answers[f"finding_{k}"]["noul"],
            "stance": verdict["choice"],
            "stance_probabilities": verdict["probabilities"],
        })
    return {"relevance": relevance, "claims": claims}


def kept_findings(claims: list[dict]) -> list[dict]:
    """Sentences Jev reads as a finding that bears on the question.

    A sentence is kept when its finding probability is at least 0.5 and its
    stance is not "unrelated". The stance already carries relevance at the
    sentence level, so paper-level relevance is recorded on each candidate
    rather than used as a second gate.
    """
    return [c for c in claims if c["finding_probability"] >= 0.5 and c["stance"] != "unrelated"]
