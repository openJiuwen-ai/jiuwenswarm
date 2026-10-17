"""Run or resume one agent-core paper pipeline task outside the AgentServer.

Fresh runs use agent-core's ``PaperTreeOrchestrator`` unchanged (the same path as JiuwenSwarm's
RSI "real" paper mode), except that an optional environment note becomes the create-mode initial
prompt.

Resume: the tree orchestrator refuses to resume (``tree_provider/provider.py``: "paper scenario
does not support resume"), but ``ManagerRuntime`` persists its state after every round. We resume
the manager directly (``arun(resume=True)``), keep appending to the task's ``model_calls.jsonl``
ledger, then run the same ``score_paper`` judge on the finished paper. The round that was in flight
when the run died is discarded and redone by the manager itself.

Every resume, counter reset and patch is appended to ``<run_dir>/resume_log.jsonl`` so resource
reports and deviations stay auditable.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class PaperRunError(RuntimeError):
    """A run that must not start or continue (bad options, open revision, missing manifest).

    ``jiuwenswarm-paper`` turns it into an exit with the message; library callers can catch it.
    """

# Used only when the manager already reached a terminal state and no follow-up was given:
# a follow-up is what clears the terminal flag.
DEFAULT_RESUME_FOLLOWUP = (
    "The previous failures were caused by an external problem (API outage, network or host "
    "environment), not by the research itself. It is fixed; continue from the last completed stage."
)
# Attempt counters an outage can burn through. rounds_used / survey_calls are never touched.
RETRY_COUNTERS = ("code_attempts", "design_revisions", "execution_attempts", "decision_retries", "reporting_attempts")


@dataclass
class PaperRunOptions:
    run_dir: Path
    topic: str
    task_id: str = "paper"
    config_path: str | None = None
    max_iterations: int = 1
    env_note: str = ""
    code_react_iterations: int | None = None
    # resume only
    run_id: str | None = None
    followup: str = ""
    reset_counters: list[str] = field(default_factory=list)
    skip_score: bool = False
    rebuild_latex: bool = True
    iclr_template: bool = True
    rigor: bool = True
    # experiment subprocesses use these credentials / this model instead of the pipeline's
    experiment_env: str | None = None
    experiment_model: str | None = None
    # pipeline spend guard (yuan, priced from model_calls.jsonl); None = no guard
    budget_soft: float | None = None
    budget_hard: float | None = None
    # account-balance floors (DeepSeek /user/balance): notice below balance_floor, abort below hard
    balance_floor: float | None = None
    balance_hard_floor: float | None = None
    # per-module model override, e.g. {"reporting": "deepseek-v4-pro"} (same API_BASE / API_KEY)
    module_models: dict[str, str] = field(default_factory=dict)
    # revise only (``jiuwenswarm-paper revise``): a resume that opens one review-driven revision cycle
    review_path: Path | None = None
    max_new_cells: int = 20
    replication_model: str | None = None
    revision_note: str = ""
    revision_rounds: int = 12
    # resume of an open revision: allow an answering-model setting to differ from the persisted one
    allow_setting_change: bool = False
    writing_only: bool = False  # revise only: answer the review without new experiments


def now_iso() -> str:
    """Local time with its UTC offset, e.g. ``2026-10-07T01:02:03+08:00``."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log_event(run_dir: Path, record: dict[str, Any]) -> None:
    record = {"ts": now_iso(), **record}
    with (run_dir / "resume_log.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_orchestrator(opts: PaperRunOptions):
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.tree_provider.orchestrator import (
        DEFAULT_CONFIG_PATH,
        PaperTreeOrchestrator,
    )

    env_note = opts.env_note.strip()

    class Orchestrator(PaperTreeOrchestrator):
        def _prepare_uploaded_paper_context(self) -> None:
            super()._prepare_uploaded_paper_context()
            if env_note and not self.artifact_path:
                self.initial_prompt = env_note

    return Orchestrator(
        task_id=opts.task_id,
        run_dir=str(opts.run_dir),
        max_iterations=opts.max_iterations,
        optimization_instruction=opts.topic,
        artifact_path=None,
        model=None,
        config_path=str(opts.config_path or DEFAULT_CONFIG_PATH),
    )


def find_run_id(run_dir: Path, task_id: str) -> str:
    candidates = sorted(p.parent.parent.name for p in run_dir.glob("experiments/*/manager/state.json"))
    if not candidates:
        raise FileNotFoundError(f"nothing to resume: no experiments/*/manager/state.json under {run_dir}")
    preferred = f"{task_id}-r1"
    if preferred in candidates:
        return preferred
    if len(candidates) > 1:
        raise ValueError(f"several runs to resume, pass run_id: {candidates}")
    return candidates[0]


async def fresh_run(opts: PaperRunOptions) -> None:
    orchestrator = build_orchestrator(opts)
    # start() returns immediately; the pipeline runs in the orchestrator's (private) ``_task``.
    await orchestrator.start()
    await getattr(orchestrator, "_task")


async def resume_run(opts: PaperRunOptions) -> None:
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.common.env import load_project_dotenv
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.common.workspace import (
        paper_scoring_dir,
        paper_tex_path,
        set_project_root,
    )
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.manager.artifacts import (
        save_state,
        try_load_state,
    )
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reflection.agent import ReflectionAgent
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.pipeline.manager import ManagerRuntime
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.tree_provider.judge import score_paper
    from openjiuwen.rsi.usage import ModelUsageObserver, set_usage_node

    unknown = [c for c in opts.reset_counters if c not in RETRY_COUNTERS]
    if unknown:
        raise ValueError(f"unknown counters {unknown}; choose from {RETRY_COUNTERS}")

    # Reuse the orchestrator's config preprocessing (search scope / proxy) without starting it.
    config = build_orchestrator(opts).config
    run_dir = opts.run_dir
    run_id = opts.run_id or find_run_id(run_dir, opts.task_id)
    set_project_root(str(run_dir))
    load_project_dotenv()

    state = try_load_state(run_id)
    if state is None:
        raise FileNotFoundError(f"no manager state for run_id={run_id!r}")
    from jiuwenswarm.agents.harness.common.paper_pipeline import revision_state
    from jiuwenswarm.agents.harness.common.paper_pipeline.revision import gate_start_round, install_revision_gate

    before = state.task_state.counters.model_dump()
    followup = opts.followup
    exp_dir = Path(paper_tex_path(run_id)).parent.parent  # experiments/<run_id>/
    revision = None
    if opts.review_path is not None:
        followup, revision = open_revision(run_dir, run_id, state, opts, Path(paper_tex_path(run_id)).parent)
        save_state(state)
    else:
        revision = revision_state.find_open(run_dir)
        if revision is not None:
            activate_revision(run_dir, revision)
            log_event(run_dir, {"event": "patch", "revision_reactivated": revision.index,
                                "start_round": revision.start_round, "max_new_cells": revision.max_new_cells,
                                "executed_new_cells": len(revision.executed_new_cells)})
        else:
            open_gate = gate_start_round(state)
            if open_gate is not None:  # legacy revision without revision.json: gate only, no settings
                install_revision_gate(open_gate)
                log_event(run_dir, {"event": "patch", "revision_gate_reinstalled": open_gate,
                                    "warning": "legacy revision: settings, frozen cells and evidence gate unavailable"})
    if state.terminal is not None and not followup:
        followup = DEFAULT_RESUME_FOLLOWUP
    for name in opts.reset_counters:
        setattr(state.task_state.counters, name, 0)
    if opts.reset_counters:
        save_state(state)
    log_event(run_dir, {
        "event": "resume",
        "run_id": run_id,
        "terminal_before": state.terminal.status if state.terminal is not None else None,
        "counters_before": before,
        "reset_counters": sorted(set(opts.reset_counters)),
        "followup": followup or None,
    })

    observer = ModelUsageObserver(None)
    async with observer.observe():
        # Same ledger as the fresh run; bind() refuses a mismatching task_id.
        await observer.bind({"task_id": opts.task_id}, run_dir)
        set_usage_node(run_id)
        reflection = None
        if (config.get("manager") or {}).get("modules", {}).get("reflection", False):
            reflection = ReflectionAgent(config)
        report = await ManagerRuntime(config, reflection=reflection).arun(
            topic=opts.topic, run_id=run_id, resume=True, followup=followup,
        )
        log_event(run_dir, {"event": "manager_done", "status": report.status,
                            "failure_reason": getattr(report, "failure_reason", None)})

        tex = Path(paper_tex_path(run_id))
        paper_check = post_process_paper(run_dir, tex.parent, opts) if tex.is_file() else None
        write_acceptance(run_dir, exp_dir / "results", pipeline_status=report.status, paper_check=paper_check,
                         revision=revision)
        if tex.is_file():
            if not opts.skip_score:
                set_usage_node(f"{run_id}-score")
                score = await score_paper(tex_path=str(tex), output_dir=str(paper_scoring_dir(run_id)), config=config)
                log_event(run_dir, {"event": "scored", "overall": score.overall, "breakdown": score.breakdown})
        else:
            log_event(run_dir, {"event": "no_paper", "expected": str(tex)})
        await observer.finish_pending()


def activate_revision(run_dir: Path, revision) -> None:
    """Install everything an open revision needs in this process, from its persisted state: the
    completion gate (with the evidence check), the frozen-cell execution guard and the replication
    model. Used by ``revise`` and by every plain ``resume`` of an unfinished revision.
    """
    from jiuwenswarm.agents.harness.common.paper_pipeline import revision_state
    from jiuwenswarm.agents.harness.common.paper_pipeline.revision import (
        install_replication_model,
        install_revision_gate,
    )

    install_revision_gate(revision.start_round, evidence=lambda: revision_state.evidence_status(run_dir, revision),
                          writing_only=revision.mode == "writing_only")
    manifest = revision_state.load_manifest(revision.folder(run_dir))
    if manifest is None:
        raise PaperRunError(f"revision {revision.index:02d} has no {revision_state.MANIFEST_FILE}; refusing to execute "
                            "without the frozen-cell manifest")
    revision_state.install_execution_guard(revision_state.ExecutionGuard(run_dir, revision, manifest))
    if revision.settings.get("replication_model"):
        install_replication_model(revision.settings["replication_model"])


def open_revision(run_dir: Path, run_id: str, state, opts: PaperRunOptions, paper_dir: Path) -> tuple[str, Any]:
    """Prepare ``state`` for one review-driven revision cycle; returns (follow-up, revision state).

    Snapshots the current paper, freezes every existing result (hash manifest), persists the
    revision's settings in ``revisions/revision_NN/revision.json``, stores the review and the brief,
    gives the manager fresh attempt counters and ``revision_rounds`` more rounds, and installs the
    host completion gate anchored at the current round. ``paper_dir`` is ``experiments/<run_id>/paper``.
    """
    exp_dir = paper_dir.parent
    from jiuwenswarm.agents.harness.common.paper_pipeline import revision_state
    from jiuwenswarm.agents.harness.common.paper_pipeline.revision import (
        build_brief,
        load_review,
        review_items,
        snapshot_paper,
    )

    review = load_review(opts.review_path)
    max_new_cells = 0 if opts.writing_only else opts.max_new_cells
    brief = build_brief(review, max_new_cells=max_new_cells, replication_model=opts.replication_model,
                        note=opts.revision_note, writing_only=opts.writing_only)
    if opts.followup.strip():
        brief += "\n## Additional operator follow-up\n\n" + opts.followup.strip() + "\n"
    index = revision_state.next_index(run_dir)
    task = state.task_state
    start_round = task.counters.rounds_used
    settings = revision_state.settings_of(opts)
    revision = revision_state.RevisionState(index=index, start_round=start_round, max_new_cells=max_new_cells,
                                            settings=settings, identity=revision_state.identity_of(settings),
                                            mode="writing_only" if opts.writing_only else "experiments",
                                            review_items=review_items(review))
    folder = revision.folder(run_dir)
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(opts.review_path, folder / f"review{Path(opts.review_path).suffix or '.json'}")
    (folder / "brief.md").write_text(brief, encoding="utf-8")
    snapshot = snapshot_paper(paper_dir)
    revision.paper_snapshot = str(snapshot) if snapshot else None
    manifest = revision_state.build_manifest(exp_dir / "results", exp_dir / "generated_code", folder)
    revision_state.save(revision, run_dir)

    for name in RETRY_COUNTERS:
        setattr(task.counters, name, 0)
    task.limits.max_rounds = max(task.limits.max_rounds, start_round + opts.revision_rounds)
    activate_revision(run_dir, revision)
    log_event(run_dir, {
        "event": "revise",
        "run_id": run_id,
        "revision": index,
        "review_source": review.source,
        "review_score": review.score,
        "start_round": start_round,
        "max_rounds": task.limits.max_rounds,
        "max_new_cells": max_new_cells,
        "mode": revision.mode,
        "review_items": len(revision.review_items),
        "replication_model": opts.replication_model,
        "paper_snapshot": str(snapshot) if snapshot else None,
        "brief": str(folder / "brief.md"),
        "identity": revision.identity,
        "frozen_cells": len(manifest["cells"]),
        "not_frozen": manifest["not_frozen"],
    })
    return brief, revision


def post_process_paper(run_dir: Path, paper_dir: Path, opts: PaperRunOptions):
    """Template conversion, table compaction, then the final PDF check (returned, never skipped:
    ``--no-latex-rebuild`` only stops the verified rebuild from replacing the paper's PDF).
    """
    # Both steps work on a copy and are optional: a crash (e.g. a LaTeX timeout) is logged and the
    # paper is left as it was, so the final check and acceptance.json still happen.
    if opts.iclr_template:
        from jiuwenswarm.agents.harness.common.paper_pipeline.iclr_template import convert_to_iclr

        try:
            converted = convert_to_iclr(paper_dir)
            log_event(run_dir, {"event": "iclr_template", "converted": converted.converted, "note": converted.note})
        except Exception as exc:  # noqa: BLE001 - optional step, must not skip the acceptance record
            log_event(run_dir, {"event": "iclr_template", "converted": False, "error": repr(exc)})
    if opts.rigor:
        from jiuwenswarm.agents.harness.common.paper_pipeline.results_table import compact_paper_tables

        try:
            compact = compact_paper_tables(paper_dir)
            log_event(run_dir, {"event": "compact_tables", "replaced": compact.replaced, "rebuilt": compact.rebuilt,
                                "note": compact.note})
        except Exception as exc:  # noqa: BLE001 - optional step, must not skip the acceptance record
            log_event(run_dir, {"event": "compact_tables", "replaced": 0, "error": repr(exc)})
    from jiuwenswarm.agents.harness.common.paper_pipeline.latex_check import final_check

    try:
        check = final_check(paper_dir, adopt=opts.rebuild_latex)
    except Exception as exc:  # noqa: BLE001 - a crashed check is "unverified", never "passed"
        from jiuwenswarm.agents.harness.common.paper_pipeline.latex_check import FinalCheck

        check = FinalCheck("unverified", [f"final check crashed: {exc!r}"])
    log_event(run_dir, {"event": "latex_final_check", "status": check.status, "reasons": check.reasons,
                        "exit_codes": check.exit_codes, "undefined_citations": check.undefined_citations,
                        "undefined_references": check.undefined_references, "adopted": check.adopted,
                        "pdf_sha256": check.pdf_sha256})
    return check


def manager_terminal_status(exp_dir: Path | None) -> str | None:
    """``terminal.status`` of the persisted manager state (the tree orchestrator returns none)."""
    path = Path(exp_dir) / "manager" / "state.json" if exp_dir else None
    try:
        terminal = json.loads(path.read_text(encoding="utf-8")).get("terminal") if path and path.is_file() else None
    except (OSError, ValueError):
        return None
    return terminal.get("status") if isinstance(terminal, dict) else None


def write_acceptance(run_dir: Path, results_dir: Path, *, pipeline_status: str | None, paper_check,
                     revision=None) -> dict[str, Any]:
    """``<run_dir>/acceptance.json``: "the pipeline finished" and "the deliverable is accepted" kept apart.

    Deliverable = pipeline complete + evidence audit passed (+ for a revision: evidence gate and
    frozen-result integrity) + final PDF check passed. An open revision is closed here as
    ``accepted`` or ``not_accepted`` once its manager run completes.
    """
    from jiuwenswarm.agents.harness.common.paper_pipeline import revision_state

    audit_path = Path(results_dir) / "audit.json"
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.is_file() else {}
    except (OSError, ValueError):
        audit = {"verdict": "unverified", "blocking": ["audit.json unreadable"]}
    evidence = {"verdict": audit.get("verdict", "unverified"), "blocking": audit.get("blocking", [])[:10]}
    paper = ({"status": paper_check.status, "reasons": paper_check.reasons} if paper_check is not None
             else {"status": "failed", "reasons": ["no paper"]})
    checks = {"pipeline_complete": pipeline_status == "complete", "evidence_passed": evidence["verdict"] == "passed",
              "paper_passed": paper["status"] == "passed"}
    record: dict[str, Any] = {"at": now_iso(), "pipeline_status": pipeline_status,
                              "evidence": evidence, "paper": paper}
    if revision is not None:
        from jiuwenswarm.agents.harness.common.paper_pipeline.revision import check_response

        ok, detail = revision_state.evidence_status(run_dir, revision)
        checks["revision_evidence_and_integrity"] = ok
        paper_dir = Path(results_dir).parent / "paper"
        response = check_response(revision.review_items, paper_dir, Path(results_dir))
        if revision.review_items:
            checks["review_items_resolved"] = not response["unresolved"]
        (revision.folder(run_dir) / "response_check.json").write_text(
            json.dumps(response, indent=2, ensure_ascii=False), encoding="utf-8")
        record["revision"] = {"index": revision.index, "mode": revision.mode, "evidence_detail": detail,
                              "unresolved_items": [u["id"] for u in response["unresolved"]],
                              # both papers stay available; `jiuwenswarm-paper rollback` restores the previous
                              "candidates": {"previous": revision.paper_snapshot, "revised": str(paper_dir)}}
    record["checks"] = checks
    record["deliverable"] = all(checks.values())
    (Path(run_dir) / "acceptance.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    if revision is not None and pipeline_status == "complete":
        revision.status = revision_state.ACCEPTED if record["deliverable"] else revision_state.NOT_ACCEPTED
        revision.acceptance["final"] = record
        revision_state.save(revision, run_dir)
        revision_state.install_execution_guard(None)
    log_event(run_dir, {"event": "acceptance", "deliverable": record["deliverable"], "checks": checks,
                        "revision_status": revision.status if revision is not None else None})
    return record


async def run(opts: PaperRunOptions, *, resume: bool) -> None:
    from jiuwenswarm.agents.harness.common.paper_pipeline.code_agent_budget import (
        install_code_agent_iteration_fix,
    )

    from jiuwenswarm.agents.harness.common.paper_pipeline import revision_state

    opts.run_dir = Path(opts.run_dir).resolve()
    opts.run_dir.mkdir(parents=True, exist_ok=True)
    # An open revision's settings are restored before anything below installs models from opts.
    open_revision_state = revision_state.find_open(opts.run_dir) if resume else None
    if opts.review_path is not None and open_revision_state is not None:
        raise PaperRunError(f"revision {open_revision_state.index:02d} of this run is still open; continue it with "
                            "`resume` (a second revise would open a second revision on top of it)")
    if open_revision_state is not None:
        overrides = revision_state.restore_settings(opts, open_revision_state, opts.run_dir,
                                                    allow_change=opts.allow_setting_change)
        log_event(opts.run_dir, {"event": "revision_restored", "revision": open_revision_state.index,
                                 "identity": open_revision_state.identity,
                                 "settings": open_revision_state.settings, "overrides": overrides})
    limit = install_code_agent_iteration_fix(opts.code_react_iterations)
    log_event(opts.run_dir, {"event": "patch", "code_react_iterations": limit})
    if opts.module_models:
        from jiuwenswarm.agents.harness.common.paper_pipeline.module_models import install_module_models

        log_event(opts.run_dir, {"event": "patch", "module_models": install_module_models(opts.module_models)})
    # Order matters: the experiment-model note is folded into the protocol text, and the budget
    # notice wraps the protocol-augmented manager prompt.
    if opts.experiment_env:
        from jiuwenswarm.agents.harness.common.paper_pipeline.experiment_model import (
            install_experiment_model,
            load_experiment_env,
        )

        override = load_experiment_env(Path(opts.experiment_env), opts.experiment_model)
        log_event(opts.run_dir, {"event": "patch",
                                 "experiment_model": install_experiment_model(override, Path(opts.experiment_env)),
                                 "experiment_env": str(opts.experiment_env),
                                 "experiment_api_base": override["API_BASE"]})
    from jiuwenswarm.agents.harness.common.paper_pipeline.revision import revision_open_in_log

    # a revise, or a plain resume of a revision that has not completed yet (legacy: from the log)
    legacy_open = resume and not revision_state.all_revisions(opts.run_dir) and revision_open_in_log(opts.run_dir)
    if opts.review_path is not None or open_revision_state is not None or legacy_open:
        if not opts.rigor:
            raise PaperRunError("revise needs the rigor protocol (drop --no-rigor)")
        from jiuwenswarm.agents.harness.common.paper_pipeline.revision import install_revision_protocol

        # before install_research_protocol: it reads the protocol texts this extends
        log_event(opts.run_dir, {"event": "patch", "revision_protocol": install_revision_protocol()})
    if opts.rigor:
        from jiuwenswarm.agents.harness.common.paper_pipeline.research_protocol import install_research_protocol

        log_event(opts.run_dir, {"event": "patch", "rigor_protocol": install_research_protocol()})
    reader = None
    balance_before = None
    guard_spec = budget_limits(opts)
    if guard_spec is not None:
        import os

        from jiuwenswarm.agents.harness.common.paper_pipeline.budget_guard import (
            BudgetGuard,
            deepseek_balance,
            install_budget_guard,
            install_execution_budget_check,
        )

        soft, hard, soft_floor, hard_floor = guard_spec
        if soft_floor is not None or hard_floor is not None:
            key, base = os.environ.get("API_KEY", ""), os.environ.get("API_BASE", "")
            if "deepseek.com" not in base:
                raise PaperRunError("--balance-floor / --balance-hard-floor need the pipeline on api.deepseek.com "
                                    "(balance endpoint)")

            def reader() -> float | None:
                return deepseek_balance(key, base.split("/v1")[0])
        guard = BudgetGuard(opts.run_dir / "model_calls.jsonl", soft, hard, reader, soft_floor, hard_floor)
        install_budget_guard(guard)
        install_execution_budget_check(guard)  # also between experiment variants, not only between rounds
        balance_before = reader() if reader else None
        log_event(opts.run_dir, {"event": "patch", "budget_soft_yuan": soft, "budget_hard_yuan": hard,
                                 "balance_floor": soft_floor, "balance_hard_floor": hard_floor,
                                 "balance_now": balance_before, "checked": "every manager round and every variant"})
    started = time.time()
    try:
        if resume:
            await resume_run(opts)
        else:
            await fresh_run(opts)
            paper = next(iter(sorted(opts.run_dir.glob("experiments/*/paper/main.tex"))), None)
            check = post_process_paper(opts.run_dir, paper.parent, opts) if paper is not None else None
            exp = paper.parent.parent if paper is not None else None
            write_acceptance(opts.run_dir, exp / "results" if exp else opts.run_dir / "results",
                             pipeline_status=manager_terminal_status(exp), paper_check=check)
    finally:
        # Accumulate wall-clock across the fresh run and every resume.
        elapsed = time.time() - started
        wall = opts.run_dir / "wallclock_seconds.txt"
        previous = float(wall.read_text()) if wall.is_file() else 0.0
        wall.write_text(f"{previous + elapsed:.1f}\n", encoding="utf-8")
        try:
            from jiuwenswarm.agents.harness.common.paper_pipeline.budget_guard import spend_report

            spend_report(opts.run_dir, balance_before=balance_before, balance_after=reader() if reader else None)
        except Exception as exc:  # noqa: BLE001 - accounting must not mask the run's own outcome
            log_event(opts.run_dir, {"event": "spend_report_failed", "error": repr(exc)})


def budget_limits(opts: PaperRunOptions) -> tuple[float, float, float | None, float | None] | None:
    """(soft yuan, hard yuan, soft balance floor, hard balance floor), or None when no limit is set.

    Any one of the four options installs the guard: ``--budget-hard`` alone is a hard stop without a
    notice, ``--balance-hard-floor`` alone an abort floor without a notice floor.
    """
    if all(v is None for v in (opts.budget_soft, opts.budget_hard, opts.balance_floor, opts.balance_hard_floor)):
        return None
    soft = opts.budget_soft if opts.budget_soft is not None else float("inf")
    hard = opts.budget_hard if opts.budget_hard is not None else soft * 1.3
    if opts.budget_soft is not None and hard < soft:
        raise PaperRunError(f"--budget-hard {hard} is below --budget {soft}")
    hard_floor = opts.balance_hard_floor if opts.balance_hard_floor is not None else (
        opts.balance_floor / 2 if opts.balance_floor is not None else None)
    if opts.balance_floor is not None and hard_floor is not None and hard_floor > opts.balance_floor:
        raise PaperRunError(f"--balance-hard-floor {hard_floor} is above --balance-floor {opts.balance_floor}")
    return soft, hard, opts.balance_floor, hard_floor
