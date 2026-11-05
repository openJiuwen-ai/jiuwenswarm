# coding: utf-8
"""Train one SkillsBench task and keep a start-to-finish log of every round.

The trainer stdout is copied into the log as it is produced. When a round
finishes (``apply_report.json`` is on disk), the log also records that round's
inputs and outputs: the skill files the agent received, the rollout score, the
verifier reward, files the agent wrote, the rewrite report, and the new skill
version.

Usage (from the jiuwenswarm repo)::

    .\\.venv\\Scripts\\python.exe -u scripts\\skillsbench_train_logged.py
    .\\.venv\\Scripts\\python.exe -u scripts\\skillsbench_train_logged.py xlsx-recover-data --num-epochs 3
"""

from __future__ import annotations

import argparse
import hashlib
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

TASK_ID_DEFAULT = "xlsx-recover-data"
TEXT_SUFFIXES = {".md", ".json", ".jsonl", ".txt", ".csv", ".py", ".log", ".yml", ".yaml", ".toml"}
SKIP_INLINE_NAMES = {
    "jiuwenswarm_raw.txt",
    "jiuwenswarm_trace_steps.txt",
    "ctrf.json",
    "conversation.json",
    "target_system_prompt.txt",
}
INLINE_LIMIT = 80_000


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _setup_logging() -> None:
    """INFO to stdout, WARNING+ to stderr, message-only format (matches the old echo).

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


def _now() -> str:
    # Timezone-explicit now, rendered in local time so train.log stamps stay familiar.
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


class _Log:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = path.open("a", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()

    def write(self, text: str, *, stamp: bool = False) -> None:
        if stamp:
            text = f"{_now()} {text}"
        if not text.endswith("\n"):
            text += "\n"
        with self._lock:
            self._fh.write(text)
            self._fh.flush()
            # Console echo via logging; the trailing newline is the record separator.
            logger.info(text[:-1])

    def close(self) -> None:
        with self._lock:
            self._fh.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    if len(text) <= INLINE_LIMIT:
        return text
    omitted = len(text) - INLINE_LIMIT
    return text[:INLINE_LIMIT] + f"\n...[truncated {omitted} chars, file={path}]\n"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _iter_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())


def _append_file(log: _Log, path: Path, root: Path) -> None:
    size = path.stat().st_size
    header = f"--- file {_rel(path, root)} bytes={size} sha256={_sha256(path)} ---"
    log.write(header)
    if path.name in SKIP_INLINE_NAMES or path.suffix.lower() not in TEXT_SUFFIXES:
        log.write("(binary or trace; path recorded, body omitted)")
        return
    if size > INLINE_LIMIT:
        log.write(_read_text(path))
        return
    log.write(_read_text(path).rstrip("\n"))


def _prediction_dirs(step_dir: Path) -> list[Path]:
    root = step_dir / "rollout" / "predictions"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def _score_lines(step_dir: Path) -> list[str]:
    path = step_dir / "rollout" / "results.jsonl"
    if not path.is_file():
        return ["(no results.jsonl)"]
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            lines.append(raw[:500])
            continue
        lines.append(
            "id={id} hard={hard} soft={soft} fail_reason={reason}".format(
                id=row.get("id"),
                hard=row.get("hard"),
                soft=row.get("soft"),
                reason=row.get("fail_reason") or "",
            )
        )
    return lines or ["(empty results.jsonl)"]


def _reward_file_for(step_dir: Path, pred: Path) -> Path | None:
    """Locate ``reward.txt`` for one prediction dir.

    Verifier artifacts moved out of ``predictions/`` (to ``<task_root>/logs/verifier/<iter>/``)
    so a later iteration's agent cannot read the pytest expected values; keep the
    legacy in-tree location as a fallback for older runs.
    """
    candidates = [
        step_dir.parents[2] / "logs" / "verifier" / step_dir.name / pred.name / "verifier" / "reward.txt",
        pred / "verifier_logs" / "verifier" / "reward.txt",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def _snapshot_round(log: _Log, step_dir: Path, task_root: Path) -> None:
    """Write one finished round's inputs and outputs into the log."""
    step = step_dir.name
    log.write("")
    log.write("=" * 72, stamp=True)
    log.write(f"{step} 输入：agent 收到的 skill")
    log.write("=" * 72)
    saw_skill = False
    for pred in _prediction_dirs(step_dir):
        skills = pred / "jiuwenswarm_exec" / ".agents" / "skills"
        for path in _iter_files(skills):
            if path.name != "SKILL.md":
                continue
            saw_skill = True
            _append_file(log, path, task_root)
    if not saw_skill:
        log.write("(workspace skill copies not found)")

    log.write("")
    log.write("=" * 72, stamp=True)
    log.write(f"{step} 输出：评分、agent 产物、改写后的 skill")
    log.write("=" * 72)
    log.write("score: " + " | ".join(_score_lines(step_dir)))

    for pred in _prediction_dirs(step_dir):
        reward = _reward_file_for(step_dir, pred)
        if reward is not None and reward.is_file():
            _append_file(log, reward, task_root)
        output = pred / "jiuwenswarm_exec" / "output"
        files = _iter_files(output)
        if not files:
            log.write(f"(no agent output files under {_rel(output, task_root)})")
        for path in files:
            _append_file(log, path, task_root)

    for name in ("apply_report.json", "candidate_skill.md"):
        path = step_dir / name
        if path.is_file():
            _append_file(log, path, task_root)
    for path in sorted(step_dir.glob("candidate_skill__*.md")):
        _append_file(log, path, task_root)

    version = step_dir.name.removeprefix("iter_")
    written = sorted(task_root.glob(f"skills/*/skill_v{version}.md"))
    written += [p for p in (task_root / f"skill_v{version}.md",) if p.is_file()]
    if written:
        log.write(f"{step} 写出的新 skill:")
        for path in written:
            _append_file(log, path, task_root)
    else:
        log.write(f"{step} 没有写出新 skill 版本")

    log.write(f"{step} 产物清单:")
    for path in _iter_files(step_dir):
        log.write(f"  {_rel(path, task_root)}  bytes={path.stat().st_size}")
    log.write("=" * 72)
    log.write("")


def _try_snapshot(log: _Log, step_dir: Path, task_root: Path) -> None:
    """Snapshot one round without raising: the watcher thread must survive a bad round."""
    try:
        _snapshot_round(log, step_dir, task_root)
    except Exception as exc:  # noqa: BLE001
        log.write(f"{step_dir.name} snapshot failed: {type(exc).__name__}: {exc}", stamp=True)
        logger.warning(f"[train-logged] {step_dir.name} snapshot failed: {exc}")


def _watch(log: _Log, task_root: Path, stop: threading.Event, logged: set[str]) -> None:
    while not stop.is_set():
        _drain_finished(log, task_root, logged)
        stop.wait(2.0)
    # Skill versions are written immediately after apply_report.json.
    time.sleep(2.0)
    _drain_finished(log, task_root, logged, allow_partial=True)


def _drain_finished(log: _Log, task_root: Path, logged: set[str], *, allow_partial: bool = False) -> None:
    if not task_root.is_dir():
        return
    for step_dir in sorted(p for p in task_root.glob("iter_*") if p.is_dir()):
        key = step_dir.name
        if key in logged:
            continue
        report = step_dir / "apply_report.json"
        results = step_dir / "rollout" / "results.jsonl"
        if report.is_file():
            if time.time() - report.stat().st_mtime < 1.5 and not allow_partial:
                continue
            _try_snapshot(log, step_dir, task_root)
            logged.add(key)
        elif allow_partial and results.is_file():
            _try_snapshot(log, step_dir, task_root)
            logged.add(key)


def _pump(proc: subprocess.Popen[str], log: _Log) -> None:
    if proc.stdout is None:
        raise RuntimeError("child stdout was not piped")
    for line in proc.stdout:
        log.write(line.rstrip("\n"))


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    repo = _repo_root()
    parser = argparse.ArgumentParser(description="Train one SkillsBench task and log every round's artifacts.")
    parser.add_argument("task_id", nargs="?", default=TASK_ID_DEFAULT)
    parser.add_argument("--num-epochs", type=int, default=3)
    parser.add_argument("--exec-timeout", type=int, default=1800)
    parser.add_argument("--verifier-mode", default="docker", choices=("docker", "local", "wsl", "inherit"))
    parser.add_argument("--output-dir", default="", help="Training output root. Default outputs/<task>-train.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir) if args.output_dir else repo / "outputs" / f"{args.task_id}-train"
    if not output_dir.is_absolute():
        output_dir = repo / output_dir
    log_path = output_dir / "train.log"
    task_root = output_dir / "tasks" / args.task_id

    cmd = [
        sys.executable,
        "-u",
        str(repo / "scripts" / "skillsbench_train_task.py"),
        args.task_id,
        "--num-epochs",
        str(args.num_epochs),
        "--exec-timeout",
        str(args.exec_timeout),
        "--output-dir",
        str(output_dir),
    ]
    if args.dry_run:
        cmd.append("--dry-run")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if args.verifier_mode != "inherit":
        env["SKILLSBENCH_VERIFIER_MODE"] = args.verifier_mode

    log = _Log(log_path)
    log.write("=" * 72, stamp=True)
    log.write(f"训练开始 task={args.task_id} epochs={args.num_epochs} verifier={args.verifier_mode}")
    log.write(f"output_dir={output_dir}")
    log.write(f"log={log_path}")
    log.write("command=" + " ".join(cmd))
    log.write("=" * 72)

    proc = subprocess.Popen(
        cmd,
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    logged: set[str] = set()
    stop = threading.Event()
    watcher = threading.Thread(target=_watch, args=(log, task_root, stop, logged), daemon=True)
    watcher.start()
    try:
        _pump(proc, log)
        code = int(proc.wait())
    except KeyboardInterrupt:
        proc.terminate()
        code = int(proc.wait())
        log.write(f"训练被中断 exit={code}", stamp=True)
    finally:
        stop.set()
        watcher.join(timeout=30)

    summary = output_dir / "summary.json"
    log.write("=" * 72, stamp=True)
    log.write(f"训练结束 exit={code}")
    if summary.is_file():
        log.write("summary.json:")
        _append_file(log, summary, output_dir)
    else:
        log.write(f"(no summary.json at {summary})")
    for name in ("best_skill.md", "final_skill.md"):
        path = task_root / name
        if path.is_file():
            log.write(f"结束产物 {name}:")
            _append_file(log, path, task_root)
    log.write("=" * 72)
    log.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
