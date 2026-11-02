# coding: utf-8
"""Train SkillsBench tasks under ``tasks/``, choosing the single- or multi-skill command.

A task is multi-skill when ``environment/skills/*/SKILL.md`` has more than one
file. One file uses ``--skill-init``. Several files repeat ``--skill-init-named``.
Folders without ``SKILL.md`` (for example a scripts-only skill) are skipped.
Omitting the task id trains every task in ``tasks/``, one after another.

Usage (from the jiuwenswarm repo)::

    .\\.venv\\Scripts\\python.exe scripts\\skillsbench_train_task.py
    .\\.venv\\Scripts\\python.exe scripts\\skillsbench_train_task.py citation-check --num-epochs 3
    .\\.venv\\Scripts\\python.exe scripts\\skillsbench_train_task.py ada-bathroom-plan-repair --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
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


def _tasks_dir(root: Path) -> Path:
    return root / "tasks"


def _find_task(root: Path, task_id: str) -> Path:
    candidate = _tasks_dir(root) / task_id
    if (candidate / "task.md").is_file():
        return candidate
    raise FileNotFoundError(f"task {task_id!r} not found under {_tasks_dir(root)}")


def _list_tasks(root: Path) -> list[tuple[str, Path]]:
    tasks_dir = _tasks_dir(root)
    if not tasks_dir.is_dir():
        raise FileNotFoundError(f"tasks directory not found: {tasks_dir}")
    found: list[tuple[str, Path]] = []
    for child in sorted(tasks_dir.iterdir()):
        if child.is_dir() and (child / "task.md").is_file():
            found.append((child.name, child))
    return found


def _skill_files(task_dir: Path) -> list[tuple[str, Path]]:
    skills_dir = task_dir / "environment" / "skills"
    if not skills_dir.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.is_file():
            found.append((child.name, skill_md.resolve()))
    return found


def _cli_path() -> Path:
    explicit = os.environ.get("JIUWENSWARM_CLI_PATH", "").strip()
    if explicit:
        return Path(explicit)
    scripts = Path(sys.executable).resolve().parent
    for name in ("jiuwenswarm.exe", "jiuwenswarm"):
        candidate = scripts / name
        if candidate.is_file():
            return candidate
    return Path("jiuwenswarm")


def _ensure_split(repo: Path, task_id: str, task_dir: Path) -> Path:
    """Point ``outputs/_skill_train_splits/<id>/<id>`` at the task package."""
    parent = repo / "outputs" / "_skill_train_splits" / task_id
    parent.mkdir(parents=True, exist_ok=True)
    link = parent / task_id
    if link.exists():
        return parent
    if os.name == "nt":
        # Junction via _winapi: the only stdlib path that avoids cmd.exe and
        # needs no symlink privilege (unlike os.symlink on Windows).
        import _winapi

        _winapi.CreateJunction(str(task_dir), str(link))
    else:
        link.symlink_to(task_dir, target_is_directory=True)
    return parent


def _build_command(
    *,
    cli: Path,
    split_dir: Path,
    output_dir: Path,
    skills: list[tuple[str, Path]],
    num_epochs: int,
    exec_timeout: int,
) -> list[str]:
    command = [
        str(cli),
        "skill-train",
        "--env",
        "skillsbench",
        "--split-dir",
        str(split_dir),
        "--num-epochs",
        str(num_epochs),
        "--exec-timeout",
        str(exec_timeout),
        "--output-dir",
        str(output_dir),
    ]
    if len(skills) == 1:
        command.extend(["--skill-init", str(skills[0][1])])
    else:
        for name, path in skills:
            command.extend(["--skill-init-named", f"{name}={path}"])
    return command


def _train_one(
    *,
    repo: Path,
    task_id: str,
    task_dir: Path,
    num_epochs: int,
    exec_timeout: int,
    output_dir: Path | None,
    dry_run: bool,
) -> int:
    skills = _skill_files(task_dir)
    kind = "single" if len(skills) <= 1 else "multi"
    names = ", ".join(name for name, _path in skills) or "(none)"
    logger.info(f"[skillsbench train] id={task_id} kind={kind} n_skills={len(skills)} skills={names}")
    logger.info(f"[skillsbench train] task={task_dir}")

    resolved_output = output_dir if output_dir is not None else repo / "outputs" / "outputs" / task_id
    if not resolved_output.is_absolute():
        resolved_output = repo / resolved_output
    split_dir = repo / "outputs" / "_skill_train_splits" / task_id
    if dry_run:
        split_dir.mkdir(parents=True, exist_ok=True)
    else:
        split_dir = _ensure_split(repo, task_id, task_dir)
    command = _build_command(
        cli=_cli_path(),
        split_dir=split_dir,
        output_dir=resolved_output,
        skills=skills,
        num_epochs=num_epochs,
        exec_timeout=exec_timeout,
    )
    logger.info("[skillsbench train] " + " ".join(command))
    if dry_run:
        return 0
    completed = subprocess.run(command, cwd=repo)
    return int(completed.returncode)


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    repo = _repo_root()
    parser = argparse.ArgumentParser(description="Train SkillsBench tasks under tasks/ as single-skill or multi-skill.")
    parser.add_argument("task_id", nargs="?", default="", help="One task name. Omit to train every task under tasks/.")
    parser.add_argument("--num-epochs", type=int, default=3)
    parser.add_argument("--exec-timeout", type=int, default=1800)
    parser.add_argument("--output-dir", default="", help="Output directory. Only used when a single task id is given.")
    parser.add_argument("--dry-run", action="store_true", help="Print the command without starting training.")
    args = parser.parse_args(argv)

    root = _skillsbench_root()
    if root is None:
        logger.error("[skillsbench train] SKILLSBENCH_ROOT is not set (expected: parent dir of tasks/)")
        return 2
    if args.task_id:
        selected = [(args.task_id, _find_task(root, args.task_id))]
        output_dir = Path(args.output_dir) if args.output_dir else None
    else:
        selected = _list_tasks(root)
        output_dir = None
        if args.output_dir:
            logger.error("[skillsbench train] --output-dir is ignored when training every task")
    if not selected:
        logger.error(f"[skillsbench train] no tasks under {_tasks_dir(root)}")
        return 2

    logger.info(f"[skillsbench train] count={len(selected)}")
    failed: list[str] = []
    for index, (task_id, task_dir) in enumerate(selected, start=1):
        logger.info(f"[skillsbench train] progress {index}/{len(selected)} id={task_id}")
        code = _train_one(
            repo=repo,
            task_id=task_id,
            task_dir=task_dir,
            num_epochs=args.num_epochs,
            exec_timeout=args.exec_timeout,
            output_dir=output_dir,
            dry_run=args.dry_run,
        )
        if code != 0:
            failed.append(task_id)
            logger.error(f"[skillsbench train] id={task_id} exit={code}")
    if failed:
        logger.error(f"[skillsbench train] failed={', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
