#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Produce the stage artifacts the recorded research run left without a record.

The imported run (scripts/import_research_run.py) has no artifact for seven of
the sixteen stage requirements. This script produces them now, in this
repository, from the same paper pool and the same experiment records:

  build       OpenAlex metadata for every pool paper with a DOI or arXiv id.
              Papers grouped by their OpenAlex topic give the literature_map;
              works cited by at least two pool papers but absent from the pool
              give the citation_expansion_report.
  analyze     One Jev call per paper: is the paper relevant to the research
              question, which abstract sentences report findings, and how each
              finding bears on the question. Kept findings form the
              claim_candidate_set and, grouped by stance, the evidence_pack. A
              last call asks whether the question is still open given that
              evidence (direction_proposal).
  propose     Jev checks the study design, as the paper states it, against a
              fixed checklist (adversarial_resolution).
  experiment  Every number the paper prints, bound to the experiment record it
              came from, with that record file's SHA-256 (verified_registry).

Each artifact is listed in manifest.json with origin "post_hoc_completion" and
the time it was produced; it is a completion made after the run, not a record
of it. Every OpenAlex response and Jev request/response is cached in --out, and
`--replay` rebuilds all artifacts from those caches without network or key.

    python3 scripts/complete_research_run.py \\
        --sources ../run_records/source --experiment ../scirigorbench \\
        --manuscript <paper>/source/main.tex --design <paper>/source/validation.tex \\
        --out ../run_records/completion [--replay]
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "jiuwenswarm/agents/harness/common/tools"
RAILS = REPO / "jiuwenswarm/agents/harness/team/rails"
EVIDENCE_BIND = (REPO / "jiuwenswarm/resources/agent/workspace/skills"
                 / "scholar-paper-writer/scripts/evidence_bind.py")

RESEARCH_QUESTION = (
    "Can the false-accusation rate of an automated reviewer that audits AI-written "
    "research papers be certified, and what has to be fixed when the audit corpus "
    "is built for such a certificate to hold?"
)

STANCE = {
    "shows_need": "it shows that reviewers, audits or evaluations make errors that go "
                  "unmeasured or uncontrolled",
    "offers_method": "it offers a method, protocol or guarantee that could bound or certify "
                     "an error rate",
    "counter": "it suggests such error rates are already controlled, or that certifying "
               "them is unnecessary or infeasible",
    "unrelated": "it does not bear on the question",
}

DESIGN_CHECKLIST = {
    "baseline": "Does the study compare its method against at least one named baseline?",
    "falsification": "Does the study state which result would refute its main claim?",
    "fixed_metrics": "Are the evaluation metrics and thresholds fixed before the audit runs?",
    "isolation": "Is the auditor kept from seeing the ground truth it is evaluated against?",
    "randomization": "Is the choice of which claims are planted made by a randomization the "
                     "study controls?",
    "replication": "Does the study report how many independent replicates stand behind "
                   "each estimate?",
    "negative_results": "Does the study report where its method fails or does worse than "
                        "an alternative?",
    "cost": "Does the study report the compute or token cost of its experiments?",
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


def _dump(path: Path, obj) -> str:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path.name


# ------------------------------------------------------------ experiment stage

def registry_from_binding(binding: dict, data_dir: Path) -> dict:
    """Printed numbers with the record file, field path and file hash they bind to."""
    hashes: dict[str, str] = {}
    entries = []
    for b in binding["bound"]:
        # a bound source reads "<file>.json.<path inside it>"
        stem, _, record_path = b["source"].partition(".json")
        record_file, record_path = stem + ".json", record_path.lstrip(".")
        if record_file not in hashes:
            hashes[record_file] = hashlib.sha256((data_dir / record_file).read_bytes()).hexdigest()
        entries.append({
            "literal": b["literal"],
            "value": b["value"],
            "printed_at": f"{b['file']}:{b['line']}",
            "record_file": record_file,
            "record_path": record_path,
            "record_sha256": hashes[record_file],
        })
    return {
        "entries": entries,
        "unbound": [{"literal": u["literal"], "printed_at": f"{u['file']}:{u['line']}"}
                    for u in binding["unbound"]],
    }


def latex_to_text(tex: str) -> str:
    """Rough plain text of a LaTeX section, enough for a checklist decision."""
    tex = re.sub(r"(?<!\\)%.*", "", tex)
    tex = re.sub(r"\\(cite\w*|ref|eqref|label)\{[^}]*\}", "", tex)
    tex = re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?\{([^}]*)\}", r"\2", tex)
    tex = re.sub(r"\\[a-zA-Z]+\*?", " ", tex)
    return re.sub(r"\s+", " ", tex).strip()


# ------------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", type=Path, required=True)
    ap.add_argument("--experiment", type=Path, required=True)
    ap.add_argument("--manuscript", type=Path, required=True)
    ap.add_argument("--design", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--replay", action="store_true")
    args = ap.parse_args()

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    jev = _load("jev_decision", TOOLS / "jev_decision.py")
    rs = _load("research_stages", RAILS / "research_stages.py")
    client = jev.JevClient(cache_path=out / "jev_cache.jsonl", replay=args.replay)
    alex = rs.OpenAlexCache(out / "openalex_cache.json", args.replay)
    usage: list[dict] = []

    def decide(stage: str, step: str, state: str, questions: dict) -> dict:
        response = client.decide(state, questions)
        key = jev.request_key(client.model, state, questions)
        usage.append(jev.usage_row(response, stage=stage, step=step,
                                   source_record=f"completion/jev_cache.jsonl#{key[:16]}"))
        return response["answers"]

    # build ---------------------------------------------------------------
    pool = json.loads((args.sources / "rh_paper_pool.json").read_text())
    works = []
    unresolved = []
    for paper in pool:
        url = rs.openalex_url(paper)
        work = alex.get(url) if url else None
        if work:
            works.append(work)
        else:
            unresolved.append(paper["id"])
    expansion = rs.expansion_report(works, alex)
    alex.save()

    # analyze -------------------------------------------------------------
    relevance: dict[str, float] = {}
    claims = []
    for w in works:
        screened = rs.screen_paper(
            w, RESEARCH_QUESTION, STANCE, jev,
            lambda state, questions: decide("analyze", "relevance_screen+evidence_stance",
                                            state, questions))
        if screened is None:
            continue
        relevance[w["id"]] = screened["relevance"]
        claims += screened["claims"]
    kept = rs.kept_findings(claims)
    by_stance: dict[str, list] = defaultdict(list)
    for c in kept:
        by_stance[c["stance"]].append(c)

    summary_lines = [f"{s}: {len(by_stance.get(s, []))} findings" for s in STANCE if s != "unrelated"]
    examples = [f"- ({c['stance']}) {c['sentence']}"
                for s in ("offers_method", "counter", "shows_need") for c in by_stance.get(s, [])[:3]]
    direction_state = (f"Research question: {RESEARCH_QUESTION}\n\nEvidence from the literature pool:\n"
                       + "\n".join(summary_lines) + "\n\nExamples:\n" + "\n".join(examples))
    direction = decide("analyze", "open_question", direction_state, {
        "still_open": jev.noul("Given this evidence, is the research question still open, "
                               "that is, not already answered by the cited work?"),
        "room_for": jev.choice("Which kind of contribution does the evidence leave the most room for?", {
            "benchmark": "a benchmark or measurement that exposes the unmeasured errors",
            "method": "a new auditing method",
            "guarantee": "a certificate or guarantee on the error rate",
            "negative_result": "evidence that the error rate cannot be certified",
        }),
    })

    # propose -------------------------------------------------------------
    design_text = latex_to_text(args.design.read_text(errors="replace"))[:24000]
    checks = decide("propose", "design_checklist", design_text,
                    {k: jev.noul(q) for k, q in DESIGN_CHECKLIST.items()})

    def status(p: float) -> str:
        return "met" if p >= 0.7 else "open" if p < 0.3 else "uncertain"

    # experiment ----------------------------------------------------------
    eb = _load("evidence_bind", EVIDENCE_BIND)
    record_files = sorted((args.experiment / "data").glob("*.json"))
    record_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in record_files}
    previous = out / "manifest.json"
    if args.replay and previous.exists():
        # The registry is only reproducible from the same records. A record
        # changed since the completion was made is reported by name instead of
        # silently yielding a different registry.
        recorded = json.loads(previous.read_text()).get("records", {})
        changed = sorted(n for n in set(recorded) | set(record_hashes)
                         if recorded.get(n) != record_hashes.get(n))
        if recorded and changed:
            raise SystemExit(f"experiment records differ from the completion's: {', '.join(changed)}")
    records = {p.name: json.loads(p.read_text()) for p in record_files}
    registry = registry_from_binding(eb.bind_manuscript(args.manuscript, records),
                                     args.experiment / "data")

    # write ---------------------------------------------------------------
    produced = [
        ("build", "literature_map", _dump(out / "literature_map.json", {
            "clusters": rs.topic_clusters(works, relevance),
            "resolved": len(works), "unresolved_pool_ids": unresolved,
        }), "OpenAlex primary topic of each pool paper; relevance from Jev"),
        ("build", "citation_expansion_report", _dump(out / "citation_expansion_report.json", expansion),
         "works cited by at least two pool papers and absent from the pool (OpenAlex)"),
        ("analyze", "claim_candidate_set", _dump(out / "claim_candidate_set.json", {
            "research_question": RESEARCH_QUESTION, "candidates": claims, "kept": len(kept),
        }), "abstract sentences with result markers; kept when Jev finding probability >= 0.5 and stance is not unrelated"),
        ("analyze", "evidence_pack", _dump(out / "evidence_pack.json", {
            "research_question": RESEARCH_QUESTION,
            "by_stance": {s: by_stance.get(s, []) for s in STANCE if s != "unrelated"},
        }), "kept findings grouped by Jev stance"),
        ("analyze", "direction_proposal", _dump(out / "direction_proposal.json", {
            "research_question": RESEARCH_QUESTION,
            "evidence_counts": {s: len(by_stance.get(s, [])) for s in STANCE if s != "unrelated"},
            "still_open_probability": direction["still_open"]["noul"],
            "room_for": direction["room_for"]["choice"],
            "room_for_probabilities": direction["room_for"]["probabilities"],
        }), "Jev decision over the evidence pack"),
        ("propose", "adversarial_resolution", _dump(out / "adversarial_resolution.json", {
            "design_source": args.design.name,
            "checklist": {k: {"question": q, "probability": checks[k]["noul"],
                              "status": status(checks[k]["noul"])}
                          for k, q in DESIGN_CHECKLIST.items()},
        }), "Jev checklist over the study design as stated in the paper"),
        ("experiment", "verified_registry", _dump(out / "verified_registry.json", registry),
         "printed numbers bound to experiment records (evidence_bind), with record file SHA-256"),
    ]
    # Requests in the cache that this run did not use were still paid for; they
    # are counted here so the reported spend covers every call ever sent.
    used = {r["source_record"].rsplit("#", 1)[-1] for r in usage}
    superseded = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
    answered_at = {}
    for line in (out / "jev_cache.jsonl").read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        answered_at[rec["key"][:16]] = rec.get("ts")
        if rec["key"][:16] not in used:
            superseded["requests"] += 1
            superseded["input_tokens"] += rec["response"]["usage"]["input_tokens"]
            superseded["output_tokens"] += rec["response"]["usage"]["output_tokens"]

    generated_at = (json.loads(previous.read_text())["generated_at"]
                    if args.replay and previous.exists()
                    else datetime.now(timezone.utc).isoformat(timespec="seconds"))
    # Each row keeps the time its response was written to the cache.
    for row in usage:
        row["ts"] = answered_at.get(row["source_record"].rsplit("#", 1)[-1]) or generated_at
    with (out / "usage.jsonl").open("w", encoding="utf-8") as fh:
        for row in usage:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    _dump(out / "manifest.json", {
        "generated_at": generated_at,
        "origin": "post_hoc_completion",
        "research_question": RESEARCH_QUESTION,
        "records": record_hashes,
        "artifacts": [{"stage": s, "kind": k, "file": f, "method": m} for s, k, f, m in produced],
        "jev": {
            "calls": len(usage),
            "models": sorted({r["model"] for r in usage}),
            "input_tokens": sum(r["input_tokens"] for r in usage),
            "output_tokens": sum(r["output_tokens"] for r in usage),
            "cost_usd": round(sum(r["cost_usd"] for r in usage), 6),
            "superseded": superseded,
        },
    })

    print(f"OpenAlex: {len(works)}/{len(pool)} pool papers resolved, "
          f"{expansion['cocited_not_in_pool']} co-cited works outside the pool")
    print(f"Jev: {len(usage)} calls, {sum(r['total_tokens'] for r in usage):,} tokens, "
          f"${sum(r['cost_usd'] for r in usage):.4f}")
    print(f"claims: {len(claims)} candidates, {len(kept)} kept; "
          + ", ".join(f"{s} {len(by_stance.get(s, []))}" for s in STANCE if s != "unrelated"))
    print(f"direction: still open p={direction['still_open']['noul']}, room for {direction['room_for']['choice']}")
    print("design checklist: " + ", ".join(f"{k} {status(checks[k]['noul'])}" for k in DESIGN_CHECKLIST))
    print(f"registry: {len(registry['entries'])} bound numbers, {len(registry['unbound'])} unbound")
    return 0


if __name__ == "__main__":
    sys.exit(main())
