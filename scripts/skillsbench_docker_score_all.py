# coding: utf-8
"""Score the full SkillsBench task set once (no train/val/test cut, no training).

Agent runs on the host via Gateway; verifier uses Docker
(``SKILLSBENCH_VERIFIER_MODE=docker``).

Usage (venv activated, Gateway up)::

    python scripts/skillsbench_docker_score_all.py
    python scripts/skillsbench_docker_score_all.py --exec-timeout 1800 --output-dir ./outputs/my_run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    """INFO to stdout, WARNING+ to stderr, message-only format (matches the old prints).

    Handlers live on this script's logger only (propagate off) so library logs
    keep flowing through their own configuration, unduplicated.
    """
    logger.setLevel(logging.INFO)
    logger.propagate = False
    out = logging.StreamHandler(sys.stdout)
    out.addFilter(lambda record: record.levelno < logging.WARNING)
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    logger.addHandler(out)
    logger.addHandler(err)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _build_all_split(tasks_dir: Path, split_root: Path) -> int:
    """Write the all-in-train split; return the task count (0 when *tasks_dir* is empty)."""
    from openjiuwen.agent_evolving.skill_train.envs.skillsbench.dataloader import (
        load_task_records,
    )

    items = load_task_records(tasks_dir)
    if not items:
        return 0

    for name in ("train", "val", "test"):
        folder = split_root / name
        folder.mkdir(parents=True, exist_ok=True)
        payload = items if name == "train" else []
        (folder / "items.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    logger.info(f"[skillsbench] all-tasks split: n={len(items)} -> {split_root} (all in train/)")
    return len(items)


def _summarize(results_path: Path, out_path: Path) -> dict:
    rows = []
    if results_path.is_file():
        for line_no, line in enumerate(results_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # results.jsonl is appended incrementally; a killed run can leave a
                # truncated last line. Skip it so the summary still gets written
                # (the library resume reader tolerates the same).
                logger.warning(f"[skillsbench] skip corrupt line {line_no} in {results_path}")
    n = len(rows)
    hard = sum(float(r.get("hard") or 0) for r in rows) / n if n else 0.0
    soft = sum(float(r.get("soft") or 0) for r in rows) / n if n else 0.0
    fails: dict[str, int] = {}
    for r in rows:
        fr = str(r.get("fail_reason") or ("ok" if r.get("hard") else "unknown"))
        if len(fr) > 80:
            fr = fr[:80] + "..."
        fails[fr] = fails.get(fr, 0) + 1
    summary = {
        "verifier_mode": os.environ.get("SKILLSBENCH_VERIFIER_MODE", "docker"),
        "n": n,
        "hard": hard,
        "soft": soft,
        "fail_reasons": fails,
        "results_jsonl": str(results_path),
        "items": [
            {
                "id": r.get("id"),
                "hard": r.get("hard"),
                "soft": r.get("soft"),
                "fail_reason": r.get("fail_reason"),
                "agent_ok": r.get("agent_ok"),
            }
            for r in rows
        ],
    }
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"[skillsbench] TOTAL n={n} hard={hard:.4f} soft={soft:.4f}")
    logger.info(f"[skillsbench] wrote {out_path}")
    return summary


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    root = _repo_root()
    parser = argparse.ArgumentParser(description="SkillsBench full-dataset docker score (no training).")
    parser.add_argument(
        "--tasks-dir",
        default="",
        help="Tasks dir; defaults to $SKILLSBENCH_TASKS, then $SKILLSBENCH_ROOT/tasks.",
    )
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--exec-timeout", type=int, default=int(os.environ.get("EXEC_TIMEOUT", "1800")))
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--skill-init", default="")
    args = parser.parse_args(argv)

    os.environ["SKILLSBENCH_VERIFIER_MODE"] = os.environ.get("SKILLSBENCH_VERIFIER_MODE") or "docker"

    tasks_str = args.tasks_dir.strip() or os.environ.get("SKILLSBENCH_TASKS", "").strip()
    if not tasks_str:
        root_env = os.environ.get("SKILLSBENCH_ROOT", "").strip()
        if not root_env:
            logger.error(
                "[skillsbench] tasks dir unknown: pass --tasks-dir or set SKILLSBENCH_TASKS / SKILLSBENCH_ROOT"
            )
            return 2
        tasks_str = str(Path(root_env) / "tasks")
    tasks_dir = Path(tasks_str)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_dir = Path(
        args.output_dir
        or (root / "outputs" / f"skillsbench_docker_score_all_{stamp}")
    ).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    split_root = output_dir / "_all_tasks_split"
    n_tasks = _build_all_split(tasks_dir, split_root)
    if n_tasks == 0:
        logger.error(f"No SkillsBench tasks with task.md under {Path(args.tasks_dir)}")
        return 2

    from openjiuwen.agent_evolving.skill_train import TrainLaunchOptions, run_offline_eval

    opts = TrainLaunchOptions(
        env_name="skillsbench",
        target_backend="jiuwenswarm_cli_exec",
        skill_init=args.skill_init,
        split_dir=str(split_root),
        output_dir=str(output_dir),
        exec_timeout=args.exec_timeout,
        workers=args.workers,
        use_gate=False,
        extra_env_kwargs={
            "split_mode": "split_dir",
            "verifier_mode": os.environ["SKILLSBENCH_VERIFIER_MODE"],
            "data_path": "",
        },
    )
    logger.info(
        f"[skillsbench] verifier_mode={os.environ['SKILLSBENCH_VERIFIER_MODE']} "
        f"output={output_dir} n_tasks=all-in-train (eval-only, no optimizer required)"
    )
    # run_offline_eval + jiuwenswarm_cli_exec skips optimizer credential checks.
    summary = run_offline_eval(opts, split="train")
    logger.info(json.dumps(summary, ensure_ascii=False, indent=2))

    results = output_dir / "results.jsonl"
    if not results.is_file():
        # eval may nest under predictions parent; sorted so a reused --output-dir
        # cannot pick a stale file nondeterministically.
        candidates = sorted(output_dir.rglob("results.jsonl"))
        if candidates:
            results = candidates[0]
            logger.info(f"[skillsbench] results.jsonl found at {results}")
    if not results.is_file():
        logger.error(f"[skillsbench] no results.jsonl under {output_dir}")
        return 1
    _summarize(results, output_dir / "summary.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
