# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exemplar-anchored writing: plan a paper's architecture by analogy to published papers of its kind.

The method follows the research harness EAG engine, form transferred, content
never:

  search    OpenAlex finds highly cited open-access reviews (systematic
            reviews, meta-analyses) whose title or abstract matches the topic,
            widening a query that finds none; a paper of the same form from a
            neighbouring topic beats one on the topic in another form.
  curate    for each exemplar whose full text is open, a chat model extracts
            its section architecture (section, role, share, key moves) and the
            order of its abstract's elements, as abstract form with no wording.
            An n-gram check measures how much of the extracted form repeats the
            exemplar's own text; a form that copies is dropped.
  plan      the chat model plans THIS paper's sections and abstract by analogy
            to the curated forms, naming for every section the exemplars it
            follows.

`domain_profile(discipline)` picks the field's writing profile (the
domain-writing-* skills: rhetorical contract, evidence requirements, drafting
bias) for the writer.

The run keeps the exemplars' metadata, section titles and curated forms, not
their text; curation answers are cached by exemplar and unit, so a replay
rebuilds the plan without the network.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

ROLES = ("introduction", "related_work", "method", "results", "discussion", "limitations", "conclusion", "other")

CURATE_SYSTEM = """You are curating a reference exemplar. From the paper below, extract its SECTION ARCHITECTURE
and the element order of its ABSTRACT, as abstracted FORM only, never verbatim text.
Return ONLY JSON: {"sections": [{"name": "generic section name", "role": "one of %s",
"approx_share": 0.0, "key_moves": ["the rhetorical job of a part of the section, in your own words"]}],
"abstract_elements": ["problem|objective|method|data|result|implication|limitation|..."],
"quality": 0}
quality 0-10: high only if a clean, complete, transferable skeleton is recoverable from the text.
Key moves describe the job a passage does (e.g. "state the inclusion criteria before the search"),
never the paper's topic, findings, numbers or wording.""" % "|".join(ROLES)

PLAN_SYSTEM = """Plan the SECTION ARCHITECTURE and ABSTRACT of THIS paper by analogy to the exemplar forms.
Use ONLY their structural FORM (section roles, order, key moves) and never their wording; re-instantiate
every move with THIS paper's own content. Keep a section whose role is method and one whose role is
results. Name sections the way the exemplars' field names them.
Return ONLY JSON: {"sections": [{"name": "section heading", "role": "one of %s",
"key_moves": ["what THIS paper's section must do, in order"], "follows": [exemplar numbers]}],
"abstract_elements": ["the abstract's elements in order"], "notes": "one sentence"}""" % "|".join(ROLES)

# Field -> writing profile. The first match wins, so clinical terms outrank the
# social-science ones a health-policy question also carries.
_DOMAINS = (
    ("biomedical", ("medic", "clinic", "health", "patient", "disease", "nursing", "pharma", "biolog",
                    "epidemiol", "oncolog", "genom", "neuroscience")),
    ("law-policy", ("law", "legal", "regulat", "policy", "governance")),
    ("humanities", ("history", "philosoph", "literature", "linguistic", "art ", "religio", "humanities")),
    ("social-science", ("psycholog", "sociolog", "economic", "education", "political", "social",
                        "behavio", "management", "communication")),
)


def resolve_domain(discipline: str) -> str:
    text = f" {discipline.lower()} "
    return next((name for name, stems in _DOMAINS if any(s in text for s in stems)), "stem")


def domain_profile(discipline: str, skills_dir: Path) -> dict:
    """The field's writing profile from its domain-writing skill: the sections the writer must follow."""
    name = resolve_domain(discipline)
    text = (skills_dir / f"domain-writing-{name}" / "SKILL.md").read_text(encoding="utf-8")
    parts = dict(re.findall(r"^## (.+?)\n(.*?)(?=^## |\Z)", text, re.S | re.M))
    keep = [f"{h}:\n{parts[h].strip()}" for h in ("Rhetorical Contract", "Evidence Requirements", "Drafting Bias")
            if h in parts]
    return {"domain": name, "skill": f"domain-writing-{name}", "guidance": "\n\n".join(keep)}


def _ngrams(text: str, n: int) -> set:
    t = re.findall(r"\w+", (text or "").lower())
    return {tuple(t[i:i + n]) for i in range(len(t) - n + 1)}


def max_ngram_overlap(draft: str, sources: list[str], n: int = 8) -> float:
    """Share of the draft's n-grams found in the sources (the EAG verbatim guard)."""
    d = _ngrams(draft, n)
    if not d:
        return 0.0
    seen = set().union(*(_ngrams(s, n) for s in sources)) if sources else set()
    return round(len(d & seen) / len(d), 4)


OPENALEX_SELECT = ("id,doi,title,publication_year,cited_by_count,type,ids,open_access,best_oa_location,"
                   "primary_location")


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "jiuwenswarm-research/1.0"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.load(resp)


def search(queries: list[str], get_json: Callable[[str], dict] = _get_json, per_query: int = 15,
           min_hits: int = 3) -> list[dict]:
    """Open-access reviews matching each query in turn (most relevant first within a query), deduplicated.

    OpenAlex types systematic reviews and meta-analyses as "review". A query that
    finds fewer than `min_hits` widens by dropping its last word, down to two words:
    a review of the neighbouring topic has the form this paper needs, while one odd
    match of a long query usually does not.
    """
    found: dict[str, dict] = {}
    for q in queries:
        words = re.sub(r"[,:|]", " ", q).split()
        hits: list = []
        for n in range(len(words), min(2, len(words)) - 1, -1):   # a one-word query is asked as is
            url = ("https://api.openalex.org/works?per-page=%d&sort=relevance_score:desc&select=%s&filter=%s"
                   % (per_query, OPENALEX_SELECT, urllib.parse.quote(
                       f"title_and_abstract.search:{' '.join(words[:n])},type:review,is_oa:true,cited_by_count:>20")))
            hits = get_json(url).get("results", [])
            if len(hits) >= min_hits:
                break
        for w in hits:
            found.setdefault(w["id"], w)
    # The topic's own queries come first; a broad field query only fills the rest.
    return list(found.values())


def excerpt(sections: list[dict], per_section: int = 600, total: int = 9000) -> str:
    """Section titles with the opening of each section: enough to recover roles and moves."""
    out = "\n\n".join(f"## {s['title'] or '(untitled)'}\n{s['text'][:per_section]}" for s in sections)
    return out[:total]


def _venue(work: dict) -> str:
    return ((work.get("primary_location") or {}).get("source") or {}).get("display_name") or ""


def _curate(work: dict, gateway, text: str = "") -> dict:
    """The exemplar's curated form. Cached by exemplar, so a replay asks with no text."""
    return _json(gateway.chat("exemplar_curate", CURATE_SYSTEM,
                              f"Paper: {work.get('title')} ({_venue(work)}).\nText:\n{text}",
                              cache_as={"exemplar": work["id"], "unit": "skeleton+abstract"}))


def anchor(queries: list[str], target: str, gateway, sources_path: Path, replay: bool,
           fetch: Callable[[dict], dict], want: int = 3, candidates: int = 15,
           min_quality: int = 6, max_copied: float = 0.05,
           get_json: Callable[[str], dict] = _get_json) -> dict:
    """Search, curate and plan. The record names every exemplar, its form, and what the plan follows.

    Exemplars come from the first `candidates` search hits whose full text is
    open; the first `want` whose form is clean (quality at least `min_quality`,
    at most `max_copied` of it repeating the source) anchor the plan.
    """
    if sources_path.exists():
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
    elif replay:
        raise SystemExit(f"--replay needs {sources_path}")
    else:
        sources = []
        for work in search(queries, get_json)[:candidates]:
            got = fetch(work)
            if not got["route"]:
                continue
            text = excerpt(got["sections"])
            form = _curate(work, gateway, text)
            sources.append({"id": work["id"], "doi": work.get("doi"), "title": work.get("title"),
                            "year": work.get("publication_year"), "venue": _venue(work),
                            "cited_by_count": work.get("cited_by_count"), "route": got["route"],
                            "section_titles": [x["title"] for x in got["sections"]],
                            "quality": form.get("quality"),
                            "copied_share": max_ngram_overlap(json.dumps(form, ensure_ascii=False), [text])})
            if sum(_clean(x, min_quality, max_copied) for x in sources) == want:
                break
        sources_path.write_text(json.dumps(sources, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    curated = [_curate(x, gateway) for x in sources]   # every curation is charged, kept or not
    kept = [i for i, x in enumerate(sources) if _clean(x, min_quality, max_copied)][:want]
    used = [sources[i] for i in kept]
    forms = [dict(curated[i], exemplar=n) for n, i in enumerate(kept, 1)]
    listing = "\n".join(f"Exemplar {f['exemplar']}: "
                        + json.dumps({k: f.get(k) for k in ("sections", "abstract_elements")}, ensure_ascii=False)
                        for f in forms)
    planned = _json(gateway.chat("exemplar_plan", PLAN_SYSTEM,
                                 f"THIS paper:\n{target}\n\nExemplar forms:\n{listing}")) if forms else {}
    return {"queries": queries, "considered": len(sources), "exemplars": used, "forms": forms, "plan": planned,
            "rule": f"exemplars: open full text, curated quality >= {min_quality}, "
                    f"copied share <= {max_copied}; first {want} by relevance"}


def _clean(source: dict, min_quality: int, max_copied: float) -> bool:
    try:
        return float(source.get("quality") or 0) >= min_quality and source["copied_share"] <= max_copied
    except (TypeError, ValueError):
        return False


def _json(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        obj = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        obj = {}
    return obj if isinstance(obj, dict) else {}
