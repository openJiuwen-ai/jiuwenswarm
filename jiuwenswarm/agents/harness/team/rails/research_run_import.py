# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Map a recorded research run into JiuwenSwarm's workflow and usage records.

The submitted paper was produced on our research harness (RH). This module
reads RH's own exports of that run (artifact list, provenance records, paper
pool) together with the experiment's per-call token ledger, and writes them in
the record formats this repository uses:

* Workflow records. Each source record that satisfies a six-stage requirement
  is recorded through WorkflowRunLog, and the run is then advanced stage by
  stage. A requirement with no accepted source stays unmet, and the replay
  writes the refusal.
* Usage rows. One row per recorded model call, in the token keys the
  JiuwenSwarm CLI renders (input_tokens, output_tokens, total_tokens,
  cache_tokens). Every row carries `source_runtime` and `source_record`, so a
  reader can find the exact row it was copied from.

Every satisfied requirement also names its origin:

    rh_artifact          an artifact RH recorded during the run
    source_files         files from the experiment tree, hashed at import
    derived_at_import    computed at import from the exported source records
    post_hoc_completion  produced after the run by scripts/complete_research_run.py
                         for a requirement the run left without a record

The module is stdlib only and does not import the package, so the import script
can load it by path on the Python a reviewer already has.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path


SOURCE_RUNTIME = "research-harness"
EXPERIMENT_RUNTIME = "scirigorbench-experiment"

# RH artifact type -> the (stage, kind) it satisfies. Only types that are the
# same concept as the requirement are listed. RH's experiment_design is the
# recorded study design, which is what the propose stage's study_spec asks for.
ARTIFACT_EQUIVALENCE: dict[str, tuple[str, str]] = {
    "topic_brief": ("init", "topic_brief"),
    "experiment_design": ("propose", "study_spec"),
    "draft_pack": ("write", "draft_pack"),
    "final_bundle": ("write", "final_bundle"),
}

# RH primitive -> the stage it ran in. Used to place usage rows.
PRIMITIVE_STAGE: dict[str, str] = {
    "paper_search": "build",
    "deep_read": "build",
    "gap_ground_verify": "analyze",
    "research_state_update": "analyze",
    "exemplar_critique": "write",
}

# Experiment files whose content is the recorded result of the experiment stage.
EXPERIMENT_RESULT_FILES = (
    "data/audit_results.json",
    "data/audit_grid.json",
    "data/baseline_rubric.json",
    "data/baseline_detector.json",
)
# Ledger stage -> the result file whose `model` field names the model it called.
LEDGER_STAGE_RESULT = {
    "blind_audit_grid": "data/audit_grid.json",
    "baseline_rubric": "data/baseline_rubric.json",
    "baseline_detector": "data/baseline_detector.json",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_ts(value: str) -> datetime:
    """ISO timestamp; RH writes some without a zone, and those are UTC."""
    ts = datetime.fromisoformat(value.replace(" ", "T"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def recorded_by(value: str | None, until: datetime | None) -> bool:
    """True when a record exists at or before `until`.

    `until` is the time the submitted package was built. A topic keeps growing
    after a submission; records made later did not produce the submitted paper.
    A record with no timestamp is kept, because it cannot be placed after.
    """
    if until is None or not value:
        return True
    return parse_ts(value) <= until


def artifact_requirements(rh_artifacts: list[dict], until: datetime | None = None) -> list[dict]:
    """Active RH artifacts that satisfy a six-stage requirement.

    Superseded versions are skipped: the active version is the one the run
    ended with. Artifacts created after `until` are skipped.
    """
    out: list[dict] = []
    for art in rh_artifacts:
        if art.get("status") != "active" or not recorded_by(art.get("created_at"), until):
            continue
        target = ARTIFACT_EQUIVALENCE.get(art.get("type", ""))
        if target is None:
            continue
        stage, kind = target
        out.append({
            "stage": stage,
            "kind": kind,
            "ref": f"rh:artifact/{art['id']}",
            "origin": SOURCE_RUNTIME,
            "source_type": art["type"],
            "source_created_at": art.get("created_at"),
        })
    return out


def paper_pool_snapshot(pool: list[dict]) -> dict:
    """The topic's paper pool as exported from RH, one entry per paper."""
    return {
        "paper_count": len(pool),
        "papers": [
            {k: p.get(k) for k in ("id", "title", "doi", "arxiv_id", "year")}
            for p in pool
        ],
    }


def acquisition_report(pool: list[dict]) -> dict:
    """Which pool papers have an acquired PDF, judged by a recorded content hash."""
    missing = [p["id"] for p in pool if not p.get("pdf_hash")]
    return {
        "paper_count": len(pool),
        "pdf_acquired": len(pool) - len(missing),
        "pdf_missing_ids": missing,
    }


def file_manifest(root: Path, relpaths: list[str]) -> dict:
    """Relative path and SHA-256 of each file, so the ref pins exact content."""
    return {
        "files": [
            {"path": rel, "sha256": _sha256(root / rel)}
            for rel in relpaths
            if (root / rel).is_file()
        ]
    }


def experiment_code_paths(experiment_root: Path) -> list[str]:
    paths = ["run_pipeline.py"] if (experiment_root / "run_pipeline.py").is_file() else []
    paths += sorted(
        str(p.relative_to(experiment_root)) for p in (experiment_root / "data").glob("*.py")
    )
    return paths


def usage_rows(provenance: list[dict], ledger: list[dict],
               until: datetime | None = None,
               stage_models: dict[str, str] | None = None) -> list[dict]:
    """One row per recorded call, in JiuwenSwarm's token keys, with its source.

    Provenance records started after `until` are left out. Ledger rows carry no
    timestamp; the ledger was written by the experiment run before submission.
    `stage_models` maps a ledger stage to the model its result file names.
    """
    rows: list[dict] = []
    for rec in provenance:
        if not recorded_by(rec.get("started_at"), until):
            continue
        prompt = int(rec.get("prompt_tokens") or 0)
        completion = int(rec.get("completion_tokens") or 0)
        rows.append({
            "ts": rec.get("started_at"),
            "stage": PRIMITIVE_STAGE.get(rec.get("primitive", ""), "analyze"),
            "step": rec.get("primitive"),
            "model": rec.get("model_used") or "",
            "input_tokens": prompt,
            "output_tokens": completion,
            "total_tokens": prompt + completion,
            "cache_tokens": 0,
            "cost_usd": float(rec.get("cost_usd") or 0.0),
            "success": bool(rec.get("success")),
            "measured": True,
            "source_runtime": SOURCE_RUNTIME,
            "source_record": f"rh:provenance/{rec['id']}",
        })
    for line_no, rec in enumerate(ledger, start=1):
        prompt = int(rec.get("prompt_tokens") or 0)
        completion = int(rec.get("completion_tokens") or 0)
        rows.append({
            "ts": None,
            "stage": "experiment",
            "step": rec.get("stage"),
            "model": (stage_models or {}).get(rec.get("stage"), ""),
            "input_tokens": prompt,
            "output_tokens": completion,
            "total_tokens": int(rec.get("total_tokens") or prompt + completion),
            "cache_tokens": 0,
            "cost_usd": None,
            "success": True,
            "measured": bool(rec.get("measured")),
            "source_runtime": EXPERIMENT_RUNTIME,
            "source_record": f"ideal_run_log.jsonl:{line_no}",
        })
    return rows


def process_summary(rows: list[dict], requirements: list[dict]) -> dict:
    """Totals per stage and the source of every satisfied requirement."""
    per_stage: dict[str, dict] = {}
    for row in rows:
        s = per_stage.setdefault(row["stage"], {"calls": 0, "total_tokens": 0, "measured_tokens": 0})
        s["calls"] += 1
        s["total_tokens"] += row["total_tokens"]
        if row["measured"]:
            s["measured_tokens"] += row["total_tokens"]
    return {
        "per_stage": per_stage,
        "requirements": [
            {k: r[k] for k in ("stage", "kind", "ref", "origin")} for r in requirements
        ],
    }


def coverage(requirements: list[dict], stage_registry: dict) -> list[dict]:
    """For each stage: which required kinds have a source, and which do not."""
    by_kind = {(r["stage"], r["kind"]): r for r in requirements}
    table: list[dict] = []
    for stage, spec in stage_registry.items():
        required = list(spec["required_artifacts"])
        satisfied = {
            kind: {"ref": by_kind[(stage, kind)]["ref"], "origin": by_kind[(stage, kind)]["origin"]}
            for kind in required
            if (stage, kind) in by_kind
        }
        table.append({
            "stage": stage,
            "required": required,
            "satisfied": satisfied,
            "missing": [k for k in required if k not in satisfied],
        })
    return table


def replay(requirements: list[dict], log) -> list:
    """Record every source requirement, then advance until a stage refuses.

    `log` is a WorkflowRunLog. The returned list holds each StageAdvance in
    order; the last one is the refusal, or the advance out of the final stage.
    A requirement with a source creation time is recorded at that time, in
    time order; the rest, and every stage decision, at the import's own clock.
    Stage decisions are computed here, so they carry origin derived_at_import.
    """
    def created(req: dict) -> str | None:
        return parse_ts(req["source_created_at"]).isoformat() if req.get("source_created_at") else None

    timed = sorted((r for r in requirements if created(r)), key=created)
    for req in timed + [r for r in requirements if not created(r)]:
        log.record_artifact(req["kind"], stage=req["stage"], ref=req["ref"],
                            ts=created(req), origin=req.get("origin"))
    decisions = []
    while True:
        decision = log.advance(origin="derived_at_import")
        decisions.append(decision)
        if decision.decision == "refused" or decision.next_stage is None:
            return decisions
