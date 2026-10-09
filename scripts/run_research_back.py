#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Carry a research-front run through propose, experiment and write to a compiled paper.

    python scripts/run_research_back.py --run ../run_records/front/medical-rag-hallucination [--replay]

It resumes the run log that run_research_front.py left at the propose stage.

  propose     The study is a paper-level evidence synthesis over the findings
              the analyze stage kept. Its design is fixed here, before any
              result exists (study_spec). Jev answers the questions a reviewer
              would put to that design (adversarial_resolution); a design it
              judges unfit for the question stops the run at this stage.
              The method overview figure is generated for the fixed design
              (method_figure): a layout card with no numbers is painted and
              critiqued, and a painting with a visible numeral is never used.
  experiment  Each paper's directional findings decide its stance. The share
              of supporting papers gets an exact two-sided binomial test and a
              Clopper-Pearson interval, overall and on either side of the
              median publication year. Every value the paper may print enters
              the verified_registry with the field it came from. The results
              figure is drawn from those registered literals alone, in the
              plotting convention of the paper's field (result_figures).
  write       The paper's sections and abstract are planned by analogy to
              published syntheses of the field (exemplars: form, never
              wording), and the field's writing profile is chosen from the
              discipline. A JiuwenSwarm DeepAgent (create_deep_agent) writes the
              paper from the plan, the spec, the registry, the search and
              coding protocol as the run performed it, and the cited findings,
              with RigorAuditRail, GovernanceReviewRail and UsageLedgerRail
              mounted. An appendix set from the records lists every included
              paper with its coded findings and stance.
              The delivery gate checks citation keys, DOIs against the DOI
              registry, printed numbers and residue, and demands a certificate
              for its own number check on this draft (auditor_certificate);
              the figure caption faces the same checks as the prose, and
              captions and prose may name only the panels each figure has.
              Findings go back to the writer, at most twice. Three
              reviewers and an area chair then read the delivered draft
              (review_ensemble), and an internal reviewer, a DeepAgent
              holding the capability pack's tools (research_bridge), runs
              the five-axis review panel and the polish review on the
              compiled draft and keeps the findings a rewrite can resolve.
              Both sets of tasks go to the writer once, and the revision is
              kept only if it passes the gate and the same reviewers rate it
              no lower than the draft. tectonic
              compiles the ICLR-template PDF.

Writer, figure, exemplar, review and internal-reviewer answers, Jev answers, DOI lookups and the
compiler's verdicts are cached in the run directory; --replay rebuilds every JSON artifact and the
LaTeX source byte for byte without the network or a key.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "jiuwenswarm/agents/harness/common/tools"
RAILS = REPO / "jiuwenswarm/agents/harness/team/rails"
TEMPLATE = Path(__file__).resolve().parent / "templates/iclr2026"
MAX_REVISIONS = 2


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RESEARCH = REPO / "jiuwenswarm/agents/harness/common/research"
rf = _load("result_figures", RESEARCH / "result_figures.py")
mf = _load("method_figure", RESEARCH / "method_figure.py")
gw = _load("gateway", RESEARCH / "gateway.py")
ex = _load("exemplars", RESEARCH / "exemplars.py")
ft = _load("fulltext", RESEARCH / "fulltext.py")
rev = _load("review_ensemble", RESEARCH / "review_ensemble.py")
SKILLS = REPO / "jiuwenswarm/resources/agent/workspace/skills"
DEFAULT_SECTIONS = [{"name": n, "role": r, "key_moves": []} for n, r in (
    ("Introduction", "introduction"), ("Method", "method"), ("Results", "results"),
    ("Discussion", "discussion"), ("Limitations", "limitations"))]


def _dump(path: Path, obj) -> str:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path.name


# --- statistics (exact, standard library) ---------------------------------------------------

def binom_pmf(k: int, n: int) -> float:
    return math.comb(n, k) / 2 ** n


def binom_test_two_sided(k: int, n: int) -> float:
    """Exact two-sided test of p = 1/2: total probability of outcomes no likelier than k."""
    if n == 0:
        return 1.0
    pk = binom_pmf(k, n)
    return min(1.0, sum(binom_pmf(i, n) for i in range(n + 1) if binom_pmf(i, n) <= pk * (1 + 1e-12)))


def _binom_cdf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))


def clopper_pearson(k: int, n: int, level: float = 0.95) -> tuple[float, float]:
    """Exact interval for a binomial share, by bisection on the binomial tail."""
    a = (1 - level) / 2

    def solve(f) -> float:
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if f(mid) else (lo, mid)
        return (lo + hi) / 2

    lower = 0.0 if k == 0 else solve(lambda p: 1 - _binom_cdf(k - 1, n, p) < a)
    upper = 1.0 if k == n else solve(lambda p: _binom_cdf(k, n, p) > a)
    return lower, upper


def fmt_p(p: float) -> str:
    return "<0.001" if p < 0.001 else f"{p:.3f}"


# --- evidence -> papers -> bibliography -----------------------------------------------------

def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _latex(text: str) -> str:
    return re.sub(r"([&%$#_{}])", r"\\\1", _ascii(text or ""))


def _doi(value: str) -> str:
    """The DOI without resolver prefix or page view (/pdf, /full): the rule of
    paper_search_tools.normalize_doi, which imports the framework a replay runs without."""
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", (value or "").strip(), flags=re.I)
    return re.sub(r"/(?:pdf|epdf|full|fulltext|abstract|html)$", "", value, flags=re.I).lower()


def _norm_title(title: str) -> str:
    return re.sub(r"\W+", "", (title or "").lower())


# Publishers mark a retracted article in its title ("RETRACTED ARTICLE: ..."), and a
# notice about an article (a correction, an erratum) is indexed like a paper.
RETRACTED = re.compile(r"^\s*(?:retracted(?: article)?|retraction(?: note| notice)?|withdrawn|correction|"
                       r"erratum|corrigendum|expression of concern)\b", re.I)


def _person(name: str) -> str | None:
    """An author's name, or None for what an index put in the author list that is not
    a person ("Association for Computational Linguistics 2026"); an OpenReview author
    record cached as text gives its full name. Same rule as paper_search's rows."""
    m = re.search(r"'fullname':\s*'([^']+)'", name or "")
    name = m.group(1) if m else (name or "").strip()
    return name if name and not re.search(r"\d", name) else None


def papers_from_run(run: Path) -> tuple[dict[str, dict], dict[str, int]]:
    """openalex id -> paper metadata, for every paper with a kept finding; and the
    retracted papers left out of the synthesis, title -> kept findings.

    A retracted paper is not evidence. Screening read one as relevant and kept its
    finding; the synthesis counted it until the included-studies appendix put its
    title in front of the reviewers.

    An arXiv paper takes its title and authors from arXiv's own record. OpenAlex
    aggregates and is occasionally wrong about a work: on one run it carried an
    unrelated title and no authors for an arXiv paper, which a join on DOI and
    title alone printed as an anonymous, misnamed reference.
    """
    alex = json.loads((run / "openalex_cache.json").read_text(encoding="utf-8"))
    doi_of = {w["id"]: _doi(w.get("doi"))
              for w in alex.values() if isinstance(w, dict) and "id" in w}
    year_of = {w["id"]: w.get("publication_year") for w in alex.values() if isinstance(w, dict) and "id" in w}
    search = json.loads((run / "paper_search.json").read_text(encoding="utf-8"))["papers"]
    by_doi = {_doi(p["doi"]): p for p in search if p.get("doi")}
    by_arxiv = {p["arxiv_id"].lower(): p for p in search if p.get("arxiv_id")}
    by_title = {_norm_title(p.get("title")): p for p in search}
    evidence = json.loads((run / "evidence_pack.json").read_text(encoding="utf-8"))["by_stance"]
    papers: dict[str, dict] = {}
    retracted: Counter = Counter()
    for stance, items in evidence.items():
        for it in items:
            if RETRACTED.match(it["title"] or ""):
                retracted[it["title"]] += 1
                continue
            oid = it["openalex_id"]
            doi = doi_of.get(oid, "")
            arxiv = re.match(r"10\.48550/arxiv\.(.+)$", doi)
            own = by_arxiv.get(arxiv.group(1)) if arxiv else None
            own = own if own and "arxiv" in (own.get("sources") or []) and own.get("title") else None
            title = own["title"] if own else it["title"]
            meta = own or by_doi.get(doi) or by_title.get(_norm_title(it["title"])) or {}
            p = papers.setdefault(oid, {
                "title": title, "authors": [a for a in map(_person, meta.get("authors") or []) if a],
                "year": year_of.get(oid) or meta.get("year"), "venue": meta.get("venue") or "",
                "doi": doi or _doi(meta.get("doi")), "arxiv_id": meta.get("arxiv_id") or "",
                "findings": [], "counts": Counter(),
            })
            p["findings"].append({"stance": stance, "sentence": it["sentence"]})
            p["counts"][stance] += 1
    return papers, dict(retracted)


def assign_keys(papers: dict[str, dict]) -> None:
    used: Counter = Counter()
    for oid in sorted(papers, key=lambda o: (papers[o]["year"] or 0, papers[o]["title"])):
        p = papers[oid]
        last = _ascii(p["authors"][0].split()[-1]).lower() if p["authors"] else "anon"
        word = next((w for w in re.findall(r"[a-z]+", _ascii(p["title"]).lower()) if len(w) > 3), "paper")
        base = re.sub(r"[^a-z]", "", last) + str(p["year"] or "") + word
        used[base] += 1
        p["key"] = base if used[base] == 1 else f"{base}{chr(ord('a') + used[base] - 1)}"


def _bib_name(name: str) -> str:
    """"Last, First": BibTeX reads a lowercase token in the given name as a particle,
    so "Po-hao Li" written as is cites as "hao Li"."""
    *given, last = _latex(name).split()
    return f"{last}, {' '.join(given)}" if given else last


def bib_entry(p: dict) -> str:
    fields = [f"  title = {{{_latex(p['title'])}}}",
              f"  author = {{{' and '.join(map(_bib_name, p['authors'])) or 'Anonymous'}}}",
              f"  year = {{{p['year']}}}"]
    if p["venue"]:
        fields.append(f"  journal = {{{_latex(p['venue'])}}}")
    if p["doi"]:
        fields.append(f"  doi = {{{p['doi']}}}")
    elif p["arxiv_id"]:
        fields.append(f"  note = {{arXiv:{p['arxiv_id']}}}")
    return f"@article{{{p['key']},\n" + ",\n".join(fields) + "\n}"


STANCES = ("supports", "challenges", "mixed", "method")


def coded_stance(p: dict) -> str:
    """A paper's stance in the synthesis with the counts behind it, stated for the writer."""
    c = p["counts"]
    return (f"coded stance {paper_stance(c)} ({c['supports']} supporting, {c['challenges']} challenging, "
            f"{c['method']} method findings)")


def paper_stance(counts: Counter) -> str:
    s, c = counts["supports"], counts["challenges"]
    if s > c:
        return "supports"
    if c > s:
        return "challenges"
    return "mixed" if s else "method"


# --- the three stages -----------------------------------------------------------------------

def study_spec(question: str, direction: dict) -> dict:
    return {
        "research_question": question,
        # A synthesis answers what the literature reports, not the research question
        # itself; the study question says so, and the adversarial check holds the
        # design to it.
        "study_question": f"What does the published evidence report on: {question}",
        "design": "paper-level vote-count synthesis of the findings kept at analyze",
        "unit": "paper; a paper's stance is the majority of its directional findings, ties are mixed",
        "primary": "share of supporting papers among papers with a direction; exact two-sided binomial test "
                   "of 1/2 and a 95% Clopper-Pearson interval",
        "secondary": "the same share on either side of the median publication year of directional papers",
        "failure_condition": "the interval includes 1/2: the literature pool does not settle the question",
        "analyze_direction": {k: direction.get(k) for k in ("still_open_probability", "room_for")},
        "limitations_required": ["vote counting ignores effect sizes and study quality",
                                 "the pool is what the search returned, not a systematic census"],
    }


def run_synthesis(papers: dict[str, dict]) -> tuple[dict, dict]:
    """experiment_result and the verified registry of every value the paper may print.

    Shares and interval ends are kept as the strings the paper prints ("0.40", not
    0.4): the registry records printed literals, and a value stored as a float
    loses the trailing zero the writer correctly prints.
    """
    stance = {oid: paper_stance(p["counts"]) for oid, p in papers.items()}
    directional = [oid for oid, s in stance.items() if s in ("supports", "challenges")]
    k = sum(stance[o] == "supports" for o in directional)
    n = len(directional)
    lo, hi = clopper_pearson(k, n)
    years = sorted(papers[o]["year"] for o in directional if papers[o]["year"])
    median_year = years[len(years) // 2] if years else None
    halves = {}
    for name, keep in (("earlier", lambda y: y < median_year), ("later", lambda y: y >= median_year)):
        dated = [o for o in papers if papers[o]["year"] and median_year is not None and keep(papers[o]["year"])]
        ids = [o for o in dated if o in directional]
        kk = sum(stance[o] == "supports" for o in ids)
        h_lo, h_hi = clopper_pearson(kk, len(ids)) if ids else (None, None)
        halves[name] = {"supporting": kk, "papers": len(ids),
                        "share": f"{kk / len(ids):.2f}" if ids else None,
                        "ci95": [f"{h_lo:.2f}", f"{h_hi:.2f}"] if ids else None,
                        "p": fmt_p(binom_test_two_sided(kk, len(ids))) if ids else None,
                        "stance_counts": {s: sum(stance[o] == s for o in dated) for s in STANCES}}
    counts = Counter(stance.values())
    result = {
        "papers": len(papers), "findings": sum(sum(p["counts"].values()) for p in papers.values()),
        "stance_counts": dict(counts), "directional": n, "supporting": k,
        "share": f"{k / n:.2f}" if n else None, "ci95": [f"{lo:.2f}", f"{hi:.2f}"],
        "p": fmt_p(binom_test_two_sided(k, n)), "median_year": median_year, "by_year": halves,
        "interval_includes_half": lo <= 0.5 <= hi,
    }
    registry: dict[str, str] = {}

    def bind(value, path: str) -> None:
        if value is None:
            return
        for tok in re.findall(r"\d+(?:\.\d+)?", str(value)):
            registry.setdefault(tok, f"experiment_result.json#{path}")

    for path in ("papers", "findings", "directional", "supporting", "share", "p", "median_year"):
        bind(result[path], path)
    for s, v in counts.items():
        bind(v, f"stance_counts.{s}")
    bind(result["ci95"][0], "ci95[0]")
    bind(result["ci95"][1], "ci95[1]")
    for name, h in halves.items():
        for f in ("supporting", "papers", "share", "p"):
            bind(h[f], f"by_year.{name}.{f}")
        for i, end in enumerate(h["ci95"] or []):
            bind(end, f"by_year.{name}.ci95[{i}]")
        for s, v in h["stance_counts"].items():
            bind(v, f"by_year.{name}.stance_counts.{s}")
    bind(95, "study_spec.primary")      # the interval's level
    bind("0.5", "study_spec.primary")   # the null share
    return result, registry


FIGURE_ID, FIGURE_LABEL = "fig_result", "fig:result"
METHOD_FIGURE_ID, METHOD_FIGURE_LABEL = "fig_method", "fig:method"
PANEL_TASKS = 6   # the internal reviewer's tasks the writer gets, beside the reviewers' own
GATEWAY_MODELS = ("RESEARCH_CHAT_MODEL", "RESEARCH_IMAGE_MODEL", "RESEARCH_IMAGE_SIZE")
SOURCE_NAMES = {"arxiv": "arXiv", "openalex": "OpenAlex", "crossref": "Crossref", "pubmed": "PubMed",
                "semantic_scholar": "Semantic Scholar"}


def method_overview(brief: dict, search: dict) -> tuple[str, str]:
    """What the method figure shows, and its caption: the pipeline that produced this paper, in words.

    Neither text carries a number; the figure is generated, and a generated figure
    shows structure only.
    """
    sources = ", ".join(SOURCE_NAMES.get(p, p) for p in search["providers_answered"])
    method = (
        f"Research question: {brief['question']}\n"
        f"Search: the queries run against {sources}; results are deduplicated and resolved in OpenAlex.\n"
        "Screening: a decision model reads each paper's abstract, decides whether the paper bears on the "
        "question, and labels each finding sentence as supporting, challenging, or a method.\n"
        "Synthesis: each paper takes the majority stance of its directional findings; the share of supporting "
        "papers gets an exact binomial test against an even split and an exact Clopper-Pearson interval, "
        "overall and before and after the median publication year.\n"
        "Delivery: a writer agent drafts the paper from the registered results; a delivery gate checks "
        "citations against the DOI registry, every printed number against the verified registry, and "
        "leftover marks, and returns its findings to the writer until the draft passes.")
    caption = ("Overview of the study. The queries search scholarly indexes; a decision model screens each "
               "paper and labels its findings; each paper takes the stance of its findings and an exact test "
               "summarizes the stances; a delivery gate checks citations and printed numbers before the paper "
               "is released.")
    return method, caption


FULLTEXT_ROUTES = {"arxiv": "arXiv HTML", "pmc": "Europe PMC", "openalex": "OpenAlex open-access links",
                   "unpaywall": "Unpaywall"}


def search_protocol(run: Path, brief: dict, search: dict, result: dict, registry: dict,
                    retracted: dict[str, int]) -> str:
    """The search, screening and coding as this run performed them, for the Methods.

    Reviewers of the first runs asked each paper for its search date, sources,
    criteria and coding procedure; a writer not given them made them up (one
    revision printed search dates of its own). The run holds them, so the writer
    gets them, and every value they state enters the registry with its record.
    """
    front = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    routes = json.loads((run / "acquisition_report.json").read_text(encoding="utf-8"))["fulltext"]["by_route"]
    date = datetime.fromisoformat(front["generated_at"]).date().isoformat()   # locale-free
    for value, ref in ((date, "manifest.json#generated_at"), (len(search["papers"]), "paper_search.json#papers"),
                       (front["pool"], "manifest.json#pool"), (front["fulltext_retrieved"], "manifest.json#fulltext_retrieved"),
                       (front["claims"], "manifest.json#claims"), (front["kept"], "manifest.json#kept"),
                       (len(retracted), "experiment_result.json#retracted_left_out"),
                       (sum(retracted.values()), "experiment_result.json#retracted_left_out")):
        for tok in re.findall(r"\d+", str(value)):
            registry.setdefault(tok, ref)
    # method_overview keeps the names its cached figure requests were made with.
    names = dict(SOURCE_NAMES, openreview="OpenReview", google_scholar="Google Scholar")
    sources = ", ".join(names.get(p, p) for p in search["providers_answered"])
    return "\n".join([
        f"- Searched on {date} in {sources}, with the queries "
        + "; ".join(f'"{q}"' for q in brief["queries"]) + ". Records were merged across sources and deduplicated "
        "by DOI, then arXiv id, Semantic Scholar id, OpenAlex id, PubMed id, and finally title with year.",
        f"- The search returned {len(search['papers'])} records; the {front['pool']} with a DOI or arXiv id formed "
        "the pool, and OpenAlex supplied each one's abstract, publication year and references.",
        f"- Open-access full text was sought for every paper ({', '.join(FULLTEXT_ROUTES.get(r, r) for r in routes)}) "
        f"and retrieved for {front['fulltext_retrieved']}. Up to three sentences of each abstract most likely to "
        "report a result, and up to three from the results section of each retrieved full text, became candidate "
        "findings.",
        "- Coding: one call per paper to Jev, a decision model (TypeSafe) that answers fixed multiple-choice "
        "questions with probabilities and writes no text. For each candidate sentence it judged whether the "
        "sentence reports a finding the authors observed (kept at a probability of 0.5 or above) and how it bears "
        "on the question: supports, challenges, method, or unrelated (dropped). No person coded or adjudicated "
        f"any finding. Of {front['claims']} candidate sentences, {front['kept']} were kept as findings"
        + (f"; {len(retracted)} paper(s) whose title marks a retraction or a publisher's notice, with "
           f"{sum(retracted.values())} kept finding(s), were then left out, leaving {result['findings']} findings "
           f"from {result['papers']} papers."
           if retracted else f", from {result['papers']} papers."),
        "- The study design was written by the analysis code and entered in the run log before the synthesis "
        "produced any result; it was not registered externally.",
    ])


ADMIN_FACTS = """- Data and code: the run directory released with this paper holds every search result, each decision-model
  answer with its probabilities, the analysis code and the figure specifications; replaying the run offline
  reproduces every number in the paper.
- Contributions: an automated run of the JiuwenSwarm research-paper team searched, screened, coded, analysed and
  drafted this paper, and its delivery gate checked the citations and printed numbers. No person edited the text.
- Ethics: the study uses published literature only, with no human participants and no identifiable data.
- Funding and competing interests: none declared for this automated run."""

STUDIES_LABEL = "app:studies"


def studies_appendix(papers: dict, registry: dict) -> str:
    """Every included paper with its findings by stance and the stance it takes: the
    coding behind the synthesis, set from the records like the results figure."""
    rows = []
    for p in sorted(papers.values(), key=lambda p: (p["year"] or 0, p["key"])):
        c = p["counts"]
        rows.append(f"\\citet{{{p['key']}}} & {c['supports']} & {c['challenges']} & {c['method']} & "
                    f"{paper_stance(c)} \\\\")
        for s in ("supports", "challenges", "method"):
            registry.setdefault(str(c[s]), f"evidence_pack.json#by_stance.{s}")
    return (f"\\section{{Included studies}}\\label{{{STUDIES_LABEL}}}\n"
            "Each paper with a kept finding, the number of its kept findings of each stance, and the stance it "
            "takes in the synthesis (the majority of its directional findings).\n\n"
            "\\begin{center}\\small\n\\begin{tabular}{lrrrl}\n\\hline\n"
            "Study & Supports & Challenges & Method & Paper stance \\\\\n\\hline\n"
            + "\n".join(rows) + "\n\\hline\n\\end{tabular}\n\\end{center}\n")


def result_figure(result: dict, registry: dict, style: dict) -> dict:
    """The results figure as a spec of recorded literals: (a) the shares with their
    intervals, (b) papers by stance before and after the median year.

    Every number the figure shows, as a mark or as text, must be a registry entry;
    the writer may then cite the figure without the figure adding an untraced value.
    """
    year = result["median_year"]
    halves = [(f"Before {year}", result["by_year"]["earlier"]), (f"{year} on", result["by_year"]["later"])]
    rows = [{"label": f"All ({result['directional']})", "value": result["share"],
             "lo": result["ci95"][0], "hi": result["ci95"][1], "filled": not result["interval_includes_half"]}]
    rows += [{"label": f"{name} ({h['papers']})", "value": h["share"], "lo": h["ci95"][0], "hi": h["ci95"][1],
              "filled": not float(h["ci95"][0]) <= 0.5 <= float(h["ci95"][1])}
             for name, h in halves if h["papers"]]
    series = [s for s in STANCES if result["stance_counts"].get(s)]
    spec = {
        "figure_id": FIGURE_ID, "label": FIGURE_LABEL, "style_profile": style["style_profile"], "style": style,
        "size": [5.5, 2.1],
        "caption": (f"Paper-level synthesis of the kept findings. (a) Share of supporting papers among the "
                    f"{result['directional']} papers with a direction, overall and on either side of the median "
                    f"publication year {year}, with exact 95\\% Clopper--Pearson intervals; the dashed line marks "
                    f"a share of 0.5 and filled markers have intervals that exclude it. (b) Papers by stance in "
                    f"the same two periods."),
        "panels": [
            {"kind": "forest", "rows": rows, "ref": "0.5", "xlabel": "Share of supporting papers", "width": 1.15},
            {"kind": "grouped_bar", "categories": [name for name, _ in halves], "series": series,
             "values": {s: [str(h["stance_counts"][s]) for _, h in halves] for s in series}, "ylabel": "Papers"},
        ],
    }
    untraced = [t for t in rf.printed_tokens(spec) if t not in registry]
    if untraced:
        raise ValueError(f"the result figure would show unregistered values: {untraced}")
    return spec


WRITER_SYSTEM = (
    "You are the writer of a research team. You write a short, careful ICLR-style paper in LaTeX "
    "from evidence that other team members have already gathered and analysed. You never invent "
    "numbers, citations or results."
)


def layout_from(anchored: dict, profile: dict) -> dict:
    """The sections and abstract the writer follows: the exemplar plan, or the default
    sections when no exemplar anchored one; a section of each figure's role is kept."""
    sections = [{"name": str(x.get("name", "")).strip(), "role": x.get("role") if x.get("role") in ex.ROLES else "other",
                 "key_moves": [str(m) for m in x.get("key_moves") or []], "follows": x.get("follows") or []}
                for x in (anchored.get("plan") or {}).get("sections") or [] if str(x.get("name", "")).strip()]
    roles = {x["role"] for x in sections}
    if not {"method", "results"} <= roles:
        sections = DEFAULT_SECTIONS
    return {"sections": sections, "abstract_elements": (anchored.get("plan") or {}).get("abstract_elements") or [],
            "domain": profile["skill"], "guidance": profile["guidance"]}


def section_for(layout: dict, role: str) -> str:
    return next(x["name"] for x in layout["sections"] if x["role"] == role)


def writer_prompt(brief: dict, spec: dict, result: dict, registry: dict, papers: dict, figures: list,
                  layout: dict, protocol: str) -> str:
    plan = "\n".join(f"\\section{{{x['name']}}}" + (": " + "; ".join(x["key_moves"]) if x["key_moves"] else "")
                     for x in layout["sections"])
    elements = (" with its content in this order: " + ", ".join(layout["abstract_elements"])
                + " (an order for the prose; these names are never printed as labels)"
                if layout["abstract_elements"] else "")
    # Each paper's stance is stated, not left to be read off its findings: a writer
    # asked to name the supporting papers once named a method paper among them.
    cited = []
    for p in sorted(papers.values(), key=lambda p: p["key"]):
        lines = "; ".join(f"[{f['stance']}] {f['sentence']}" for f in p["findings"][:3])
        cited.append(f"- \\citep{{{p['key']}}} {p['title']} ({p['venue'] or 'n/a'}); {coded_stance(p)}: {lines}")
    return f"""Write the paper for this research question:
{brief['question']}

Study design (fixed before the results):
{json.dumps(spec, indent=1)}

Results (from experiment_result.json):
{json.dumps(result, indent=1)}

Numbers you may print, exactly as written here and no others (no section numbers, and no year or other
digits that are not in this list): {", ".join(sorted(registry, key=float))}.

Papers you may cite, only with these keys and only for what the listed findings say; when the paper names
which papers support or challenge, it follows each paper's coded stance exactly:
{chr(10).join(cited)}

Figures placed for you, each at the start of its section:
{chr(10).join(f"- {f['section']}: Figure~{chr(92)}ref{{{f['label']}}}, caption: {f['caption']}" for f in figures)}
Refer to each figure in its section with its Figure~\\ref and describe what it shows.
An appendix placed for you after the references lists every included paper with the number of its kept findings
of each stance and its paper stance; refer to it as Appendix~\\ref{{{STUDIES_LABEL}}} where the methods describe
the coding.

Search, screening and coding, as this run performed them; report them in the methods so the search can be
repeated, and describe nothing about the procedure beyond them:
{protocol}

Facts for any availability, contribution, ethics, funding or competing-interest section; state these and
never mention prompts, supplied materials or anything you were not told:
{ADMIN_FACTS}

Sections, in this order, each doing the jobs listed (planned from published papers of this kind; their
form, never their wording):
{plan}

Writing conventions of this field ({layout['domain']}):
{layout['guidance']}

Output exactly four blocks and nothing else:
<title>a specific title</title>
<abstract>150-220 words{elements}</abstract>
<body>LaTeX with exactly the sections above, under exactly those headings; 1100-1700 words; cite with \\citep{{key}} or
\\citet{{key}}; state the failure condition and whether it was met; name every limitation the design
requires. Describe the design as fixed in the analysis code before any result, not as preregistered. Write
each p-value in math mode as $p =$ or $p <$ followed by its listed value. Write no figure or table environment
of your own and no URLs. Write a percent sign as \\% and less-than as $<$.</body>
<support>the support level of each claim, one line per claim, as the governance rules ask; these labels
are working notes and never appear in the title, abstract or body</support>"""


REVIEW_REVISION_PROMPT = """Reviewers read your paper and the area chair asks for these revisions. Carry them
out by rewriting only: print no number outside this list, {numbers}; add no citation; keep every section
heading, figure reference and the appendix reference. A task about how the study was run is answered from
these facts and from nothing else; add no step, rule, category or check to the description of how the study
was done beyond them. A distinction the reviewers ask for is interpretation: it belongs in the discussion or
the limitations, not in the methods as something the study did.
{protocol}
{admin}
Each paper's coded stance, which any statement of which papers support or challenge follows exactly:
{stances}

Output the same four blocks.

Revision tasks:
{tasks}

Your paper:
{draft}"""


REVISION_PROMPT = """The delivery gate refused your draft. Revise it so that every finding below is
resolved, keeping everything else unchanged, and output the same four blocks.

Findings:
{findings}

Your draft:
{draft}"""


def blocks(text: str) -> dict[str, str]:
    out = {}
    for name in ("title", "abstract", "body", "support"):
        m = re.search(rf"<{name}>(.*?)</{name}>", text, re.S)
        out[name] = html.unescape(m.group(1).strip()) if m else ""
    return out


class Writer:
    """The DeepAgent writer, with answers cached by prompt so a replay needs no model."""

    def __init__(self, run: Path, replay: bool, on_usage, model: str) -> None:
        self.cache_path = run / "writer_cache.jsonl"
        self.cache = {}
        if self.cache_path.exists():
            for line in self.cache_path.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                self.cache[rec["key"]] = rec
        self.replay = replay
        self.on_usage = on_usage
        self.model = model
        self.rigor_findings: list[str] = []
        self.used: set[str] = set()

    def _agent(self, step: str):
        from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
        from openjiuwen.core.single_agent import AgentCard
        from openjiuwen.harness.factory import create_deep_agent

        from jiuwenswarm.agents.harness.team.rails.governance_review_rail import GovernanceReviewRail
        from jiuwenswarm.agents.harness.team.rails.rigor_audit_rail import RigorAuditRail
        from jiuwenswarm.agents.harness.team.rails.usage_ledger_rail import UsageLedgerRail

        model = Model(
            model_client_config=ModelClientConfig(
                client_provider="OpenAI", api_key=os.environ["OPENAI_API_KEY"],
                api_base=os.environ["OPENAI_BASE_URL"], timeout=900, verify_ssl=False),
            model_config=ModelRequestConfig(model=self.model, temperature=0.0))
        self.rigor = RigorAuditRail(language="en")
        self.ledger_rows: list[dict] = []
        rails = [GovernanceReviewRail(language="en"), self.rigor,
                 UsageLedgerRail(self.ledger_rows.append, stage="write", step=step)]
        return create_deep_agent(model, card=AgentCard(name="writer", id="writer"), system_prompt=WRITER_SYSTEM,
                                 tools=[], rails=rails, max_iterations=1, add_general_purpose_agent=False,
                                 enable_llm_retry_rail=False,
                                 workspace=str(self.cache_path.parent / "writer_workspace"))

    def __call__(self, prompt: str, step: str) -> str:
        key = hashlib.sha256(f"{self.model}\n{WRITER_SYSTEM}\n{prompt}".encode()).hexdigest()
        if key not in self.cache:
            if self.replay:
                raise SystemExit(f"--replay: writer request {key[:12]} is not cached")
            for attempt in range(3):   # a dropped gateway connection is not the writer's answer
                agent = self._agent(step)
                try:
                    out = asyncio.run(agent.invoke({"query": prompt}))
                    break
                except Exception as exc:  # noqa: BLE001 - the framework wraps transport errors
                    if attempt == 2:
                        raise
                    print(f"writer {step}: {exc}; retrying")
                    time.sleep(30 * (attempt + 1))
            rec = {"key": key, "step": step, "output": out.get("output", ""),
                   "usage": self.ledger_rows, "rigor_findings": [f.render() for f in self.rigor.findings]}
            with self.cache_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.cache[key] = rec
        rec = self.cache[key]
        for row in rec["usage"]:
            self.on_usage(dict(row, source_record=f"writer_cache.jsonl#{key[:16]}"))
        self.rigor_findings += rec["rigor_findings"]
        self.used.add(key)
        return rec["output"]

    def superseded_tokens(self) -> int:
        """Tokens spent on cached answers this run did not use (earlier prompts); they were still paid for."""
        return sum(r["total_tokens"] for k, rec in self.cache.items() if k not in self.used for r in rec["usage"])


PANEL_SYSTEM = (
    "You are the internal reviewer of a research team. You check a delivered manuscript with the review "
    "tools you hold and turn what they find into revision tasks for the writer. You never edit the "
    "manuscript yourself."
)

PANEL_PROMPT = """The writer's delivered manuscript is {tex}.
1. Run arena_panel on it with out_path {panel} and scope "experimental".
2. Run polish_review on it with out_path {polish} and venue "ICLR".
3. Read both outputs with read_findings.
Keep only findings the writer can resolve by rewriting the text: a claim stated beyond its evidence, an
undefined term, a missing limitation, an inconsistency between two passages. Drop any finding that would
need new data, a new analysis, a new citation or a number not already in the manuscript.
Answer with one block and nothing else:
<tasks>
- one revision task per line, at most {most}, each naming the passage it concerns
</tasks>"""


def panel_review(run: Path, tex: Path, replay: bool, model: str, charge) -> dict:
    """A DeepAgent holding the capability pack's review tools reads the delivered draft.

    Its answer is cached by prompt, together with every model call it and its tools
    made, so a replay re-books the same calls without a model.
    """
    from jiuwenswarm.agents.harness.common.research_bridge.model_transport import VENDOR, Transport

    work = run / "panel"
    paths = {"tex": tex, "panel": work / "arena_panel.json", "polish": work / "polish_review.json"}
    prompt = PANEL_PROMPT.format(**paths, most=PANEL_TASKS)
    # keyed by the paths relative to the run, so a copied run directory replays
    relative = PANEL_PROMPT.format(**{k: v.relative_to(run) for k, v in paths.items()}, most=PANEL_TASKS)
    key = hashlib.sha256(f"{model}\n{PANEL_SYSTEM}\n{relative}".encode()).hexdigest()
    cache_path = run / "panel_cache.jsonl"
    cache = {r["key"]: r for r in map(json.loads, cache_path.read_text().splitlines())} if cache_path.exists() else {}
    if key not in cache:
        if replay:
            raise SystemExit(f"--replay: panel request {key[:12]} is not cached")
        if not (Path(os.environ.get("JIUWENSWARM_VENDOR") or VENDOR) / "research_harness").exists():
            return {"skipped": "the capability pack (vendor/) is not installed", "tasks": []}
        from openjiuwen.core.single_agent import AgentCard
        from openjiuwen.harness.factory import create_deep_agent

        from jiuwenswarm.agents.harness.common.research_bridge.model_transport import build_model
        from jiuwenswarm.agents.harness.common.research_bridge.tools import build_tools
        from jiuwenswarm.agents.harness.team.rails.usage_ledger_rail import UsageLedgerRail

        transport = Transport(work, replay=False, stage="write")
        transport.install()
        tools = [t for t in build_tools(transport) if t.card.name in ("arena_panel", "polish_review", "read_findings")]
        rows: list[dict] = []
        agent = create_deep_agent(build_model(model, 0.0), card=AgentCard(name="reviewer", id="reviewer"),
                                  system_prompt=PANEL_SYSTEM, tools=tools,
                                  rails=[UsageLedgerRail(rows.append, stage="write", step="panel_review")],
                                  max_iterations=8, workspace=str(work / "agent_workspace"),
                                  add_general_purpose_agent=False, enable_llm_retry_rail=False)
        out = asyncio.run(agent.invoke({"query": prompt}))
        output = out.get("output", "") if isinstance(out, dict) else str(out or "")
        rows = [dict(r, source_record=f"panel_cache.jsonl#{key[:16]}") for r in rows]
        rows += [dict(r, source_record=f"panel/{r['source_record']}") for r in transport.rows]
        cache[key] = {"key": key, "output": output, "usage": rows}
        with cache_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(cache[key], ensure_ascii=False) + "\n")
    rec = cache[key]
    for row in rec["usage"]:
        charge(row)
    m = re.search(r"<tasks>(.*?)</tasks>", rec["output"], re.S)
    tasks = [ln.strip()[2:].strip() for ln in (m.group(1) if m else "").splitlines() if ln.strip().startswith("- ")]
    return {"tasks": tasks[:PANEL_TASKS], "agent": "reviewer", "tools": ["arena_panel", "polish_review", "read_findings"],
            "calls": len(rec["usage"]), "outputs": ["panel/arena_panel.json", "panel/polish_review.json"]}


def certify_number_check(gate, cert, prose: str, registry: dict) -> dict:
    """Certificate for the delivery gate's number check on this draft's own printed values."""
    printed = {tok for tok in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", prose) if tok in registry}
    universe = {f"v{i}": tok for i, tok in enumerate(sorted(printed, key=float))}

    def render(facts: dict) -> str:
        text = prose
        for slot, value in facts.items():
            if value != universe[slot]:
                text = re.sub(r"(?<![\w.])%s(?![\w]|\.\d)" % re.escape(universe[slot]), value, text)
        return text

    def audit(text: str) -> list[str]:
        return [f.detail.split(" ", 1)[0] for f in gate.numbers_trace_to_registry(text, registry)]

    try:
        return cert.certify(universe, render, audit, name="registry_trace", rounds=20,
                            null_rounds=20, seed=0).to_record()
    except ValueError as exc:   # too few printed values to plant in
        return {"auditor": "registry_trace", "certified": False, "e_final": 1.0, "rounds": 0,
                "alpha": 0.05, "reason": str(exc)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True, help="a run_research_front.py output directory")
    ap.add_argument("--model", default=os.environ.get("WRITER_MODEL", "gpt-5.5"))
    ap.add_argument("--replay", action="store_true")
    args = ap.parse_args()
    run = args.run

    wf = _load("workflow_run_log", RAILS / "workflow_run_log.py")
    gate = _load("delivery_gate", RAILS / "delivery_gate.py")
    cert = _load("auditor_certificate", RAILS / "auditor_certificate.py")
    jev = _load("jev_decision", TOOLS / "jev_decision.py")

    # Clients first: a missing key stops the run before the log is touched.
    client = jev.JevClient(cache_path=run / "jev_cache.jsonl", replay=args.replay)

    # The front's records end at its refused propose advance; anything after it is
    # a previous back half, which this run replaces.
    log_path = run / "workflow_run.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    stop = next(i for i, ln in enumerate(lines)
                if '"stage_advance"' in ln and '"propose"' in ln and '"refused"' in ln)
    log_path.write_text("\n".join(lines[: stop + 1]) + "\n", encoding="utf-8")
    manifest_path = run / "back_manifest.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    generated_at = previous.get("generated_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    if args.replay:
        # A replay asks the models the run asked: the cache is keyed by them.
        args.model = previous.get("model", args.model)
        os.environ.update(previous.get("models", {}))
    log = wf.WorkflowRunLog.resume(log_path, clock=lambda: generated_at)
    assert log.stage == "propose", log.stage

    usage: list[dict] = []

    def charge(row: dict) -> None:
        row = dict(row, ts=generated_at)
        usage.append(row)
        log.record_usage(stage=row["stage"], total_tokens=row["total_tokens"], source_record=row["source_record"])

    def record(stage: str, kind: str, obj) -> None:
        log.record_artifact(kind, stage=stage, ref=_dump(run / f"{kind}.json", obj))

    # The writer, planner and critic share one OpenAI-format gateway unless told otherwise.
    os.environ.setdefault("OPENAI_BASE_URL", os.environ.get("GPT_IMAGE_BASE_URL", ""))
    os.environ.setdefault("OPENAI_API_KEY", os.environ.get("GPT_IMAGE_API_KEY", ""))
    if os.environ.get("GPT_IMAGE_MODEL"):
        os.environ.setdefault("RESEARCH_IMAGE_MODEL", os.environ["GPT_IMAGE_MODEL"])
    probe = gw.CachedGateway(run / "figure_cache.jsonl", True)   # the models it resolves, recorded below
    for k, v in zip(GATEWAY_MODELS, (probe.chat_model, probe.image_model, probe.image_size)):
        os.environ.setdefault(k, v)
    paper_dir = run / "paper"
    paper_dir.mkdir(exist_ok=True)
    brief = json.loads((run / "topic_brief.json").read_text(encoding="utf-8"))
    search = json.loads((run / "paper_search.json").read_text(encoding="utf-8"))
    direction = json.loads((run / "direction_proposal.json").read_text(encoding="utf-8"))
    papers, retracted = papers_from_run(run)
    assign_keys(papers)

    # propose -------------------------------------------------------------
    spec = study_spec(brief["question"], direction)
    state = (f"Research question: {brief['question']}\n\nProposed study:\n{json.dumps(spec, indent=1)}\n\n"
             f"Evidence available: {sum(sum(p['counts'].values()) for p in papers.values())} findings "
             f"from {len(papers)} papers.")
    questions = {
        "design_fits": jev.noul("Can this vote-count synthesis answer its study question, what the published "
                                "evidence reports, with the evidence available?"),
        "publication_bias": jev.noul("Is the published evidence on this question likely skewed toward positive "
                                     "results, so the paper must report that as a limitation?"),
    }
    response = client.decide(state, questions)
    charge(jev.usage_row(response, stage="propose", step="adversarial_review",
                         source_record=f"jev_cache.jsonl#{jev.request_key(client.model, state, questions)[:16]}"))
    answers = response["answers"]
    fits = answers["design_fits"]["noul"]
    bias = answers["publication_bias"]["noul"]
    if bias >= 0.5:
        spec["limitations_required"].append("published findings on this question are likely skewed toward "
                                            "positive results")
    record("propose", "study_spec", spec)
    record("propose", "adversarial_resolution", {
        "challenges": {"design_fits": fits, "publication_bias": bias},
        "decision": "proceed" if fits >= 0.5 else "stop",
        "rule": "proceed when Jev puts the design's fit at 0.5 or above; a likely publication bias "
                "becomes a required limitation",
    })
    if fits < 0.5:
        print(f"propose: Jev judges the synthesis unfit for the question (p={fits}); stopped")
        return 1
    # With the design fixed, the method overview figure: generated, so it shows structure only.
    method_text, method_caption = method_overview(brief, search)
    gateway = gw.CachedGateway(run / "figure_cache.jsonl", args.replay, lambda row: charge(dict(row, stage="propose")))
    method_figure = mf.generate(method_text, method_caption, gateway)
    figures = []
    if method_figure["accepted"]:
        placed = paper_dir / f"{METHOD_FIGURE_ID}.png"
        if not (args.replay and placed.exists()):   # a replay keeps the placed file, as it keeps drawings
            placed.write_bytes(mf.trim_whitespace((run / method_figure["accepted"]).read_bytes()))
        figures.append({"id": METHOD_FIGURE_ID, "file": f"{METHOD_FIGURE_ID}.png", "label": METHOD_FIGURE_LABEL,
                        "caption": method_caption, "section": "Method", "panels": 1})
    record("propose", "method_figure", dict(method_figure, method=method_text, caption=method_caption,
                                            file=f"paper/{METHOD_FIGURE_ID}.png" if method_figure["accepted"] else None))
    log.advance()

    # experiment ----------------------------------------------------------
    result, registry = run_synthesis(papers)
    result["retracted_left_out"] = retracted
    protocol = search_protocol(run, brief, search, result, registry, retracted)
    studies = studies_appendix(papers, registry)
    record("experiment", "experiment_code", {"ref": "scripts/run_research_back.py#run_synthesis",
                                             "tests": "binom_test_two_sided, clopper_pearson"})
    record("experiment", "experiment_result", result)
    record("experiment", "verified_registry", registry)
    # The paper is set in the ICLR template; the discipline, when the brief names
    # one, decides the field's plotting convention.
    figure = result_figure(result, registry, rf.resolve_style("ICLR", brief.get("discipline", "")))
    drawn = [f"paper/{FIGURE_ID}.{x}" for x in ("pdf", "png")]
    if args.replay and all((run / f).exists() for f in drawn):
        # A replay re-derives the figure spec and keeps the run's drawing, as it keeps
        # the run's PDF: a drawing is fixed by its spec, but its bytes also depend on
        # the matplotlib version, and a replay needs none installed.
        pass
    elif rf.render(figure, paper_dir) is None:
        raise SystemExit("matplotlib is needed to draw the result figure: pip install matplotlib")
    record("experiment", "result_figures", dict(figure, files=drawn))
    figures.append({"id": FIGURE_ID, "file": f"{FIGURE_ID}.pdf", "label": FIGURE_LABEL,
                    "caption": figure["caption"], "section": "Results", "panels": len(figure["panels"])})
    log.advance()

    # write ---------------------------------------------------------------
    writer = Writer(run, args.replay, charge, args.model)
    bib = {p["key"]: bib_entry(p) for p in papers.values()}
    doi_cache_path = run / "doi_cache.json"
    doi_cache = json.loads(doi_cache_path.read_text()) if doi_cache_path.exists() else {}
    resolve = ((lambda ident: doi_cache.get(ident)) if args.replay
               else gate.doi_registry_resolver(cache=doi_cache))

    for f in TEMPLATE.iterdir():
        shutil.copy(f, paper_dir / f.name)
    (paper_dir / "references.bib").write_text("\n\n".join(bib[k] for k in sorted(bib)) + "\n", encoding="utf-8")

    compile_cache_path = run / "compile_cache.json"
    compile_cache = json.loads(compile_cache_path.read_text()) if compile_cache_path.exists() else {}

    def compile_pdf(parts: dict) -> str | None:
        """Write main.tex and compile it; the first LaTeX error, or None when the PDF built."""
        (paper_dir / "main.tex").write_text(
            "\\documentclass{article}\n\\usepackage{iclr2026_conference,times}\n\\usepackage{hyperref}\n"
            "\\usepackage{url}\n\\usepackage{graphicx}\n\\iclrfinalcopy\n"
            f"\\title{{{parts['title']}}}\n\\author{{JiuwenSwarm research-paper team (automated run)}}\n"
            "\\begin{document}\n\\maketitle\n"
            f"\\begin{{abstract}}\n{parts['abstract']}\n\\end{{abstract}}\n\n{with_figures(parts['body'])}\n\n"
            "\\bibliography{references}\n\\bibliographystyle{iclr2026_conference}\n"
            f"\\appendix\n{studies}\\end{{document}}\n",
            encoding="utf-8")
        # The compiler's verdict is an external answer like any other: cached by the
        # source's hash, so a replay reaches the same delivery decision with or
        # without a compiler installed.
        key = hashlib.sha256((paper_dir / "main.tex").read_bytes()).hexdigest()
        if args.replay and key in compile_cache:
            if shutil.which("tectonic"):   # rebuild the PDF; the verdict stays the run's
                subprocess.run(["tectonic", "-X", "compile", "main.tex"], cwd=paper_dir, capture_output=True)
            return compile_cache[key]
        if not shutil.which("tectonic"):
            if args.replay:
                print("tectonic is not installed: main.tex rebuilt, the run's PDF kept")
                return None
            return "tectonic is not installed"
        done = subprocess.run(["tectonic", "-X", "compile", "main.tex"], cwd=paper_dir, capture_output=True, text=True)
        error = None if done.returncode == 0 else next(
            (ln for ln in (done.stdout + done.stderr).splitlines() if ln.startswith("error")), "compile failed")
        compile_cache[key] = error
        return error

    def with_figures(body: str) -> str:
        """The body with each figure placed at the start of its section (at the end when the section is missing)."""
        for f in figures:
            env = ("\\begin{figure}[t]\n\\centering\n"
                   f"\\includegraphics[width=\\linewidth]{{{f['file']}}}\n"
                   f"\\caption{{{f['caption']}}}\n\\label{{{f['label']}}}\n\\end{{figure}}\n")
            m = re.search(r"\\section\*?\{%s\}[^\n]*\n" % f["section"], body)
            body = body[:m.end()] + env + body[m.end():] if m else body + "\n" + env
        return body

    # Exemplar-anchored plan of the paper's architecture, and the field's writing profile.
    profile = ex.domain_profile(brief.get("discipline", ""), SKILLS)
    target = (f"Research question: {brief['question']}\nField: {brief.get('discipline') or 'not stated'}\n"
              f"Study: {spec['design']}; {spec['primary']}\nFigures: "
              + "; ".join(f"{f['label']} ({f['section'].lower()})" for f in figures))
    exemplar_gateway = gw.CachedGateway(run / "exemplar_cache.jsonl", args.replay,
                                        lambda row: charge(dict(row, stage="write")))
    gateways = [gateway, exemplar_gateway]
    anchored = ex.anchor(list(dict.fromkeys([brief["queries"][0], brief["queries"][-1]]
                                            + ([brief["discipline"]] if brief.get("discipline") else []))),
                         target, exemplar_gateway,
                         run / "exemplar_sources.json", args.replay, ft.fetch,
                         get_json=lambda url: json.loads(gw.fetch(url, timeout=60, headers={
                             "User-Agent": "jiuwenswarm-research/1.0"})))
    layout = layout_from(anchored, profile)
    for f in figures:
        f["section"] = section_for(layout, "method" if f["id"] == METHOD_FIGURE_ID else "results")
    record("write", "exemplar_plan", dict(anchored, domain_profile=profile["skill"], layout=layout["sections"]))

    def deliver(draft: str, attempt) -> tuple[dict, list, list]:
        """Gate one draft and compile it: its blocks, the gate's findings, the missing blocks."""
        parts = blocks(draft)
        # Captions and the appendix are submission-facing text too: their citations and
        # numbers face the same checks.
        prose = "\n\n".join([parts["abstract"], parts["body"]] + [f["caption"] for f in figures] + [studies])
        certificate = certify_number_check(gate, cert, prose, registry)
        findings = gate.delivery_ready(prose, bib, registry, resolve=resolve, certificate=certificate,
                                       figures=figures)
        findings += [gate.DeliveryFinding("figure_reference", f"the body never refers to Figure~\\ref{{{f['label']}}}, "
                                                              f"placed in {f['section']}")
                     for f in figures if parts["body"] and f"\\ref{{{f['label']}}}" not in parts["body"]]
        # The figures and the appendix are placed for the writer; one it draws in itself prints twice.
        if re.search(r"\\includegraphics|\\begin\{(?:figure|table)\*?\}", parts["body"]):
            findings.append(gate.DeliveryFinding("own_figure", "the body places a figure or table of its own; "
                                                 "the figures are placed for you and are only referred to"))
        if parts["body"] and f"\\ref{{{STUDIES_LABEL}}}" not in parts["body"]:
            findings.append(gate.DeliveryFinding("appendix_reference", "the body never refers to "
                                                 f"Appendix~\\ref{{{STUDIES_LABEL}}}, the included studies"))
        headings = re.findall(r"\\section\*?\{([^}]*)\}", parts["body"])
        findings += [gate.DeliveryFinding("section_plan", f"the planned section {x['name']} is missing")
                     for x in layout["sections"] if parts["body"] and x["name"] not in headings]
        missing = [name for name in ("title", "abstract", "body") if not parts[name]]
        error = None if missing else compile_pdf(parts)
        if error:
            findings.append(gate.DeliveryFinding("latex_compile", error))
        log.record_delivery(findings)
        attempts.append({"attempt": attempt, "findings": [f.render() for f in findings], "missing_blocks": missing,
                         "certificate": {k: certificate.get(k) for k in ("certified", "certified_at_round",
                                                                          "rejections", "rounds", "e_final",
                                                                          "false_accusation_upper",
                                                                          "collision_free_slots")}})
        return parts, findings, missing

    draft = writer(writer_prompt(brief, spec, result, registry, papers, figures, layout, protocol), "draft")
    attempts: list[dict] = []
    for attempt in range(MAX_REVISIONS + 1):
        parts, findings, missing = deliver(draft, attempt)
        # The gate's own certificate is a property of this draft's printed values; a
        # draft that prints too few cannot be certified, and that is the writer's to fix.
        if (not findings and not missing) or attempt == MAX_REVISIONS:
            break
        draft = writer(REVISION_PROMPT.format(
            findings="\n".join(f"- {f.render()}" for f in findings) or "- a block is missing: " + ", ".join(missing),
            draft=draft), f"revision_{attempt + 1}")
    ready = not findings and not missing

    # Reviewers and an area chair read the delivered draft; their revision tasks go to
    # the writer once, and the revision replaces the draft only if it passes the gate
    # and the same reviewers rate it no lower.
    if ready:
        review_gateway = gw.CachedGateway(run / "review_cache.jsonl", args.replay,
                                          lambda row: charge(dict(row, stage="write")))
        gateways.append(review_gateway)
        reviewed = rev.review(f"{parts['title']}\n\n{parts['abstract']}\n\n{with_figures(parts['body'])}"
                              f"\n\n{studies}", review_gateway)
        # The internal reviewer runs the capability pack's panel and polish review on the
        # compiled draft; its rewrite-only tasks join the reviewers' tasks.
        panel = panel_review(run, paper_dir / "main.tex", args.replay, args.model, charge)
        record("write", "panel_review", panel)
        tasks = reviewed["tasks"] + panel["tasks"]
        adopted, revision_rating = False, None
        if tasks:
            revised = writer(REVIEW_REVISION_PROMPT.format(
                numbers=", ".join(sorted(registry, key=float)), protocol=protocol, admin=ADMIN_FACTS,
                stances="\n".join(f"- {p['key']}: {coded_stance(p)}" for p in sorted(papers.values(),
                                                                                    key=lambda p: p["key"])),
                tasks="\n".join(f"- {t}" for t in tasks), draft=draft), "review_revision")
            if not blocks(revised)["title"]:   # the title the gate passed stands when a revision drops it
                revised = f"<title>{parts['title']}</title>\n{revised}"
            r_parts, r_findings, r_missing = deliver(revised, "review_revision")
            if r_findings or r_missing:   # the gate's findings go back to the writer once
                revised = writer(REVISION_PROMPT.format(
                    findings="\n".join(f"- {f.render()}" for f in r_findings)
                    or "- a block is missing: " + ", ".join(r_missing), draft=revised), "review_revision_repair")
                if not blocks(revised)["title"]:
                    revised = f"<title>{parts['title']}</title>\n{revised}"
                r_parts, r_findings, r_missing = deliver(revised, "review_revision_repair")
            if not r_findings and not r_missing:
                # The same reviewers rate the revision; it replaces the draft only if it reads no worse.
                rereview = rev.review(f"{r_parts['title']}\n\n{r_parts['abstract']}\n\n"
                                      f"{with_figures(r_parts['body'])}\n\n{studies}", review_gateway, chair=False)
                revision_rating = rereview["mean_rating"]
                adopted = (revision_rating or 0) >= (reviewed["mean_rating"] or 0)
            if adopted:
                draft, parts = revised, r_parts
            else:
                compile_pdf(parts)   # the gate-passing draft stays the paper
        record("write", "review_report", dict(reviewed, adopted=adopted, revision_rating=revision_rating,
                                              method="independent reviews and an area-chair meta-review "
                                                     "(AgentReview, Jin et al. 2024)"))
    _dump(doi_cache_path, doi_cache)
    _dump(compile_cache_path, dict(sorted(compile_cache.items())))

    pdf = "paper/main.pdf" if ready else None
    record("write", "draft_pack", {"title": parts["title"], "attempts": attempts, "claim_support": parts["support"],
                                   "rigor_audit_findings": writer.rigor_findings, "source": "paper/main.tex"})
    if ready and pdf:
        record("write", "final_bundle", {"pdf": pdf, "source": "paper/main.tex", "bibliography": "paper/references.bib"})
    tokens = defaultdict(int)
    for row in usage:
        tokens[row["stage"]] += row["total_tokens"]
    record("write", "process_summary", {
        "delivery": "ready" if ready else "refused", "revisions": len(attempts) - 1,
        "calls": Counter(row["stage"] for row in usage), "tokens_by_stage": dict(tokens),
        "measured": all(row.get("measured", True) for row in usage),
    })
    final = log.advance()

    with (run / "back_usage.jsonl").open("w", encoding="utf-8") as fh:
        for row in usage:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    _dump(manifest_path, {
        "generated_at": generated_at, "model": args.model,
        "models": {k: os.environ[k] for k in GATEWAY_MODELS if os.environ.get(k)},
        "delivery": "ready" if ready else "refused",
        "revisions": len(attempts) - 1, "pdf": pdf, "write_stage": final.decision,
        "tokens": sum(r["total_tokens"] for r in usage),
        "writer_tokens_superseded": writer.superseded_tokens(),
        "gateway_tokens_superseded": sum(g.superseded_tokens() for g in gateways),
        "result": {k: result[k] for k in ("directional", "supporting", "share", "ci95", "p")},
    })
    print(f"propose: design fit p={fits}, publication bias p={bias}")
    print(f"experiment: {result['supporting']}/{result['directional']} papers support, share {result['share']} "
          f"95% CI {result['ci95']}, p {result['p']}")
    for a in attempts:
        c = a["certificate"]
        print(f"delivery attempt {a['attempt']}: {len(a['findings'])} finding(s); number check "
              f"{'certified at round ' + str(c['certified_at_round']) if c['certified'] else 'not certified'}")
    print(f"write: {final.decision}; pdf {pdf}; tokens {sum(r['total_tokens'] for r in usage):,}")
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
