#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run a new research question through the init, build and analyze stages.

    python3 scripts/run_research_front.py \\
        --question "Do LLM judges prefer answers written by their own model family?" \\
        --query "LLM-as-a-judge self-preference bias" --query "LLM evaluator favors own outputs" \\
        --out ../run_records/front/llm-judge-self-preference [--replay]

What runs, and what decides each step:

  init      the question and queries become the topic_brief.
  build     paper_search (arXiv, OpenAlex, Crossref, PubMed, Semantic Scholar)
            gives the pool; OpenAlex metadata gives topic clusters
            (literature_map), co-citation expansion and open-access status.
            Open-access full text is read through the arXiv, PubMed Central,
            OpenAlex and Unpaywall routes (fulltext), and the acquisition_report
            counts what was retrieved by which route.
  analyze   one Jev call per paper decides relevance, which candidate sentences
            -- from the abstract and from the results sections of the full
            text -- report findings and how each bears on the question
            (claim_candidate_set, evidence_pack); one more decides whether the
            question is still open and what kind of contribution the evidence
            leaves room for (direction_proposal).
  propose   needs a written study design, which is a writing step and goes to
            the chat-model roles of the research-paper-team skill. The run
            stops here, and the log records exactly which artifacts that
            hand-off has to supply.

Every stage decision goes through WorkflowRunLog with a token budget; every
Jev call is charged to it. Search results, OpenAlex answers, full-text answers
(section titles and results-section candidates only) and Jev answers are cached
in --out, and --replay rebuilds every output byte for byte without the network
or a key.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "jiuwenswarm/agents/harness/common/tools"
RESEARCH = REPO / "jiuwenswarm/agents/harness/common/research"
RAILS = REPO / "jiuwenswarm/agents/harness/team/rails"

# How a finding can bear on any research question. Kept generic on purpose:
# the same four options serve every topic, so runs on different topics are
# comparable.
STANCE = {
    "supports": "it reports evidence for the effect or answer the question asks about",
    "challenges": "it reports evidence against it, or that the effect is absent or reversed",
    "method": "it offers a method, dataset or measurement the question could be studied with",
    "unrelated": "it does not bear on the question",
}

ROOM_FOR = {
    "measurement": "a measurement or benchmark that settles the question empirically",
    "method": "a new method that addresses the problem the question points at",
    "theory": "a model or guarantee that explains the evidence",
    "replication": "a replication that resolves conflicting findings",
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _dump(path: Path, obj) -> str:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path.name


def fulltext_results(works: list, cache_path: Path, ft, rs, replay: bool) -> dict:
    """openalex id -> the full-text route and up to three results-section sentences.

    Only section titles and the candidate sentences are kept, which is all the
    analyze stage reads; the full text itself stays out of the run records.
    """
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    missing = [w for w in works if w["id"] not in cache]
    if missing and replay:
        raise SystemExit(f"--replay: no full-text answer cached for {len(missing)} papers")

    def one(work: dict) -> dict:
        got = ft.fetch(work)
        if not got["route"]:
            return {"route": None, "attempts": got["attempts"], "results_sentences": []}
        results = " ".join(ft.results_text(got["sections"]).split())
        return {"route": got["route"], "url": got["url"], "sections": [s["title"] for s in got["sections"]],
                "results_sentences": rs.candidate_sentences(results) if results else []}

    with ThreadPoolExecutor(8) as pool:
        for work, answer in zip(missing, pool.map(one, missing)):
            cache[work["id"]] = answer
    if missing:
        _dump(cache_path, dict(sorted(cache.items())))
    return {w["id"]: cache[w["id"]] for w in works}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", help="required for a new run; a replay reads topic_brief.json")
    ap.add_argument("--query", action="append", help="repeatable; as --question")
    ap.add_argument("--discipline", default="", help="the field, e.g. 'clinical psychology'; it picks the "
                    "writing profile and the figure style")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--max-papers", type=int, default=40)
    ap.add_argument("--token-budget", type=int, default=300_000)
    ap.add_argument("--replay", action="store_true")
    args = ap.parse_args()

    out = args.out
    brief_path = out / "topic_brief.json"
    if not args.question and brief_path.exists():
        brief = json.loads(brief_path.read_text())
        args.question, args.query = brief["question"], brief["queries"]
        args.discipline = brief.get("discipline", "")
    if not args.question or not args.query:
        ap.error("--question and at least one --query are needed for a new run")
    out.mkdir(parents=True, exist_ok=True)
    wf = _load("workflow_run_log", RAILS / "workflow_run_log.py")
    rs = _load("research_stages", RAILS / "research_stages.py")
    jev = _load("jev_decision", TOOLS / "jev_decision.py")
    ft = _load("fulltext", RESEARCH / "fulltext.py")
    fields = rs.OPENALEX_FIELDS + ",open_access,ids,best_oa_location"

    # A replay reuses the original run's time and topic id, so every output --
    # the stage log included -- comes out byte for byte the same wherever the
    # outputs are copied to.
    manifest_path = out / "manifest.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    generated_at = previous.get("generated_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    topic_id = previous.get("topic_id") or out.name
    (out / "workflow_run.jsonl").unlink(missing_ok=True)
    log = wf.WorkflowRunLog(out / "workflow_run.jsonl", topic_id=topic_id,
                            token_budget=args.token_budget, clock=lambda: generated_at)
    client = jev.JevClient(cache_path=out / "jev_cache.jsonl", replay=args.replay)
    alex = rs.OpenAlexCache(out / "openalex_cache.json", args.replay)
    usage: list[dict] = []

    def decide(step: str, state, questions: dict) -> dict:
        response = client.decide(state, questions)
        key = jev.request_key(client.model, state, questions)
        row = jev.usage_row(response, stage="analyze", step=step,
                            source_record=f"jev_cache.jsonl#{key[:16]}")
        row["ts"] = generated_at
        usage.append(row)
        log.record_usage(stage="analyze", total_tokens=row["total_tokens"],
                         source_record=row["source_record"])
        return response["answers"]

    def record(stage: str, kind: str, obj) -> None:
        log.record_artifact(kind, stage=stage, ref=_dump(out / f"{kind}.json", obj))

    # init ----------------------------------------------------------------
    record("init", "topic_brief", {"question": args.question, "queries": args.query,
                                   **({"discipline": args.discipline} if args.discipline else {})})
    log.advance()

    # build ---------------------------------------------------------------
    search_path = out / "paper_search.json"
    if search_path.exists():   # like every network answer here: cached first; delete to search again
        search = json.loads(search_path.read_text())
    elif args.replay:
        raise SystemExit(f"--replay needs {search_path}")
    else:
        tools = _load("paper_search_tools", TOOLS / "paper_search_tools.py")
        search = tools.search_papers(args.query[0], extra_queries=args.query[1:],
                                     max_results=args.max_papers)
        _dump(search_path, search)
    pool = [p for p in search["papers"] if p.get("doi") or p.get("arxiv_id")]
    record("build", "paper_pool_snapshot", {
        "read_stands": search["read_stands"],
        "providers_answered": search["providers_answered"],
        "provider_errors": search["provider_errors"],
        "papers": [{k: p.get(k) for k in ("title", "year", "venue", "doi", "arxiv_id")} for p in pool],
    })
    works = []
    for paper in pool:
        url = rs.openalex_url(paper, fields)
        work = alex.get(url) if url else None
        if work:
            works.append(work)
    record("build", "literature_map", {"clusters": rs.topic_clusters(works, {}),
                                       "resolved": len(works), "pool": len(pool)})
    record("build", "citation_expansion_report", rs.expansion_report(works, alex))
    open_access = [w for w in works if (w.get("open_access") or {}).get("is_oa")]
    texts = fulltext_results(works, out / "fulltext_cache.json", ft, rs, args.replay)
    got = [t for t in texts.values() if t["route"]]
    record("build", "acquisition_report", {
        "pool": len(pool), "resolved": len(works), "open_access": len(open_access),
        "open_access_urls": [(w.get("open_access") or {}).get("oa_url") for w in open_access],
        "fulltext": {"attempted": len(texts), "retrieved": len(got),
                     "with_results_section": sum(bool(t["results_sentences"]) for t in got),
                     "by_route": dict(sorted(Counter(t["route"] for t in got).items())),
                     "reasons": "fulltext_cache.json records each route tried for a paper not retrieved"},
    })
    alex.save()
    log.advance()

    # analyze -------------------------------------------------------------
    claims = []
    for w in works:
        screened = rs.screen_paper(w, args.question, STANCE, jev,
                                   lambda s, q: decide("relevance_screen+evidence_stance", s, q),
                                   results=texts[w["id"]]["results_sentences"])
        if screened:
            claims += screened["claims"]
    kept = rs.kept_findings(claims)
    by_stance: dict[str, list] = defaultdict(list)
    for c in kept:
        by_stance[c["stance"]].append(c)
    counts = {s: len(by_stance.get(s, [])) for s in STANCE if s != "unrelated"}
    examples = [f"- ({s}) {c['sentence']}" for s in ("supports", "challenges", "method")
                for c in by_stance.get(s, [])[:3]]
    direction = decide("open_question",
                       f"Research question: {args.question}\n\nEvidence from the literature pool:\n"
                       + "\n".join(f"{s}: {n} findings" for s, n in counts.items())
                       + "\n\nExamples:\n" + "\n".join(examples), {
                           "still_open": jev.noul("Given this evidence, is the research question still "
                                                  "open, that is, not already answered by the cited work?"),
                           "room_for": jev.choice("Which kind of contribution does the evidence leave "
                                                  "the most room for?", ROOM_FOR),
                       })
    record("analyze", "claim_candidate_set", {"candidates": claims, "kept": len(kept)})
    record("analyze", "evidence_pack", {"by_stance": {s: by_stance.get(s, []) for s in counts}})
    record("analyze", "direction_proposal", {
        "question": args.question, "evidence_counts": counts,
        "still_open_probability": direction["still_open"]["noul"],
        "room_for": direction["room_for"]["choice"],
        "room_for_probabilities": direction["room_for"]["probabilities"],
    })
    log.advance()

    # propose: the first writing step; the run hands off here --------------
    stop = log.advance()
    with (out / "usage.jsonl").open("w", encoding="utf-8") as fh:
        for row in usage:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    _dump(manifest_path, {
        "generated_at": generated_at,
        "topic_id": topic_id,
        "question": args.question,
        "queries": args.query,
        "pool": len(pool), "resolved": len(works), "read_stands": search["read_stands"],
        "claims": len(claims), "kept": len(kept), "evidence_counts": counts,
        "kept_from_fulltext": sum(c["source"] == "full text" for c in kept),
        "fulltext_retrieved": len(got),
        "stopped_at": stop.stage, "missing": list(stop.missing_artifacts),
        "jev": {"calls": len(usage), "tokens": sum(r["total_tokens"] for r in usage),
                "cost_usd": round(sum(r["cost_usd"] for r in usage), 6)},
        "token_budget": args.token_budget,
    })

    print(f"pool {len(pool)} papers (read_stands={search['read_stands']}), "
          f"{len(works)} resolved in OpenAlex, {len(open_access)} open access")
    print(f"full text {len(got)}/{len(texts)} papers; "
          f"{sum(c['source'] == 'full text' for c in kept)} of {len(kept)} kept findings come from full text")
    print(f"claims {len(claims)}, kept {len(kept)}: "
          + ", ".join(f"{s} {n}" for s, n in counts.items()))
    print(f"direction: still open p={direction['still_open']['noul']}, "
          f"room for {direction['room_for']['choice']}")
    print(f"Jev {len(usage)} calls, {sum(r['total_tokens'] for r in usage):,} tokens, "
          f"${sum(r['cost_usd'] for r in usage):.4f}; budget {log.tokens_used:,}/{args.token_budget:,}")
    print(f"stages: init, build, analyze advanced; {stop.stage} waits for "
          + ", ".join(stop.missing_artifacts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
