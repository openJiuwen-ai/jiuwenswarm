"""``jiuwenswarm-paper``: generate a research paper with the agent-core paper pipeline, or resume one.

    jiuwenswarm-paper run    --run-dir runs/p1 --topic "..." [--config cfg.yaml] [--env-note note.md]
    jiuwenswarm-paper resume --run-dir runs/p1 [--reset-counters reporting_attempts] [--followup "..."]
    jiuwenswarm-paper revise --run-dir runs/p1 --review paperreview_review.json [--max-new-cells 20]
                             [--module-model reporting=deepseek-v4-pro]
    jiuwenswarm-paper review --paper-dir runs/p1/experiments/<run_id>/paper --panel env:model,...
                             [--evidence-dir .../results] [--previous old/review_panel.json]
    jiuwenswarm-paper rollback --run-dir runs/p1 --revision 1
    jiuwenswarm-paper abandon  --run-dir runs/p1 --revision 1 --reason "..."
    jiuwenswarm-paper audit    --run-dir runs/p1 [--design path/to/design.md]   (offline re-audit)
    jiuwenswarm-paper evidence --run-dir runs/p1                                (print the evidence verdict)
    jiuwenswarm-paper retire   --run-dir runs/p1 --cell abl_x_T1 --reason "..." --affects-comparison "..."

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
        p.add_argument("--delivery-policy", choices=("confirmatory", "descriptive"), default=None,
                       help="confirmatory (default): every required primary comparison must be verified; "
                            "descriptive: deliver without it, stated as a limitation (fixed before execution)")
        p.add_argument("--missing-primary-rule", choices=("refuse", "score_zero"), default=None,
                       help="a missing primary value: refuse the comparison (default) or score unanswered / "
                            "API-failed / unparsable items 0 (fixed in the protocol before execution)")
        p.add_argument("--tier-gate", action="append", default=[], metavar="TIER=RATE",
                       help="minimum constraint-activation rate of a tier, e.g. T1=0.5 (repeatable; frozen into "
                            "the protocol before execution, overrides the design's gate)")
        p.add_argument("--evidence-rail", action="store_true",
                       help="mount PaperEvidenceRail on the manager and reporting agents (rejects DONE without "
                            "accepted evidence; gives the writer the verified-evidence manifest)")
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
    review = sub.add_parser("review", help="score a paper with an independent reviewer panel")
    review.add_argument("--paper-dir", required=True, help="directory holding main.tex (+ sections/, main.bbl)")
    review.add_argument("--panel", required=True,
                        help="comma-separated <env name>:<model>, e.g. bailian:qwen3.8-max-0902,bailian:kimi-k3")
    review.add_argument("--env-dir", default=str(Path.home() / ".config" / "bdci2026"))
    review.add_argument("--out-dir", default=None, help="default: <paper-dir>/review_panel")
    review.add_argument("--evidence-dir", default=None,
                        help="results directory whose statistics.json / audit.json are given to every reviewer")
    review.add_argument("--evidence-checker", default=None,
                        help="<env name>:<model> of an extra evidence-checker call (one more paid model call)")
    review.add_argument("--max-paper-chars", type=int, default=None,
                        help="limit on the paper text sent; whole sections are dropped and listed, never cut silently")
    review.add_argument("--previous", default=None,
                        help="review_panel.json of the previous paper: compare candidates (offline, no extra call)")
    review.add_argument("--revision-dir", default=None,
                        help="revisions/revision_NN of the run: its review items / response check join the comparison")
    rollback = sub.add_parser("rollback", help="restore the pre-revision paper of a revision (offline)")
    rollback.add_argument("--run-dir", required=True)
    rollback.add_argument("--revision", type=int, required=True)
    rollback.add_argument("--run-id", default=None)
    rollback.add_argument("--task-id", default="paper")
    abandon = sub.add_parser("abandon", help="end an active revision without accepting it (offline)")
    abandon.add_argument("--run-dir", required=True)
    abandon.add_argument("--revision", type=int, required=True)
    abandon.add_argument("--reason", required=True)
    for name, text in (("audit", "re-audit the results on disk and write the evidence manifest (offline)"),
                       ("evidence", "verify the run's evidence now and print the verdict (offline)")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--run-id", default=None)
        p.add_argument("--task-id", default="paper")
        if name == "audit":
            p.add_argument("--design", default=None, help="design file (default: the run's experiment_design.md)")
            p.add_argument("--metric", action="append", default=[],
                           help="declared metric, primary first (when the design names none)")
            p.add_argument("--delivery-policy", choices=("confirmatory", "descriptive"), default=None)
            p.add_argument("--missing-primary-rule", choices=("refuse", "score_zero"), default=None)
            p.add_argument("--tier-gate", action="append", default=[], metavar="TIER=RATE")
            p.add_argument("--item-set", default=None,
                           help="item ids of the original setting, declared after execution (recorded as such: "
                                "a limitation, not a pre-registration) — only for runs whose design declared none")
    retire = sub.add_parser("retire", help="retire a cell from the design with a recorded reason (offline)")
    retire.add_argument("--run-dir", required=True)
    retire.add_argument("--run-id", default=None)
    retire.add_argument("--task-id", default="paper")
    retire.add_argument("--cell", required=True)
    retire.add_argument("--reason", required=True)
    retire.add_argument("--affects-comparison", action="append", default=[],
                        help="a comparison this retirement takes away (repeatable), e.g. 'proposed_T1 vs abl_x_T1'")
    retire.add_argument("--affects-claim", action="append", default=[],
                        help="a claim the paper can no longer support (repeatable)")
    return parser


def _exp_dir(args) -> Path:
    from jiuwenswarm.agents.harness.common.paper_pipeline.runner import find_run_id

    run_dir = Path(args.run_dir)
    return run_dir / "experiments" / (args.run_id or find_run_id(run_dir, args.task_id))


def _audit(args) -> None:
    import json

    from jiuwenswarm.agents.harness.common.paper_pipeline import evidence
    from jiuwenswarm.agents.harness.common.paper_pipeline.execution_audit import reaudit

    from jiuwenswarm.agents.harness.common.paper_pipeline import experiment_protocol, revision_state

    exp = _exp_dir(args)
    design = Path(args.design) if args.design else next(iter(sorted(exp.rglob("experiment_design.md"))), None)
    if design is None and not args.metric:
        raise PaperRunError("no experiment_design.md found: pass --design or --metric")
    try:
        gates = experiment_protocol.parse_gate_options(args.tier_gate) if args.tier_gate else None
    except ValueError as exc:
        raise PaperRunError(str(exc)) from exc
    evidence.configure(missing_primary_rule=args.missing_primary_rule, delivery_policy=args.delivery_policy,
                       tier_gates=gates)
    results = exp / "results"
    if args.item_set:
        _declare_item_set_post_hoc(results, Path(args.item_set))
    run_dir = Path(args.run_dir)
    revision = revision_state.find_open(run_dir)
    verdict = reaudit(results, design_text=design.read_text(encoding="utf-8") if design else "",
                      plan_metrics=args.metric, code_dir=exp / "generated_code",
                      design_path=design.resolve() if design else None, revision=revision, run_dir=run_dir)
    result = (revision_state.evidence_check(run_dir, revision) if revision is not None
              else evidence.verify(results))
    _say(f"audit {verdict}; {evidence.summary_line(result)}")
    _say(json.dumps({k: result[k] for k in ("protocol_id", "execution_id", "pending_tasks", "limitations")},
                    indent=2, ensure_ascii=False))


def _declare_item_set_post_hoc(results: Path, path: Path) -> None:
    """Seed the protocol chain with an operator item set (source says: after execution)."""
    from jiuwenswarm.agents.harness.common.paper_pipeline import evidence, experiment_protocol

    entry, why = experiment_protocol.item_set_entry(experiment_protocol.read_ids(path), dataset=None,
                                                    source=f"operator (declared after execution) {path}")
    if entry is None:
        raise PaperRunError(f"--item-set {path}: {why}")
    current = evidence.load_protocol(results) or {}
    if current.get("item_sets", {}).get(""):
        raise PaperRunError("the protocol already declares an item set for the original setting")
    body = {k: v for k, v in current.items() if k not in ("protocol_id", "created_at", "supersedes")}
    body["item_sets"] = {**current.get("item_sets", {}), "": entry}
    evidence.save_protocol(results, body)


def _retire(args) -> None:
    from jiuwenswarm.agents.harness.common.paper_pipeline import evidence
    from jiuwenswarm.agents.harness.common.paper_pipeline.runner import log_event

    try:
        entry = evidence.retire_cell(_exp_dir(args) / "results", args.cell, reason=args.reason,
                                     affected_comparisons=args.affects_comparison, affected_claims=args.affects_claim)
    except ValueError as exc:
        raise PaperRunError(str(exc)) from exc
    log_event(Path(args.run_dir), {"event": "retire", **entry})
    _say(f"retired {args.cell}; it takes effect with the next protocol: re-execute, or run "
         "`jiuwenswarm-paper audit` to re-check the remaining evidence under it")


def _evidence(args) -> int:
    """Print the evidence verdict; the exit code is 0 when it passes, 2 when it does not."""
    import json

    from jiuwenswarm.agents.harness.common.paper_pipeline import evidence, revision_state

    exp = _exp_dir(args)
    revision = revision_state.find_open(Path(args.run_dir))
    result = (revision_state.evidence_check(Path(args.run_dir), revision) if revision is not None
              else evidence.verify(exp / "results"))
    _say(evidence.summary_line(result))
    _say(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result["ok"] else 2


def _abandon(args) -> None:
    from jiuwenswarm.agents.harness.common.paper_pipeline import revision_state
    from jiuwenswarm.agents.harness.common.paper_pipeline.runner import log_event

    run_dir = Path(args.run_dir)
    state = next((s for s in revision_state.all_revisions(run_dir) if s.index == args.revision), None)
    if state is None or not state.active:
        raise PaperRunError(f"revision {args.revision} is not active")
    state.set_status(revision_state.ABANDONED, args.reason)
    revision_state.save(state, run_dir)
    log_event(run_dir, {"event": "abandon", "revision": args.revision, "reason": args.reason})
    _say(f"revision {args.revision:02d} abandoned; its paper snapshot stays at {state.paper_snapshot}")


def _review(args) -> None:
    import json

    from jiuwenswarm.agents.harness.common.paper_pipeline.review_panel import (
        MAX_PAPER_CHARS,
        members_from_spec,
        run_panel,
    )
    from jiuwenswarm.agents.harness.common.paper_pipeline.revision import compare_reviews

    paper_dir = Path(args.paper_dir)
    out_dir = Path(args.out_dir) if args.out_dir else paper_dir / "review_panel"
    checker = members_from_spec(args.evidence_checker, Path(args.env_dir))[0] if args.evidence_checker else None
    summary = run_panel(paper_dir, members_from_spec(args.panel, Path(args.env_dir)), out_dir,
                        evidence_dir=Path(args.evidence_dir) if args.evidence_dir else None, checker=checker,
                        max_chars=args.max_paper_chars or MAX_PAPER_CHARS)
    _say(f"mean overall {summary['overall_mean']}  each {summary['overall_each']}")
    if summary["coverage"]["truncated"]:
        _say(f"PARTIAL REVIEW: not sent {summary['coverage']['sections_omitted']}")
    if args.previous:
        items, response, deliverable = [], None, None
        if args.revision_dir:
            folder = Path(args.revision_dir)
            state = json.loads((folder / "revision.json").read_text(encoding="utf-8"))
            items = state.get("review_items", [])
            if (folder / "response_check.json").is_file():
                response = json.loads((folder / "response_check.json").read_text(encoding="utf-8"))
            deliverable = (state.get("acceptance", {}).get("final") or {}).get("deliverable")
        decision = compare_reviews(json.loads(Path(args.previous).read_text(encoding="utf-8")), summary,
                                   items=items, response=response, deliverable=deliverable)
        (out_dir / "candidate_decision.json").write_text(json.dumps(decision, indent=2, ensure_ascii=False),
                                                         encoding="utf-8")
        _say(f"prefer {decision['prefer']}: {'; '.join(decision['reasons']) or 'all checks passed'}")


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
    state.set_status(revision_state.ROLLED_BACK, "operator rollback")
    revision_state.save(state, run_dir)
    log_event(run_dir, {"event": "rollback", "revision": args.revision, "rejected_paper": str(rejected),
                        "restored_from": state.paper_snapshot})
    _say(f"restored {state.paper_snapshot} -> {paper_dir}; revised paper kept at {rejected}")


def _dispatch(args) -> int | None:
    """Run the command; returns an exit code when the command has one (``evidence``)."""
    offline = {"review": _review, "rollback": _rollback, "abandon": _abandon, "audit": _audit,
               "evidence": _evidence, "retire": _retire}
    if args.command in offline:
        return offline[args.command](args)
    if args.env_file:
        load_env_file(Path(args.env_file))
    missing = [k for k in ("API_KEY", "API_BASE", "MODEL_NAME") if not os.environ.get(k)]
    if missing:
        raise PaperRunError(f"missing model settings: {', '.join(missing)}")
    if args.command == "run" and not args.topic:
        raise PaperRunError("--topic is required for run")
    try:
        parse_module_models(args.module_model)
        from jiuwenswarm.agents.harness.common.paper_pipeline.experiment_protocol import parse_gate_options

        tier_gates = parse_gate_options(args.tier_gate) if args.tier_gate else None
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
        delivery_policy=args.delivery_policy,
        missing_primary_rule=args.missing_primary_rule,
        tier_gates=tier_gates,
        evidence_rail=args.evidence_rail,
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
        code = _dispatch(args)
    except PaperRunError as exc:
        raise SystemExit(str(exc)) from exc
    if code is not None:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
