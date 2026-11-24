"""``jiuwenswarm-paper``: generate a research paper with the agent-core paper pipeline, or resume one.

    jiuwenswarm-paper run    --run-dir runs/p1 --topic "..." [--config cfg.yaml] [--env-note note.md]
    jiuwenswarm-paper resume --run-dir runs/p1 [--reset-counters reporting_attempts] [--followup "..."]
    jiuwenswarm-paper revise --run-dir runs/p1 --review paperreview_review.json [--max-new-cells 20]
                             [--module-model reporting=deepseek-v4-pro]
    jiuwenswarm-paper rollback --run-dir runs/p1 --revision 1

Model credentials come from the environment (``API_KEY`` / ``API_BASE`` / ``MODEL_NAME`` /
``MODEL_PROVIDER``) or ``--env-file``; they are never written to the run directory.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from jiuwenswarm.agents.harness.common.paper_pipeline.module_models import parse_module_models
from jiuwenswarm.agents.harness.common.paper_pipeline.runner import RETRY_COUNTERS, PaperRunError, PaperRunOptions, run


class _StdoutHandler(logging.StreamHandler):
    """Plain lines on the current ``sys.stdout`` (also when it is replaced after import)."""

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, _value) -> None:
        pass


_OUT = logging.getLogger("jiuwenswarm.paper.cli")
if not _OUT.handlers:
    _handler = _StdoutHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _OUT.addHandler(_handler)
    _OUT.setLevel(logging.INFO)
    _OUT.propagate = False


def _say(text: str) -> None:
    """Command result for the operator: stdout, separate from the pipeline's own logging."""
    _OUT.info(text)


def load_env_file(path: Path) -> None:
    """Minimal KEY=VALUE loader; existing environment variables win."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jiuwenswarm-paper", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "resume", "revise"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--topic", default="", help="research topic / instruction (required for run)")
        p.add_argument("--task-id", default="paper")
        p.add_argument("--config", default=None, help="pipeline YAML (default: agent-core pipeline.default.yaml)")
        p.add_argument("--env-file", default=None, help="KEY=VALUE file with model credentials")
        p.add_argument("--env-note", default=None, help="file whose text becomes the create-mode initial prompt")
        p.add_argument("--code-react-iterations", type=int, default=None,
                       help="inner ReAct cap of the coding agent (default 80; upstream silently uses 15)")
        p.add_argument("--no-latex-rebuild", action="store_true",
                       help="do not rebuild the PDF when citations are left undefined")
        p.add_argument("--no-iclr", action="store_true",
                       help="keep the pipeline's NeurIPS template instead of re-typesetting with ICLR 2027")
        p.add_argument("--no-rigor", action="store_true",
                       help="disable the rigor protocol (prompt addenda, execution audit, paired statistics)")
        p.add_argument("--experiment-env", default=None,
                       help="KEY=VALUE file whose API_KEY/API_BASE/MODEL_NAME the experiment subprocesses use")
        p.add_argument("--experiment-model", default=None, help="override MODEL_NAME from --experiment-env")
        p.add_argument("--budget", type=float, default=None,
                       help="soft pipeline budget in yuan: past it the manager is told to write the paper")
        p.add_argument("--budget-hard", type=float, default=None, help="abort above this (default 1.3 x --budget)")
        p.add_argument("--balance-floor", type=float, default=None,
                       help="DeepSeek only: tell the manager to write the paper once the account balance drops here")
        p.add_argument("--balance-hard-floor", type=float, default=None,
                       help="abort when the balance drops here (default half of --balance-floor)")
        p.add_argument("--module-model", default="",
                       help="per-module model on the same endpoint, e.g. reporting=deepseek-v4-pro "
                            "(modules: reporting, reflection, code_implementation)")
        if name in ("resume", "revise"):
            p.add_argument("--run-id", default=None)
            p.add_argument("--followup", default="")
            p.add_argument("--skip-score", action="store_true")
        if name == "resume":
            p.add_argument("--reset-counters", default="",
                           help=f"comma-separated subset of {', '.join(RETRY_COUNTERS)}")
            p.add_argument("--allow-setting-change", action="store_true",
                           help="open revision only: accept an experiment / replication model that differs from "
                                "the one the revision's executed cells used (recorded in revision.json)")
        if name == "revise":
            p.add_argument("--review", required=True,
                           help="Agentic Reviewer result (paperreview_fetch.py output) or review_panel.json")
            p.add_argument("--max-new-cells", type=int, default=20,
                           help="cap on new variants (method x tier x setting) the revision may add")
            p.add_argument("--replication-model", default=None,
                           help="second answering model exposed to experiments as REPLICATION_MODEL_NAME")
            p.add_argument("--note-file", default=None, help="extra operator note appended to the revision brief")
            p.add_argument("--revision-rounds", type=int, default=12, help="extra manager rounds for the revision")
            p.add_argument("--writing-only", action="store_true",
                           help="answer the review by rewriting only: no new experiment cell may run")
    rollback = sub.add_parser("rollback", help="restore the pre-revision paper of a revision (offline)")
    rollback.add_argument("--run-dir", required=True)
    rollback.add_argument("--revision", type=int, required=True)
    rollback.add_argument("--run-id", default=None)
    rollback.add_argument("--task-id", default="paper")
    return parser


def _rollback(args) -> None:
    from jiuwenswarm.agents.harness.common.paper_pipeline import revision_state
    from jiuwenswarm.agents.harness.common.paper_pipeline.revision import rollback_paper
    from jiuwenswarm.agents.harness.common.paper_pipeline.runner import find_run_id, log_event

    run_dir = Path(args.run_dir)
    state = next((s for s in revision_state.all_revisions(run_dir) if s.index == args.revision), None)
    if state is None or not state.paper_snapshot:
        raise PaperRunError(f"revision {args.revision} has no recorded paper snapshot")
    paper_dir = run_dir / "experiments" / (args.run_id or find_run_id(run_dir, args.task_id)) / "paper"
    rejected = rollback_paper(paper_dir, Path(state.paper_snapshot))
    state.acceptance["rolled_back"] = {"rejected_paper": str(rejected), "restored_from": state.paper_snapshot}
    revision_state.save(state, run_dir)
    log_event(run_dir, {"event": "rollback", "revision": args.revision, "rejected_paper": str(rejected),
                        "restored_from": state.paper_snapshot})
    _say(f"restored {state.paper_snapshot} -> {paper_dir}; revised paper kept at {rejected}")


def _dispatch(args) -> None:
    if args.command == "rollback":
        _rollback(args)
        return
    if args.env_file:
        load_env_file(Path(args.env_file))
    missing = [k for k in ("API_KEY", "API_BASE", "MODEL_NAME") if not os.environ.get(k)]
    if missing:
        raise PaperRunError(f"missing model settings: {', '.join(missing)}")
    if args.command == "run" and not args.topic:
        raise PaperRunError("--topic is required for run")
    try:
        parse_module_models(args.module_model)
    except ValueError as exc:
        raise PaperRunError(str(exc)) from exc

    opts = PaperRunOptions(
        run_dir=Path(args.run_dir),
        topic=args.topic,
        task_id=args.task_id,
        config_path=args.config,
        env_note=Path(args.env_note).read_text(encoding="utf-8") if args.env_note else "",
        code_react_iterations=args.code_react_iterations,
        rebuild_latex=not args.no_latex_rebuild,
        iclr_template=not args.no_iclr,
        rigor=not args.no_rigor,
        experiment_env=args.experiment_env,
        experiment_model=args.experiment_model,
        budget_soft=args.budget,
        budget_hard=args.budget_hard,
        balance_floor=args.balance_floor,
        balance_hard_floor=args.balance_hard_floor,
        module_models=parse_module_models(args.module_model),
    )
    if args.command in ("resume", "revise"):
        opts.run_id = args.run_id
        opts.followup = args.followup
        opts.skip_score = args.skip_score
    if args.command == "resume":
        opts.reset_counters = [c.strip() for c in args.reset_counters.split(",") if c.strip()]
        opts.allow_setting_change = args.allow_setting_change
    if args.command == "revise":
        opts.review_path = Path(args.review)
        opts.max_new_cells = args.max_new_cells
        opts.replication_model = args.replication_model
        opts.revision_note = Path(args.note_file).read_text(encoding="utf-8") if args.note_file else ""
        opts.revision_rounds = args.revision_rounds
        opts.writing_only = args.writing_only
    asyncio.run(run(opts, resume=args.command in ("resume", "revise")))



def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        _dispatch(args)
    except PaperRunError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
