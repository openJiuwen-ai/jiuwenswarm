#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import the recorded research run into JiuwenSwarm's workflow and usage records.

    python3 scripts/import_research_run.py \
        --sources ../run_records/source \
        --experiment ../scirigorbench \
        --until 2026-08-20T19:16:43+08:00 \
        --out ../run_records/jiuwenswarm_run

Inputs are the three RH exports written by run_records/export_sources.sh and the
experiment tree with its per-call ledger (data/ideal_run_log.jsonl). `--until`
is the time the submitted package was built; RH records made after it belong
to later work on the topic and are left out. Outputs:

    workflow_run.jsonl       WorkflowRunLog: artifacts recorded, then stage advances
    usage.jsonl              one row per recorded call, JiuwenSwarm token keys
    coverage.json            per stage: required kinds, their sources, what is missing
    paper_pool_snapshot.json / acquisition_report.json / process_summary.json
    experiment_code.json / experiment_result.json   file manifests with SHA-256

No model is called and no package install is needed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, obj) -> str:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path.name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", type=Path, required=True)
    ap.add_argument("--experiment", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--until", default=None,
                    help="ISO time the submitted package was built")
    ap.add_argument("--manuscript", type=Path, default=None,
                    help="top .tex of the submitted paper; its numbers are bound to the records")
    ap.add_argument("--completion", type=Path, default=None,
                    help="output of complete_research_run.py; its artifacts are recorded as post-hoc")
    args = ap.parse_args()

    rails = Path(__file__).resolve().parents[1] / "jiuwenswarm/agents/harness/team/rails"
    wf = _load("workflow_run_log", rails / "workflow_run_log.py")
    imp = _load("research_run_import", rails / "research_run_import.py")

    artifacts = json.loads((args.sources / "rh_artifacts.json").read_text())
    provenance = json.loads((args.sources / "rh_provenance.json").read_text())
    pool = json.loads((args.sources / "rh_paper_pool.json").read_text())
    ledger_path = args.experiment / "data/ideal_run_log.jsonl"
    ledger = [json.loads(line) for line in ledger_path.read_text().splitlines() if line]

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*"):
        stale.unlink()

    until = imp.parse_ts(args.until) if args.until else None
    requirements = imp.artifact_requirements(artifacts, until)

    def derived(stage: str, kind: str, obj, origin: str = "derived_at_import") -> None:
        name = _write_json(out / f"{kind}.json", obj)
        requirements.append({
            "stage": stage, "kind": kind,
            "ref": f"jiuwenswarm_run/{name}", "origin": origin,
        })

    derived("build", "paper_pool_snapshot", imp.paper_pool_snapshot(pool))
    derived("build", "acquisition_report", imp.acquisition_report(pool))
    derived("experiment", "experiment_code",
            imp.file_manifest(args.experiment, imp.experiment_code_paths(args.experiment)),
            origin="source_files")
    derived("experiment", "experiment_result",
            imp.file_manifest(args.experiment, list(imp.EXPERIMENT_RESULT_FILES)),
            origin="source_files")

    stage_models = {
        stage: json.loads((args.experiment / rel).read_text()).get("model") or ""
        for stage, rel in imp.LEDGER_STAGE_RESULT.items()
        if (args.experiment / rel).is_file()
    }
    rows = imp.usage_rows(provenance, ledger, until, stage_models)

    # Stage artifacts produced after the run by complete_research_run.py, and the
    # Jev calls that produced them. They carry their own origin, never the run's.
    if args.completion:
        manifest = json.loads((args.completion / "manifest.json").read_text())
        for art in manifest["artifacts"]:
            requirements.append({
                "stage": art["stage"], "kind": art["kind"],
                "ref": f"completion/{art['file']}", "origin": manifest["origin"],
                "source_created_at": manifest["generated_at"],
            })
        rows += [json.loads(line) for line in
                 (args.completion / "usage.jsonl").read_text().splitlines() if line]

    with (out / "usage.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    derived("write", "process_summary", imp.process_summary(rows, requirements))

    table = imp.coverage(requirements, wf.STAGE_REGISTRY)
    _write_json(out / "coverage.json", table)

    # Printed numbers of the submitted paper against the shipped experiment
    # records: a check on the finished paper, written whether or not a stage
    # completion supplied the experiment-stage registry.
    binding = None
    if args.manuscript:
        eb_path = (Path(__file__).resolve().parents[1] / "jiuwenswarm/resources/agent/workspace"
                   / "skills/scholar-paper-writer/scripts/evidence_bind.py")
        eb = _load("evidence_bind", eb_path)
        records = {p.name: json.loads(p.read_text())
                   for p in sorted((args.experiment / "data").glob("*.json"))}
        binding = eb.bind_manuscript(args.manuscript, records)
        _write_json(out / "number_binding.json", binding)

    log = wf.WorkflowRunLog(out / "workflow_run.jsonl", topic_id="rh-topic-91")
    decisions = imp.replay(requirements, log)

    met = sum(len(s["satisfied"]) for s in table)
    need = sum(len(s["required"]) for s in table)
    print(f"requirements with a source: {met}/{need}")
    for s in table:
        print(f"  {s['stage']:<11} {len(s['satisfied'])}/{len(s['required'])}"
              + (f"  missing: {', '.join(s['missing'])}" if s["missing"] else ""))
    last = decisions[-1]
    print(f"replay: {' -> '.join(d.stage for d in decisions)} "
          f"({last.decision}{': ' + ', '.join(last.missing_artifacts) if last.missing_artifacts else ''})")
    total = sum(r["total_tokens"] for r in rows)
    measured = sum(r["total_tokens"] for r in rows if r["measured"])
    if binding:
        print(f"paper numbers bound to experiment records: "
              f"{len(binding['bound'])}/{binding['total']} across {len(binding['files'])} files")
    kept = sum(1 for r in rows if r["source_runtime"] == imp.SOURCE_RUNTIME)
    print(f"usage rows: {len(rows)}, tokens {total:,} (measured {measured:,}); "
          f"RH provenance kept {kept}/{len(provenance)}"
          + (f" up to {args.until}" if args.until else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
