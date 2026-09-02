#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Rewrite pred.jsonl and run official ContextBench evaluate.

Requires a ContextBench checkout (``CONTEXTBENCH_ROOT`` / ``--contextbench-root``).

    uv run --extra code-graph --with pyarrow python scripts/eval/run_evaluate.py \
        --pred docs/ai/experiments/03-contextbench-before-productization/runs/run01-contextbench-verified/cfg_b__graph/raw
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

for path in (SCRIPT_DIR,):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from eval_env import (  # noqa: E402
    prepend_contextbench,
    prepend_swe_bench,
    resolve_contextbench_parquet,
    resolve_contextbench_root,
    resolve_swe_root,
    swe_harness_env,
)
from swe_dataset import (  # noqa: E402
    BENCHMARK_CONTEXTBENCH,
    BENCHMARK_SWE,
    official_split_n,
    resolve_benchmark,
)
from swe_submit import package_swe_submission  # noqa: E402
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


def _resolve_swe_preds(pred: Path) -> Path:
    pred = pred.expanduser().resolve()
    if pred.is_file():
        return pred
    swe = pred / "swe_preds.jsonl"
    if swe.is_file():
        return swe
    raise SystemExit(f"SWE predictions not found: {swe} (or pass a .jsonl)")


def _count_preds(swe_preds: Path) -> int:
    n = 0
    for line in swe_preds.read_text(encoding="utf-8").splitlines():
        if line.strip():
            n += 1
    return n


def _write_passat1(
    dest: Path,
    *,
    split: str,
    submitted: int,
    resolved: int | None,
) -> None:
    official = official_split_n(split)
    denom = official or submitted
    payload = {
        "split": split,
        "official_n": official,
        "submitted": submitted,
        "resolved": resolved,
        "pass_at_1_official": (resolved / denom) if resolved is not None and denom else None,
        "pass_at_1_submitted": (
            resolved / submitted if resolved is not None and submitted else None
        ),
        "leaderboard_eligible": False,
        "note": (
            f"Official {split} Pass@1 is resolved/{official}. "
            "resolved/submitted is smoke only. ARM Mac is not the leaderboard host."
            if official
            else "Unknown split; report resolved/submitted as smoke."
        ),
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _try_official_package(
    args: argparse.Namespace,
    swe_preds: Path,
    run_id: str,
    swe_root: Path,
    env: dict[str, str],
) -> bool:
    if not (swe_root / "swebench" / "submit" / "package.py").is_file():
        return False
    out_dir = swe_preds.parent / "submission"
    cmd = [
        sys.executable,
        "-m",
        "swebench",
        "submit",
        "package",
        run_id,
        "-s",
        args.swe_split,
        "-p",
        str(swe_preds),
        "-o",
        str(out_dir),
        "--trajs",
        str(swe_preds.parent),
    ]
    print(" ".join(cmd), flush=True)
    completed = subprocess.run(cmd, env=env, check=False)
    if completed.returncode != 0:
        return False
    print(
        f"official swebench submit package -> {out_dir} "
        "(re-grades from test_output.txt; metadata TODOs still need a human)",
        flush=True,
    )
    return True


def _read_official_resolved(out_dir: Path) -> int | None:
    results = out_dir / "entry" / "results" / "results.json"
    if not results.is_file():
        return None
    try:
        data = json.loads(results.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    resolved = data.get("resolved") if isinstance(data, dict) else None
    if isinstance(resolved, list):
        return len(resolved)
    return None


def _package_swe(args: argparse.Namespace, swe_preds: Path, run_id: str) -> int:
    raw_dir = swe_preds.parent
    out_dir = raw_dir / "unofficial-submission"
    model = None
    extra: dict[str, Any] = {}
    config = raw_dir.parent / "config.json"
    if config.is_file():
        try:
            cfg = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = {}
        model = ((cfg.get("model") or {}).get("MODEL_NAME") if isinstance(cfg.get("model"), dict) else None)
        extra["agent"] = "jiuwenswarm"
        extra["name"] = f"jiuwenswarm-{cfg.get('profile') or 'graph'}"
    result = package_swe_submission(
        predictions=swe_preds,
        out_dir=out_dir,
        split=args.swe_split,
        run_id=run_id,
        model=model,
        trajs=raw_dir,
        metadata_extra=extra,
    )
    official = official_split_n(args.swe_split)
    denom = official or len(result.resolved) + len(result.unresolved) + len(result.no_generation)
    print(
        f"unofficial helper {result.submission_id}: "
        f"resolved={len(result.resolved)} / {denom} "
        f"(reads report.json, does not re-grade test_output.txt) "
        f"empty={len(result.no_generation)} no_logs={len(result.no_logs)} -> {out_dir}",
        flush=True,
    )
    print(
        f"  {out_dir} is a report.json copy, not a leaderboard submission. "
        "Official entry is `swebench submit package` (re-grades test_output.txt).",
        flush=True,
    )
    _write_passat1(
        raw_dir / "swebench" / "passat1.json",
        split=args.swe_split,
        submitted=_count_preds(swe_preds),
        resolved=len(result.resolved),
    )
    return len(result.resolved)


def _eval_swe(args: argparse.Namespace) -> int:
    """Docker Pass@1, then a submission tree (official package if the checkout has it)."""
    swe_preds = _resolve_swe_preds(args.pred)
    run_id = args.run_id or "swe"
    swe_root = resolve_swe_root(getattr(args, "swe_root", None))
    if swe_root is not None:
        prepend_swe_bench(swe_root)
    env = swe_harness_env(swe_root=swe_root)
    submitted = _count_preds(swe_preds)
    official = official_split_n(args.swe_split)
    code = 0
    if not args.package_only:
        report_dir = (
            args.out.expanduser().resolve()
            if args.out
            else swe_preds.parent / "swebench"
        )
        report_dir.mkdir(parents=True, exist_ok=True)
        exe = Path(sys.executable).parent / "swebench"
        cmd = [
            str(exe) if exe.is_file() else sys.executable,
            *([] if exe.is_file() else ["-m", "swebench"]),
            "eval",
            args.swe_split,
            "-p",
            str(swe_preds),
            "--run-id",
            run_id,
            "-j",
            str(args.workers),
            "--report-dir",
            str(report_dir),
        ]
        print(" ".join(cmd), flush=True)
        if official:
            print(
                f"NOTE: official {args.swe_split} Pass@1 is resolved/{official}. "
                f"This file has {submitted} pred row(s). Missing / failed / empty "
                "patches all count as not resolved. ARM Mac is not the "
                "leaderboard host; pre-pull linux/amd64 images.",
                file=sys.stderr,
                flush=True,
            )
        if swe_root is None:
            print(
                "WARNING: SWE-bench checkout not found. Install with "
                "`uv run --with swebench` or `--with-editable /path/to/SWE-bench`, "
                "or set SWE_BENCH_ROOT.",
                file=sys.stderr,
                flush=True,
            )
        code = subprocess.run(cmd, env=env, check=False).returncode
    if args.no_package:
        return code
    used_official = False
    if not getattr(args, "unofficial_package", False):
        if swe_root is not None:
            try:
                used_official = _try_official_package(
                    args, swe_preds, run_id, swe_root, env
                )
            except Exception as exc:
                print(f"warning: official package failed: {exc}", file=sys.stderr)
        if used_official:
            resolved = _read_official_resolved(swe_preds.parent / "submission")
            _write_passat1(
                swe_preds.parent / "swebench" / "passat1.json",
                split=args.swe_split,
                submitted=submitted,
                resolved=resolved,
            )
            return code
        print(
            "NOTE: no official `swebench submit package` (need a SWE-bench "
            "checkout with swebench/submit). Not writing a self-copied "
            "submission/ from report.json — that is not a leaderboard entry. "
            "Pass --unofficial-package for a local report.json copy.",
            file=sys.stderr,
            flush=True,
        )
        if args.package_only:
            return 1
        return code
    try:
        _package_swe(args, swe_preds, run_id)
    except Exception as exc:
        print(f"warning: unofficial package failed: {exc}", file=sys.stderr, flush=True)
        if args.package_only:
            return 1
    return code


def main() -> None:
    parser = argparse.ArgumentParser(description="Official ContextBench or SWE score")
    parser.add_argument("--pred", type=Path, required=True)
    parser.add_argument("--gold", type=Path, default=None)
    parser.add_argument(
        "--benchmark",
        choices=(BENCHMARK_CONTEXTBENCH, BENCHMARK_SWE),
        default=BENCHMARK_CONTEXTBENCH,
        help="contextbench = official evaluate.py; swe = swebench eval Docker",
    )
    parser.add_argument("--swe-split", default="verified")
    parser.add_argument(
        "--swe-root",
        type=Path,
        default=None,
        help="SWE-bench checkout (SWE_BENCH_ROOT / ../SWE-bench). Used as PYTHONPATH.",
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--workers", "-j", type=int, default=1)
    parser.add_argument(
        "--package-only",
        action="store_true",
        help="SWE: skip Docker eval; run official swebench submit package only",
    )
    parser.add_argument(
        "--no-package",
        action="store_true",
        help="SWE: skip packaging after eval",
    )
    parser.add_argument(
        "--unofficial-package",
        action="store_true",
        help="SWE: write raw/unofficial-submission/ from report.json "
        "(not a leaderboard entry; official package re-grades test_output.txt)",
    )
    parser.add_argument(
        "--contextbench-root",
        type=Path,
        default=None,
        help="ContextBench checkout (or set CONTEXTBENCH_ROOT).",
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--cache", type=Path, default=None)
    args = parser.parse_args()
    if resolve_benchmark(args.benchmark) == BENCHMARK_SWE:
        raise SystemExit(_eval_swe(args))
    contextbench_root = resolve_contextbench_root(
        args.contextbench_root, parquet=args.gold
    )
    prepend_contextbench(contextbench_root)
    gold = resolve_contextbench_parquet(args.gold, root=contextbench_root)
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
        str(contextbench_root)
        + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    )
    completed = subprocess.run(cmd, cwd=str(contextbench_root), env=env, check=False)
    empty_ids, real_ids = _patch_id_sets(records)
    stripped = _drop_editloc(out, empty_ids)
    if empty_ids and real_ids:
        print(
            f"NOTE: stripped EditLoc on {stripped} empty-patch instance(s); "
            "remaining EditLoc uses the agent diff. Do not micro-average the "
            "two kinds together. Pass@1 still needs a SWE Docker harness.",
            file=sys.stderr,
            flush=True,
        )
    elif real_ids:
        print(
            "NOTE: model_patch is present. EditLoc is scored against the "
            "agent diff. Pass@1 still needs a SWE Docker harness.",
            file=sys.stderr,
            flush=True,
        )
    else:
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


def _patch_id_sets(records: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    empty_ids: set[str] = set()
    real_ids: set[str] = set()
    for item in records:
        iid = str(item.get("instance_id") or "").strip()
        if not iid:
            continue
        if str(item.get("model_patch") or "").strip():
            real_ids.add(iid)
        else:
            empty_ids.add(iid)
    return empty_ids, real_ids


def _drop_editloc(eval_path: Path, empty_ids: set[str]) -> int:
    """Strip EditLoc on empty-patch rows so gold-vs-gold cannot enter a report.

    Official evaluate.py falls back to gold ``patch`` when ``model_patch`` is
    empty. A mixed run must drop those rows one by one; keeping them would
    lift the micro EditLoc average.
    """
    if not eval_path.is_file() or not empty_ids:
        return 0
    rewritten: list[dict[str, Any]] = []
    stripped = 0
    try:
        lines = eval_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            rewritten.append({"raw": line})
            continue
        if isinstance(row, dict):
            iid = str(row.get("instance_id") or "").strip()
            if iid in empty_ids and "editloc" in row:
                row.pop("editloc", None)
                row["editloc_omitted"] = "empty_model_patch_would_use_gold"
                stripped += 1
        rewritten.append(row)
    with eval_path.open("w", encoding="utf-8") as handle:
        for row in rewritten:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return stripped


if __name__ == "__main__":
    main()
