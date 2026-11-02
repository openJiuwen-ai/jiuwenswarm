# coding: utf-8
"""Train SkillsBench tasks that do not yet have a finished ``summary.json``.

A task is considered done when::

    <output-root>/<task_id>/summary.json

exists (default ``output-root`` = ``jiuwenswarm/outputs/outputs``).
Partial directories without ``summary.json`` are retried (skill-train may resume).

Usage (from the jiuwenswarm repo, venv python)::

    .\\.venv\\Scripts\\python.exe scripts\\skillsbench_train_remaining.py --list
    .\\.venv\\Scripts\\python.exe scripts\\skillsbench_train_remaining.py --dry-run
    .\\.venv\\Scripts\\python.exe scripts\\skillsbench_train_remaining.py --num-epochs 3 --parallel 2
    .\\.venv\\Scripts\\python.exe scripts\\skillsbench_train_remaining.py --verifier-mode wsl
    .\\.venv\\Scripts\\python.exe scripts\\skillsbench_train_remaining.py --skip fix-druid-loophole-cve
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
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


def _skillsbench_root() -> Path | None:
    """SkillsBench root (parent of ``tasks/``); ``SKILLSBENCH_ROOT`` only, no fallback."""
    raw = os.environ.get("SKILLSBENCH_ROOT", "").strip()
    return Path(raw) if raw else None


def _list_task_ids(tasks_dir: Path) -> list[str]:
    ids: list[str] = []
    for child in sorted(tasks_dir.iterdir()):
        if child.is_dir() and (child / "task.md").is_file():
            ids.append(child.name)
    return ids


def _is_done(output_root: Path, task_id: str) -> bool:
    return (output_root / task_id / "summary.json").is_file()


def _has_partial(output_root: Path, task_id: str) -> bool:
    d = output_root / task_id
    return d.is_dir() and not _is_done(output_root, task_id)


def _load_skip_file(output_root: Path) -> set[str]:
    """Optional persistent skip list: <output-root>/_skip_tasks.txt (one id per line)."""
    path = output_root / "_skip_tasks.txt"
    if not path.is_file():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def _resolve_remaining(
    *,
    task_ids: list[str],
    output_root: Path,
    skip: set[str],
    only: set[str],
    include_done: bool,
) -> list[str]:
    skip = set(skip) | _load_skip_file(output_root)
    remaining: list[str] = []
    for task_id in task_ids:
        if only and task_id not in only:
            continue
        if task_id in skip:
            continue
        if _is_done(output_root, task_id) and not include_done:
            continue
        remaining.append(task_id)
    return remaining


_LOG_LOCK = threading.Lock()


def _append_log(log_path: Path, line: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_LOCK:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    repo = _repo_root()
    parser = argparse.ArgumentParser(description="Train remaining SkillsBench tasks (skip those with summary.json).")
    parser.add_argument("--num-epochs", type=int, default=3)
    parser.add_argument("--exec-timeout", type=int, default=1800)
    parser.add_argument(
        "--parallel",
        type=int,
        default=2,
        help="How many tasks to train at once (default 2). Each task still has its own output dir.",
    )
    parser.add_argument(
        "--output-root",
        default="",
        help="Root for per-task output dirs (default: <repo>/outputs/outputs).",
    )
    parser.add_argument(
        "--verifier-mode",
        choices=("local", "docker", "wsl", "inherit"),
        default="inherit",
        help="Set SKILLSBENCH_VERIFIER_MODE for child trains. inherit=keep current env.",
    )
    parser.add_argument("--skip", action="append", default=[], help="Task id to skip (repeatable).")
    parser.add_argument("--only", action="append", default=[], help="Only these task ids (repeatable).")
    parser.add_argument("--include-done", action="store_true", help="Do not skip tasks that already have summary.json.")
    parser.add_argument("--stop-on-fail", action="store_true", help="Stop the loop when one task fails.")
    parser.add_argument("--list", action="store_true", help="Print remaining task ids and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without training.")
    parser.add_argument(
        "--log-file",
        default="",
        help="Append progress JSON lines (default: <output-root>/_remaining_train_log.jsonl).",
    )
    args = parser.parse_args(argv)

    output_root = Path(args.output_root) if args.output_root else repo / "outputs" / "outputs"
    if not output_root.is_absolute():
        output_root = repo / output_root

    root = _skillsbench_root()
    if root is None:
        logger.error("[remaining] SKILLSBENCH_ROOT is not set (expected: parent dir of tasks/)")
        return 2
    tasks_dir = root / "tasks"
    task_ids = _list_task_ids(tasks_dir)
    skip = set(args.skip or [])
    only = set(args.only or [])
    remaining = _resolve_remaining(
        task_ids=task_ids,
        output_root=output_root,
        skip=skip,
        only=only,
        include_done=args.include_done,
    )

    done_n = sum(1 for t in task_ids if _is_done(output_root, t))
    partial_n = sum(1 for t in remaining if _has_partial(output_root, t))
    parallel = max(1, int(args.parallel))
    logger.info(
        f"[remaining] tasks_total={len(task_ids)} done={done_n} remaining={len(remaining)} "
        f"partial_in_remaining={partial_n} parallel={parallel}"
    )
    logger.info(f"[remaining] output_root={output_root}")
    if args.verifier_mode != "inherit":
        logger.info(f"[remaining] verifier_mode={args.verifier_mode}")
    else:
        logger.info(f"[remaining] verifier_mode={os.environ.get('SKILLSBENCH_VERIFIER_MODE', '')!r} (inherit)")

    if args.list or args.dry_run:
        for i, task_id in enumerate(remaining, start=1):
            mark = "partial" if _has_partial(output_root, task_id) else "fresh"
            logger.info(f"  {i:3d}. {task_id}  ({mark})")
        if args.list and not args.dry_run:
            return 0

    if not remaining:
        logger.info("[remaining] nothing to run")
        return 0

    train_script = repo / "scripts" / "skillsbench_train_task.py"
    py = Path(sys.executable)
    log_path = Path(args.log_file) if args.log_file else output_root / "_remaining_train_log.jsonl"
    if not log_path.is_absolute():
        log_path = repo / log_path

    env = os.environ.copy()
    if args.verifier_mode != "inherit":
        env["SKILLSBENCH_VERIFIER_MODE"] = args.verifier_mode

    failed: list[str] = []
    ok: list[str] = []
    halt = False

    def _record(task_id: str, out_dir: Path, code: int, started: float) -> None:
        elapsed = round(time.time() - started, 1)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
            "exit": code,
            "elapsed_sec": elapsed,
            "output_dir": str(out_dir),
            "summary_exists": _is_done(output_root, task_id),
            "verifier_mode": env.get("SKILLSBENCH_VERIFIER_MODE", ""),
        }
        _append_log(log_path, json.dumps(record, ensure_ascii=False))
        if code == 0:
            ok.append(task_id)
            logger.info(f"[remaining] id={task_id} ok elapsed={elapsed}s")
        else:
            failed.append(task_id)
            logger.error(f"[remaining] id={task_id} exit={code} elapsed={elapsed}s")

    def _launch(index: int, task_id: str) -> tuple[subprocess.Popen[str], object] | None:
        out_dir = output_root / task_id
        cmd = [
            str(py),
            "-u",
            str(train_script),
            task_id,
            "--num-epochs",
            str(args.num_epochs),
            "--exec-timeout",
            str(args.exec_timeout),
            "--output-dir",
            str(out_dir),
        ]
        if args.dry_run:
            cmd.append("--dry-run")
        logger.info(f"[remaining] progress {index}/{len(remaining)} id={task_id}")
        logger.info("[remaining] " + " ".join(cmd))
        if args.dry_run:
            code = subprocess.run(cmd, cwd=repo, env=env).returncode
            _record(task_id, out_dir, code, time.time())
            return None
        out_dir.mkdir(parents=True, exist_ok=True)
        train_log = out_dir / "train.log"
        log_handle = train_log.open("w", encoding="utf-8", buffering=1)
        log_handle.write("[remaining] " + " ".join(cmd) + "\n")
        log_handle.flush()
        logger.info(f"[remaining] train_log={train_log}")
        child_env = dict(env)
        child_env["PYTHONUNBUFFERED"] = "1"
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=repo,
                env=child_env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
        except Exception:
            log_handle.close()
            raise
        return proc, log_handle

    inflight: list[dict] = []

    def _reap(block: bool) -> None:
        nonlocal halt
        while inflight:
            finished_any = False
            for slot in list(inflight):
                code = slot["proc"].poll()
                if code is None:
                    continue
                inflight.remove(slot)
                log_handle = slot.get("log")
                if log_handle is not None:
                    log_handle.close()
                _record(slot["task_id"], slot["out_dir"], code, slot["started"])
                if code != 0 and args.stop_on_fail:
                    halt = True
                finished_any = True
            if finished_any or not block:
                return
            time.sleep(1.0)

    for index, task_id in enumerate(remaining, start=1):
        if halt:
            break
        while len(inflight) >= parallel:
            _reap(block=True)
            if halt:
                break
        if halt:
            break
        launched = _launch(index, task_id)
        if launched is None:
            if failed and args.stop_on_fail:
                halt = True
            continue
        proc, log_handle = launched
        inflight.append(
            {
                "proc": proc,
                "log": log_handle,
                "task_id": task_id,
                "out_dir": output_root / task_id,
                "started": time.time(),
            }
        )
    while inflight:
        _reap(block=True)

    logger.info(f"[remaining] finished ok={len(ok)} failed={len(failed)} log={log_path}")
    if failed:
        logger.error(f"[remaining] failed_ids={', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
