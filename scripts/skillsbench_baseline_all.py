# coding: utf-8
"""Score every SkillsBench task once on its original skills. No rewrite.

Each task uses the same starting document as training iteration 1: the
concatenation of ``environment/skills/*/SKILL.md``. The analyst is not called.

Requires the Gateway (``jiuwenswarm-start app``). Verifier defaults to local,
matching skill-train. Already-finished ids in ``results.jsonl`` are skipped.

Usage (from the jiuwenswarm repo, venv python)::

    python scripts/skillsbench_baseline_all.py
    python scripts/skillsbench_baseline_all.py --output-dir ./outputs/skillsbench_baseline --exec-timeout 1800
    python scripts/skillsbench_baseline_all.py --ids citation-check
    python scripts/skillsbench_baseline_all.py --verifier-mode docker --include-extra
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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _configure_logging(level: str) -> None:
    """INFO to stdout, WARNING+ to stderr, message-only format (matches the old prints).

    Handlers live on this script's logger only (propagate off) so library logs
    keep flowing through their own configuration, unduplicated.
    """
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    out = logging.StreamHandler(sys.stdout)
    out.addFilter(lambda record: record.levelno < logging.WARNING)
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    logger.addHandler(out)
    logger.addHandler(err)
    try:
        from openjiuwen.core.common.logging import logger as oj_logger

        oj_logger.reconfigure(
            {
                "level": level.upper(),
                "output": ["console"],
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            }
        )
    except Exception:
        logger.debug("openjiuwen logger reconfigure skipped", exc_info=True)


def _load_items(tasks_dir: Path, include_extra: bool) -> list[dict]:
    from openjiuwen.agent_evolving.skill_train.envs.skillsbench.dataloader import load_task_records

    items = load_task_records(tasks_dir)
    if include_extra:
        extra = tasks_dir.parent / "tasks-extra"
        items.extend(load_task_records(extra))
    return items


def _summarize(results_path: Path, items: list[dict], out_path: Path, verifier_mode: str) -> dict:
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
                logger.warning(f"[skillsbench baseline] skip corrupt line {line_no} in {results_path}")
    by_id = {str(item["id"]): item for item in items}
    n = len(rows)
    hard = sum(float(row.get("hard") or 0) for row in rows) / n if n else 0.0
    soft = sum(float(row.get("soft") or 0) for row in rows) / n if n else 0.0
    listed = []
    for row in rows:
        item = by_id.get(str(row.get("id")), {})
        skills = item.get("skills") or []
        listed.append(
            {
                "id": row.get("id"),
                "hard": row.get("hard"),
                "soft": row.get("soft"),
                "n_skills": len(skills),
                "skills": [str(record.get("name") or "") for record in skills if isinstance(record, dict)],
                "fail_reason": row.get("fail_reason"),
                "agent_ok": row.get("agent_ok"),
            }
        )
    summary = {
        "kind": "baseline",
        "verifier_mode": verifier_mode,
        "n": n,
        "n_tasks": len(items),
        "hard": hard,
        "soft": soft,
        "n_passed": sum(1 for row in rows if int(row.get("hard") or 0) == 1),
        "results_jsonl": str(results_path),
        "items": listed,
    }
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info(
        f"[skillsbench baseline] n={n}/{len(items)} passed={summary['n_passed']} "
        f"hard={hard:.4f} soft={soft:.4f}"
    )
    logger.info(f"[skillsbench baseline] wrote {out_path}")
    return summary


def main(argv: list[str] | None = None) -> int:
    root = _repo_root()
    parser = argparse.ArgumentParser(
        description="SkillsBench full-task baseline: original skills, one rollout, no rewrite."
    )
    parser.add_argument(
        "--tasks-dir",
        default="",
        help="Tasks dir; defaults to $SKILLSBENCH_TASKS, then $SKILLSBENCH_ROOT/tasks.",
    )
    parser.add_argument("--include-extra", action="store_true", help="Also score <root>/tasks-extra.")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--exec-timeout", type=int, default=int(os.environ.get("EXEC_TIMEOUT", "1800")))
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--verifier-mode",
        choices=("local", "docker", "wsl"),
        default=os.environ.get("SKILLSBENCH_VERIFIER_MODE", "local") or "local",
    )
    parser.add_argument("--limit", type=int, default=0, help="Score only the first N tasks (debug).")
    parser.add_argument(
        "--ids",
        default="",
        help="Comma-separated task ids to score (for example citation-check). Empty means every task.",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--skip-gateway-check", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.log_level)
    os.environ["SKILLSBENCH_VERIFIER_MODE"] = args.verifier_mode

    tasks_str = args.tasks_dir.strip() or os.environ.get("SKILLSBENCH_TASKS", "").strip()
    if not tasks_str:
        root_env = os.environ.get("SKILLSBENCH_ROOT", "").strip()
        if not root_env:
            logger.error(
                "[skillsbench baseline] tasks dir unknown: pass --tasks-dir or set SKILLSBENCH_TASKS / SKILLSBENCH_ROOT"
            )
            return 2
        tasks_str = str(Path(root_env) / "tasks")
    tasks_dir = Path(tasks_str)

    if not args.skip_gateway_check:
        from jiuwenswarm.cli.chat import _build_default_gateway_url
        from jiuwenswarm.cli.skill_train import check_gateway

        url = _build_default_gateway_url()
        if not check_gateway(url):
            logger.error(f"Gateway unreachable at {url}. Start with: .\\.venv\\Scripts\\jiuwenswarm-start.exe app")
            return 3

    from openjiuwen.agent_evolving.skill_train.envs.rollout_batch import run_parallel_rollout
    from openjiuwen.agent_evolving.skill_train.envs.skillsbench import per_task_train
    from openjiuwen.agent_evolving.skill_train.envs.skillsbench.per_task_train import _task_base_skill
    from openjiuwen.agent_evolving.skill_train.envs.skillsbench.rollout import (
        SkillsBenchItemConfig,
        process_one,
    )
    from openjiuwen.agent_evolving.skill_train.jiuwenswarm_exec import configure_jiuwenswarm_exec
    from openjiuwen.agent_evolving.skill_train.model_compat import set_target_backend

    items = _load_items(tasks_dir, args.include_extra)
    wanted = {part.strip() for part in args.ids.split(",") if part.strip()}
    if wanted:
        items = [item for item in items if str(item.get("id")) in wanted]
        missing = wanted - {str(item.get("id")) for item in items}
        if missing:
            logger.error(f"Unknown task ids: {', '.join(sorted(missing))}")
            return 2
    if args.limit > 0:
        items = items[: args.limit]
    if not items:
        logger.error(f"No tasks with task.md under {tasks_dir}")
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir or (root / "outputs" / f"skillsbench_baseline_{stamp}")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    fallback_path = Path(per_task_train.__file__).resolve().parent / "skills" / "initial.md"
    fallback = fallback_path.read_text(encoding="utf-8") if fallback_path.is_file() else ""

    set_target_backend("jiuwenswarm_cli_exec")
    from jiuwenswarm.cli.skill_train import _resolve_cli_path

    configure_jiuwenswarm_exec(cli_path=_resolve_cli_path(""))

    def _run(item: dict) -> dict:
        skill = _task_base_skill(item, fallback)
        verifier_timeout = int(item.get("verifier_timeout_sec") or 900)
        cfg = SkillsBenchItemConfig(
            out_root=str(output_dir),
            skill_content=skill,
            exec_timeout=max(int(item.get("timeout_sec") or 0), args.exec_timeout),
            verifier_mode=args.verifier_mode,
        )
        row = process_one(item, cfg)
        row["n_skills"] = len(item.get("skills") or [])
        row["verifier_timeout_sec"] = verifier_timeout
        return row

    deadlines = [
        max(int(item.get("timeout_sec") or 0), args.exec_timeout) + int(item.get("verifier_timeout_sec") or 900)
        for item in items
    ]
    logger.info(
        f"[skillsbench baseline] tasks={len(items)} verifier={args.verifier_mode} "
        f"workers={args.workers} output={output_dir}"
    )
    run_parallel_rollout(
        items,
        str(output_dir),
        process_one=_run,
        workers=max(1, args.workers),
        task_timeout=max(deadlines) if deadlines else args.exec_timeout + 900,
    )
    _summarize(output_dir / "results.jsonl", items, output_dir / "summary.json", args.verifier_mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
