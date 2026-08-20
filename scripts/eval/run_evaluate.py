#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Rewrite pred.jsonl and run official ContextBench evaluate.

    UV_NO_SYNC=1 uv run --with pyarrow python scripts/eval/run_evaluate.py \
        --pred docs/ai/experiments-contextbench/runs/run01-contextbench-verified/cfg_b__graph/raw
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
JIUWEN_ROOT = SCRIPT_DIR.parents[1]
GRAPH_ROOT = JIUWEN_ROOT.parent
CONTEXTBENCH_ROOT = GRAPH_ROOT / "reconstruct_tmp" / "ContextBench"
DEFAULT_GOLD = CONTEXTBENCH_ROOT / "data" / "contextbench_verified.parquet"

for path in (SCRIPT_DIR, CONTEXTBENCH_ROOT):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from trajectory import contextbench_record  # noqa: E402


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def load_raw_records(pred: Path) -> tuple[list[dict[str, Any]], Path]:
    pred = pred.expanduser().resolve()
    if pred.is_dir():
        records: list[dict[str, Any]] = []
        raw_dir = pred
        for traj in sorted(pred.glob("*.traj.json")):
            try:
                raw = json.loads(traj.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"warning: skip {traj}: {exc}", file=sys.stderr)
                continue
            instance_id = str(raw.get("instance_id") or "").strip()
            repo_dir = ""
            meta_path = pred / f"{instance_id}.json"
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    saved = str((meta or {}).get("repo_dir") or "").strip()
                    if saved and Path(saved).is_dir():
                        repo_dir = saved
                except (OSError, json.JSONDecodeError):
                    repo_dir = ""
            records.append(contextbench_record(raw, repo_root=repo_dir))
        return records, raw_dir
    return [_normalize_one(item) for item in _load_jsonl(pred)], pred.parent


def _normalize_one(record: dict[str, Any]) -> dict[str, Any]:
    return contextbench_record(record)


def write_pred_jsonl(records: list[dict[str, Any]], dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description="Official ContextBench score")
    parser.add_argument("--pred", type=Path, required=True)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--cache", type=Path, default=None)
    args = parser.parse_args()
    if not CONTEXTBENCH_ROOT.is_dir():
        raise SystemExit(f"ContextBench checkout missing: {CONTEXTBENCH_ROOT}")
    gold = args.gold.expanduser().resolve()
    if not gold.is_file():
        raise SystemExit(f"gold parquet not found: {gold}")
    records, raw_dir = load_raw_records(args.pred)
    if not records:
        raise SystemExit(f"no trajectories in {args.pred}")
    pred_jsonl = raw_dir / "pred.jsonl"
    write_pred_jsonl(records, pred_jsonl)
    out = args.out.expanduser().resolve() if args.out else raw_dir / "eval.jsonl"
    cache = args.cache.expanduser().resolve() if args.cache else raw_dir.parent.parent / "repos"
    cache.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "contextbench.evaluate",
        "--gold",
        str(gold),
        "--pred",
        str(pred_jsonl),
        "--cache",
        str(cache),
        "--out",
        str(out),
    ]
    print(" ".join(cmd), flush=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        str(CONTEXTBENCH_ROOT)
        + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    )
    completed = subprocess.run(cmd, cwd=str(CONTEXTBENCH_ROOT), env=env, check=False)
    _drop_editloc(out, records)
    print(
        "NOTE: EditLoc is not reportable. Official evaluate.py falls back to "
        "gold `patch` when model_patch is empty; this pipeline is locate-only.",
        file=sys.stderr,
        flush=True,
    )
    print(
        "NOTE: This is a locate-only graph ablation, not a ContextBench "
        "leaderboard run. Do not compare File Cov to MiniSWE / Prometheus.",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(completed.returncode)


def _drop_editloc(eval_path: Path, records: list[dict[str, Any]]) -> None:
    """Strip EditLoc so gold-vs-gold scores cannot be copied into a report."""
    if not eval_path.is_file():
        return
    has_real_patch = any(str(item.get("model_patch") or "").strip() for item in records)
    if has_real_patch:
        return
    rewritten: list[dict[str, Any]] = []
    try:
        lines = eval_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            rewritten.append({"raw": line})
            continue
        if isinstance(row, dict):
            row.pop("editloc", None)
            row["editloc_omitted"] = "empty_model_patch_would_use_gold"
        rewritten.append(row)
    with eval_path.open("w", encoding="utf-8") as handle:
        for row in rewritten:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
