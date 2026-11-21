"""Review-driven revision of a finished paper run (``jiuwenswarm-paper revise``).

A finished run already holds a paper, its experiment code and per-item results. A reviewer (the
Stanford Agentic Reviewer, or our own ``review`` panel) then says what is unsupported: a credited
mechanism that an ablation does not back, one dataset and one model, a metric confounded by a turn
cap, missing related work. The cheapest way to answer is not a new run but one more science loop on
the same run: add single-change ablations and a few replication settings to the existing
experiment, measure, and rewrite the paper around what the evidence supports.

This module turns that loop into a pipeline stage. ``revise`` resumes the run's manager with

* a **revision brief** built from the review (score, low dimensions, weaknesses, questions) plus a
  fixed playbook of what usually answers each kind of criticism;
* **revision addenda** appended to the design / code / reflection / manager / reporting prompts (the
  rigor protocol stays in place; the official prompts are never edited);
* a **host completion gate**: requirements that only the host can mark complete — a successful
  experiment execution, its evidence accepted (audit verdict passed, frozen results unchanged; see
  ``revision_state.evidence_status``) and then a successful reporting round, all after the revision
  started, so the manager cannot declare DONE on the strength of the pre-revision paper;
* a snapshot of the pre-revision paper and an audit record in ``resume_log.jsonl``; the frozen
  experiment manifest, persisted settings and host cell cap live in ``revision_state``.

The research question, frozen items, primary metric and every existing result are kept: the
revision adds evidence, it does not restart the study.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RESPONSE_FILE = "revision_response.json"
DISPOSITIONS = ("new_experiment", "narrowed_claim", "rewritten", "limitation")
DIMENSIONS = ("Claims_Support", "Experimental_Soundness", "Writing_Clarity", "Prior_Work_Context",
              "Question_Importance", "Originality", "Value_to_Community")
REQ_EXECUTE = "req-revision-execute"
REQ_EVIDENCE = "req-revision-evidence"
REQ_REPORT = "req-revision-report"


# --------------------------------------------------------------------------- review input
@dataclass
class ReviewInput:
    source: str  # "agentic_reviewer" | "panel"
    score: float | None
    dimensions: dict[str, float] = field(default_factory=dict)
    weaknesses: list[str] = field(default_factory=list)
    questions: str = ""
    assessment: str = ""


def _parse_triple_scores(text: str) -> dict[str, float]:
    """``- Claims_Support: [+1]  # ...`` lines -> {dimension: value}."""
    out: dict[str, float] = {}
    for line in (text or "").splitlines():
        for dim in DIMENSIONS:
            if dim in line and ":" in line:
                raw = line.split(":", 1)[1].split("#", 1)[0].strip().strip("[]").strip()
                try:
                    out[dim] = float(raw.replace("+", ""))
                except ValueError:
                    pass
    return out


def load_review(path: Path) -> ReviewInput:
    """Read an Agentic Reviewer result (``paperreview_fetch.py`` output) or a ``review`` panel summary."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "reviews" in data and "overall_mean" in data:  # our reviewer panel
        weaknesses: list[str] = []
        for review in data["reviews"]:
            if review.get("ok"):
                weaknesses += [f"[{review.get('model')}] {w}" for w in review.get("top_weaknesses") or []]
        return ReviewInput("panel", data.get("overall_mean"), dict(data.get("dimension_mean") or {}), weaknesses)
    sections = data.get("sections") or {}
    weakness_text = sections.get("weaknesses") or ""
    bullets = [line for line in weakness_text.splitlines() if line.strip().startswith("- ")]
    # the reviewer nests items under category headings ("- Experimental gaps ...\n  - item"): keep items
    nested = [line for line in bullets if line[:1].isspace()]
    weaknesses = [line.strip()[2:].strip() for line in (nested or bullets) if len(line.strip()) > 20]
    score = data.get("numerical_score")
    return ReviewInput(
        "agentic_reviewer",
        float(score) if score not in (None, "") else None,
        _parse_triple_scores(sections.get("binary_scores") or data.get("content") or ""),
        weaknesses or [weakness_text.strip()],
        (sections.get("questions") or "").strip(),
        (sections.get("assessment") or "").strip(),
    )


# --------------------------------------------------------------------------- brief
PLAYBOOK = """
How to answer each kind of criticism (use what applies; skip what does not):

1. **A credited mechanism is not supported** (e.g. "the ablation matches the full method"):
   enumerate the components of the proposed method (ranking signals, allocation rule, each
   fidelity/representation level, any extra model call) and add one **single-change ablation per
   component**: identical code path, exactly one component removed or replaced. Include the
   pair that separates the two most plausible sources of the gain. Then let the paper credit only
   what the ablations support, and say plainly which components had no detectable effect.
2. **One dataset / one model / short horizon**: add at most three **replication settings** of
   the decisive comparison only (best variant, strongest baseline, the key ablation, the
   unconstrained reference), e.g. a second public dataset from an accessible mirror, a second
   answering model (see the host setting below, if any), a longer turn/step cap. Each setting
   recalibrates its own budget tiers with the same rule from its own reference run, except a
   setting that changes only the cap, which reuses the tiers so that only the cap differs.
3. **The primary metric is confounded** (e.g. items scored zero because the agent ran out of turns):
   report unanswered/unfinished counts per variant and tier, and decompose paired wins and losses
   by whether the losing side left no answer (the host statistics now do this). State how much of
   each effect is "finishing" versus "answering".
4. **Borderline or post hoc evidence**: replicate the key contrast in a second setting instead of
   adding more contrasts; label every new variant as post hoc in the paper; report intervals and
   say what a null excludes (e.g. "every interval inside +/-0.05") rather than calling it zero.
5. **Extra computation as a confound** (e.g. the method makes an additional model call): compare
   against variants that make as many or more such calls without the claimed benefit, and report
   cumulative cost including those calls.
6. **Missing related work**: add the named works only after verifying they exist (title, authors,
   identifier from the survey or a search tool); position against them concretely; never invent a
   citation.
7. **Presentation**: every table must be readable (no tables wider than the page or set in tiny
   type); every number in the text must come from results; correct factual errors of the previous
   version explicitly.
"""


def build_brief(review: ReviewInput, *, max_new_cells: int, replication_model: str | None = None,
                note: str = "", writing_only: bool = False) -> str:
    task = ("Run **one writing-only revision** on this run: no new experiments; answer the review by "
            "narrowing claims, rewriting, and stating limitations, using only the existing results."
            if writing_only else
            "Run **one revision cycle** on this run: extend the existing experiment with the evidence the "
            "review asks for, execute it, reflect, and rewrite the paper.")
    lines = [
        "# Revision request (host follow-up)",
        "",
        "The paper of this run has been reviewed. " + task + " Keep the research question, the frozen "
        "item set, the primary metric, the existing variants and all existing results; never re-run or "
        "overwrite completed variants.",
        "",
        f"Review source: {review.source}; overall score: {review.score}.",
    ]
    low = [d for d in DIMENSIONS if d in review.dimensions and review.dimensions[d] <= 0]
    if low:
        lines.append("Dimensions not yet positive: " + ", ".join(f"{d} ({review.dimensions[d]:+g})" for d in low) + ".")
    lines += ["", "## Review items (stable ids)", ""] + [f"- `{i['id']}` {i['text']}" for i in review_items(review)]
    if review.questions:
        lines += ["", "## Reviewer questions (full text)", "", review.questions]
    if review.assessment:
        lines += ["", "## Reviewer assessment", "", review.assessment]
    lines += ["", "## Revision playbook", PLAYBOOK.strip(), "", "## Limits for this revision", ""]
    if writing_only:
        lines += ["- **No new variants**: the host refuses any execution of a new cell in this revision.",
                  "- Route: reflection (on the existing results) -> reporting. DONE needs a new successful "
                  "reporting round after this request, with every existing result unchanged."]
    else:
        lines += [f"- At most **{max_new_cells} new variants** (each variant = one method x tier x setting over "
                  "the full item set). Prefer few decisive cells over many marginal ones. The host enforces this: "
                  "every result that exists now is frozen (hashed) and only referenced, the execution step runs "
                  "only variants that are not frozen, and new variants beyond the cap are refused.",
                  "- DONE needs the new cells to pass the host evidence audit (AUDIT VERDICT PASSED) with frozen "
                  "results unchanged. A blocking audit error is either fixed or justified in "
                  "`audit_exceptions.json` in the experiment code directory: "
                  '`{"exceptions": [{"code", "variants", "reason", "affected_comparisons", "affected_claims"}]}`.',
                  "- Route: experiment_design (update) -> code_implementation -> experiment_execution -> "
                  "reflection -> reporting. Use topic_survey only if the review names related work that is "
                  "not in the survey yet.",
                  "- The host will not accept DONE until a new execution and a new paper exist after this request."]
    lines += [f"- Reporting writes `{RESPONSE_FILE}` next to main.tex: one entry per review item id, "
              '`{"items": [{"id", "disposition", "evidence", "paper_location", "summary"}]}` with disposition '
              f"one of {', '.join(DISPOSITIONS)}; `evidence` names the variants / statistics rows used "
              "(required for new_experiment); `paper_location` is a \\label or a section title that exists in "
              "the paper. The host checks it; unaddressed items keep the revision from being accepted."]
    if replication_model:
        lines.append(f"- A second answering model is available to the experiment code as the environment "
                     f"variable REPLICATION_MODEL_NAME={replication_model} (same API_KEY / API_BASE). Use it "
                     "only for a replication setting, never for the existing variants.")
    if note.strip():
        lines += ["", "## Operator note", "", note.strip()]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- prompt addenda
REVISION_DESIGN = """

## Revision mode (host addendum — binding while a revision request is open)

You are revising a finished study, not designing a new one. Output an **updated** design that:

- keeps the research question, primary metric, frozen item set, tier rule and every existing variant
  (with its name) unchanged, and marks them "existing — do not re-run";
- adds new variants only where the revision request asks for evidence: one single-change ablation per
  credited component, and at most three replication settings of the decisive comparison. For each new
  variant give: name, the one thing it changes relative to which existing variant, and which reviewer
  point it answers;
- names replication settings with a prefix in the variant name (e.g. `cap16__proposed_T1`,
  `ds2__proposed_T1`, `m2__proposed_T1`) so the host can run each with `--method <name>`; a setting
  with a new dataset or model calibrates its own tiers (same quantile rule) from its own reference run;
- states, for each new contrast, what result would support and what would refute the claim it tests,
  and that all new contrasts are post hoc;
- stays within the new-variant limit of the revision request.
"""

REVISION_CODE = """

## Revision mode (host addendum — binding while a revision request is open)

- Extend the existing experiment code; do not rewrite it. New variants are configurations of the
  existing classes (parameters such as quota shares, ranking weights, disabled components) whose
  **defaults reproduce the existing variants exactly**.
- Never touch the records or metrics files of existing variants. New variants write new files. The
  host hashes every existing result when the revision opens; a changed file stops its analysis.
- Keep existing variant names registered (the host references them instead of re-running them);
  bumping an implementation revision does not make the host re-run them.
- Replication settings are selected by the variant-name prefix and keep their own frozen tier file
  and results; a second dataset gets its own frozen item list with a content hash; if its gold
  answers come with aliases, score EM/F1 as the maximum over the answer and its aliases.
- Record per item whether the episode ended without an answer (empty prediction / cap reached), so
  the host can separate finishing from answering.
"""

REVISION_REFLECTION = """

## Revision mode (host addendum)

Judge the revision against the review: for each reviewer weakness, say whether the new results
answer it, contradict the previous paper, or leave it open. Separate effects that come from items
the losing variant left unanswered from effects among answered items (the host statistics give the
decomposition).
"""

REVISION_MANAGER = """

## Revision mode (host addendum)

An operator follow-up titled "Revision request" opens one revision cycle on this finished run. Follow
its route and limits. Do not redo the topic survey unless the request names missing related work; do
not re-run completed variants. DONE is accepted only after a new successful experiment execution and
a new successful reporting round, both after the request.
"""

REVISION_REPORTING = """

## Revision mode (host addendum — what the rewrite must do)

This is a rewrite of an existing paper after review, using the new results:

- Re-derive the title, abstract and contributions from what the evidence now supports; if the
  credited mechanism was not supported, say so in the abstract and credit what was.
- Keep the pre-registered results and label every new variant and setting as post hoc.
- Add a component-audit table (one row per removed component, paired difference, 95% CI, p) and a
  replication table (one row per setting); keep tables within the page width.
- Answer every reviewer weakness in the body (not in a rebuttal section): with new evidence, with a
  scoped claim, or as a stated limitation.
- Report finishing versus answering when unanswered items drive a difference; report cumulative cost
  including any extra model calls; state what nulls exclude (interval width) instead of "no effect".
- Correct factual errors of the previous version explicitly (one sentence each).
- Cite only works whose existence is verified in the survey or tool output.
- Write `revision_response.json` next to main.tex: for every review item id in the revision request,
  its disposition (new_experiment / narrowed_claim / rewritten / limitation), the evidence (variant
  names or statistics rows) and where in the paper it is answered (a \\label or section title).
"""


def install_revision_protocol() -> list[str]:
    """Append the revision addenda to the rigor protocol texts and module prompts. Idempotent.

    Must run before ``install_research_protocol`` for design/code/reflection/manager (which read
    the protocol constants at install time) — ``runner.run`` handles the order.
    """
    from jiuwenswarm.agents.harness.common.paper_pipeline import research_protocol as rp

    patched = []
    for name, addendum in (("DESIGN_PROTOCOL", REVISION_DESIGN), ("CODE_PROTOCOL", REVISION_CODE),
                           ("REFLECTION_PROTOCOL", REVISION_REFLECTION), ("MANAGER_PROTOCOL", REVISION_MANAGER),
                           ("REPORTING_PROTOCOL", REVISION_REPORTING)):
        current = getattr(rp, name)
        if "Revision mode" not in current:
            setattr(rp, name, current + addendum)
        patched.append(name)
    return patched


# --------------------------------------------------------------------------- completion gate
def _round_of(report: Any) -> int:
    for attr in ("round_index", "round"):
        value = getattr(report, attr, None)
        if isinstance(value, int):
            return value
    rid = str(getattr(report, "report_id", ""))  # e.g. "experiment_execution:12:1"
    parts = rid.split(":")
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else -1


def _succeeded_after(reports: list[Any], module: str, after_round: int) -> list[Any]:
    return [r for r in reports if r.module == module and r.outcome == "succeeded" and _round_of(r) > after_round]


def revision_status(reports: list[Any], start_round: int,
                    latest_execution_status: str) -> tuple[str | None, str | None]:
    """(execution report id, reporting report id) that satisfy the revision gate, or None each."""
    execution = _succeeded_after(reports, "experiment_execution", start_round)
    exec_id = execution[-1].report_id if execution and latest_execution_status == "completed" else None
    if exec_id is None:
        return None, None
    reporting = _succeeded_after(reports, "reporting", _round_of(execution[-1]))
    return exec_id, (reporting[-1].report_id if reporting else None)


def gate_rows(exec_id: str | None, report_id: str | None,
              evidence: tuple[bool, str] | None) -> dict[str, tuple[str, str | None, str]]:
    """requirement id -> (description, supporting report id or None, note).

    "The pipeline ran" (an execution, then a report) and "the evidence passed" are separate rows;
    with an evidence check, the report row also waits for it, so DONE needs both.
    """
    rows = {
        REQ_EXECUTE: ("Revision: a successful experiment execution after the revision request", exec_id, ""),
        REQ_REPORT: ("Revision: a successful reporting round after the revision execution", report_id, ""),
    }
    if evidence is not None:
        ok, detail = evidence
        support = exec_id if exec_id and ok else None
        note = detail if exec_id else "waiting for a revision execution"
        rows[REQ_EVIDENCE] = ("Revision: the new cells pass the host evidence audit and frozen results are "
                              "unchanged (fix the blocking audit errors or record a structured exception in "
                              "audit_exceptions.json)", support, note)
        if support is None:
            rows[REQ_REPORT] = (rows[REQ_REPORT][0], None, "blocked until the evidence requirement passes")
    return rows


def writing_only_rows(report_id: str | None,
                      evidence: tuple[bool, str] | None) -> dict[str, tuple[str, str | None, str]]:
    """Gate of a writing-only revision: a new report after the request, frozen results unchanged."""
    ok, detail = evidence if evidence is not None else (True, "")
    rows = {REQ_REPORT: ("Revision (writing only): a successful reporting round after the revision request",
                         report_id if ok else None, "" if ok else "blocked until the evidence requirement passes")}
    if evidence is not None:
        rows[REQ_EVIDENCE] = ("Revision (writing only): every existing result unchanged",
                              report_id if ok and report_id else None, detail)
    return rows


def install_revision_gate(start_round: int, evidence=None, writing_only: bool = False) -> None:
    """Wrap ``sync_host_requirements`` so host-owned requirements block DONE until satisfied.

    The wrapper re-derives their status every round (both ways), so a manager ``state_changes``
    entry cannot mark them complete early. ``evidence``: optional ``() -> (ok, detail)`` (see
    ``revision_state.evidence_status``); when given, DONE also needs the evidence to pass.
    """
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.manager.schemas import RequirementRecord
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.pipeline import manager as manager_mod
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.pipeline import transitions

    original = transitions.sync_host_requirements
    original = getattr(original, "__revision_wrapped__", original)

    def sync_host_requirements(state):
        original(state)
        task = state.task_state
        exec_id, report_id = revision_status(state.reports, start_round, task.latest_execution_status)
        if writing_only:  # no execution needed: any successful report after the request
            reports = _succeeded_after(state.reports, "reporting", start_round)
            exec_id, report_id = None, (reports[-1].report_id if reports else None)
        checked = None
        if evidence is not None:
            checked = (False, "")
            if exec_id or writing_only:
                try:
                    checked = evidence()
                except Exception as exc:  # noqa: BLE001 - a crashed check is "unverified", never a pass
                    checked = (False, f"evidence check crashed ({exc!r}); unverified")
        rows = {r.id: r for r in task.requirements}
        wanted = writing_only_rows(report_id, checked) if writing_only else gate_rows(exec_id, report_id, checked)
        for rid, (description, support, note) in wanted.items():
            row = rows.get(rid)
            if row is None:
                row = RequirementRecord(id=rid, description=description)
                task.requirements.append(row)
            row.status = "completed" if support else "pending"
            row.supporting_report_ids = [support] if support else []
            row.notes = f"host: revision gate (start_round={start_round})" + (f"; {note}" if note else "")

    sync_host_requirements.__revision_wrapped__ = original  # type: ignore[attr-defined]
    transitions.sync_host_requirements = sync_host_requirements
    manager_mod.sync_host_requirements = sync_host_requirements


def revision_open_in_log(run_dir: Path) -> bool:
    """True if ``resume_log.jsonl`` has a ``revise`` event not yet followed by a complete manager run.

    Legacy signal for revisions opened before ``revision_state`` existed; revisions with a
    ``revision.json`` use its status instead.
    """
    log = Path(run_dir) / "resume_log.jsonl"
    if not log.is_file():
        return False
    open_ = False
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "revise":
            open_ = True
        elif event.get("event") == "manager_done" and event.get("status") == "complete":
            open_ = False
    return open_


def gate_start_round(state) -> int | None:
    """Start round of an open revision gate persisted in ``state`` (None if there is none).

    Fallback for legacy revisions without ``revision.json`` (which stores ``start_round``): a
    plain ``resume`` of a revision that crashed must re-install the gate, otherwise the persisted
    pending requirement rows could never be completed.
    """
    for row in state.task_state.requirements:
        if row.id == REQ_EXECUTE and "start_round=" in row.notes:
            try:
                return int(row.notes.split("start_round=", 1)[1].split(")", 1)[0])
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------------- replication model
def install_replication_model(model: str) -> None:
    """Expose ``REPLICATION_MODEL_NAME`` to experiment subprocesses (same credentials). Idempotent."""
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.experiment_execution import agent as execution

    current = getattr(execution, "_variant_env")  # agent-core's private per-variant environment builder
    if getattr(current, "__replication_model__", None) == model:
        return

    def _variant_env(*args, **kwargs):
        env = current(*args, **kwargs)
        env["REPLICATION_MODEL_NAME"] = model
        return env

    _variant_env.__replication_model__ = model  # type: ignore[attr-defined]
    _variant_env.__wrapped__ = getattr(current, "__wrapped__", current)  # type: ignore[attr-defined]
    setattr(execution, "_variant_env", _variant_env)


# --------------------------------------------------------------------------- snapshot
def snapshot_paper(paper_dir: Path) -> Path | None:
    """Copy the current paper directory to ``paper_prev_<timestamp>`` next to it (sources, PDF, figures)."""
    paper_dir = Path(paper_dir)
    if not paper_dir.is_dir():
        return None
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d_%H%M%S")  # local time
    target = paper_dir.with_name(f"paper_prev_{stamp}")
    shutil.copytree(paper_dir, target, ignore=shutil.ignore_patterns("*.aux", "*.log", "*.fls", "*.fdb_latexmk"))
    return target


# --------------------------------------------------------------------------- review items + response
def _normalize(text: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in text).split())


def item_id(text: str) -> str:
    """Stable id of a review item: the same text gives the same id in every run and revision."""
    import hashlib

    return "R-" + hashlib.sha1(_normalize(text).encode("utf-8")).hexdigest()[:8]


def review_items(review: ReviewInput) -> list[dict[str, str]]:
    """Weaknesses and numbered questions, each with a stable id (duplicates collapsed)."""
    texts = [w for w in review.weaknesses if w and w.strip()]
    for line in (review.questions or "").splitlines():
        stripped = line.strip()
        if stripped[:2].rstrip(".").isdigit() or stripped.startswith("- "):
            texts.append(stripped.lstrip("-0123456789. ").strip())
    items, seen = [], set()
    for text in texts:
        rid = item_id(text)
        if rid not in seen and len(_normalize(text)) > 10:
            seen.add(rid)
            items.append({"id": rid, "text": text})
    return items


def _paper_text(paper_dir: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in sorted(Path(paper_dir).rglob("*.tex")))


def _has_metrics(results_dir: Path | None, variant: str) -> bool:
    return results_dir is not None and (Path(results_dir) / f"{variant}.metrics.json").is_file()


def check_response(items: list[dict[str, str]], paper_dir: Path, results_dir: Path | None) -> dict[str, Any]:
    """Check ``revision_response.json`` against the review items, the paper and the results.

    An item is resolved only when it has a valid disposition, a paper location that exists, and —
    for ``new_experiment`` — evidence naming variants that have a metrics file.
    """
    path = Path(paper_dir) / RESPONSE_FILE
    if not path.is_file():
        return {"file": str(path), "present": False, "resolved": [], "unresolved":
                [{"id": i["id"], "text": i["text"], "why": f"no {RESPONSE_FILE}"} for i in items]}
    try:
        entries = {e.get("id"): e for e in json.loads(path.read_text(encoding="utf-8")).get("items", [])
                   if isinstance(e, dict)}
    except (OSError, ValueError, AttributeError) as exc:
        return {"file": str(path), "present": True, "resolved": [], "unresolved":
                [{"id": i["id"], "text": i["text"], "why": f"unreadable ({exc})"} for i in items]}
    tex = _paper_text(paper_dir)
    resolved, unresolved = [], []
    for item in items:
        entry = entries.get(item["id"])
        why = ""
        if entry is None:
            why = "no entry"
        elif entry.get("disposition") not in DISPOSITIONS:
            why = f"disposition {entry.get('disposition')!r} not in {DISPOSITIONS}"
        else:
            location = str(entry.get("paper_location") or "").strip()
            label = location.removeprefix("\\label{").rstrip("}")
            if not location or (f"\\label{{{label}}}" not in tex and location not in tex):
                why = f"paper_location {location!r} not found in the paper"
            elif entry["disposition"] == "new_experiment":
                evidence = [str(e) for e in entry.get("evidence") or []]
                missing = [e for e in evidence if not _has_metrics(results_dir, e)]
                if not evidence:
                    why = "new_experiment without evidence"
                elif len(missing) == len(evidence):
                    why = f"evidence {missing} has no metrics file"
        record = {"id": item["id"], "text": item["text"], "disposition": (entry or {}).get("disposition")}
        (unresolved if why else resolved).append({**record, **({"why": why} if why else {})})
    extra = sorted(k for k in entries if k not in {i["id"] for i in items})
    return {"file": str(path), "present": True, "resolved": resolved, "unresolved": unresolved,
            "unknown_ids": extra}


# --------------------------------------------------------------------------- re-review + candidates
def compare_reviews(before: dict, after: dict, *, items: list[dict[str, str]] | None = None,
                    response: dict | None = None, deliverable: bool | None = None,
                    max_dimension_drop: float = 0.34) -> dict[str, Any]:
    """Decide between the pre- and post-revision paper from two review-panel summaries.

    Not by mean alone: the revised candidate is preferred only if it is deliverable, its overall mean
    did not drop, no dimension mean dropped by more than ``max_dimension_drop``, and every review item
    is resolved. Items whose wording comes back in the new reviews are listed as still raised.
    """
    reasons: list[str] = []
    old_mean, new_mean = before.get("overall_mean"), after.get("overall_mean")
    if old_mean is None or new_mean is None:
        reasons.append("a panel has no valid review; cannot compare")
    elif new_mean < old_mean:
        reasons.append(f"overall mean dropped {old_mean} -> {new_mean}")
    old_dims, new_dims = before.get("dimension_mean", {}), after.get("dimension_mean", {})
    drops = {}
    for d in DIMENSIONS:
        if d in old_dims and d in new_dims and new_dims[d] < old_dims[d] - max_dimension_drop:
            drops[d] = (old_dims[d], new_dims[d])
    reasons += [f"{d} dropped {a} -> {b}" for d, (a, b) in drops.items()]
    if deliverable is False:
        reasons.append("the revised paper is not deliverable (acceptance.json)")
    if response is not None and response.get("unresolved"):
        reasons.append(f"{len(response['unresolved'])} review items unresolved")
    new_weak = []
    for review in after.get("reviews", []):
        if review.get("ok"):
            new_weak += review.get("top_weaknesses") or []
    still = []
    for item in items or []:
        words = set(_normalize(item["text"]).split())
        for weakness in new_weak:
            other = set(_normalize(weakness).split())
            if words and other and len(words & other) / len(words | other) >= 0.35:
                still.append({"id": item["id"], "text": item["text"], "new_review": weakness})
                break
    return {"prefer": "revised" if not reasons else "previous", "reasons": reasons,
            "overall_mean": {"previous": old_mean, "revised": new_mean}, "dimension_drops": drops,
            "still_raised": still, "unresolved": (response or {}).get("unresolved", [])}


def rollback_paper(paper_dir: Path, snapshot: Path) -> Path:
    """Restore the pre-revision paper; the rejected revised paper is kept as ``paper_rejected_<ts>``."""
    paper_dir, snapshot = Path(paper_dir), Path(snapshot)
    if not snapshot.is_dir():
        raise FileNotFoundError(f"no paper snapshot at {snapshot}")
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d_%H%M%S")  # local time
    rejected = paper_dir.with_name(f"paper_rejected_{stamp}")
    if paper_dir.is_dir():
        shutil.move(str(paper_dir), str(rejected))
    shutil.copytree(snapshot, paper_dir)
    return rejected
