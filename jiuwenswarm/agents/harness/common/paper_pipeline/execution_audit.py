"""Post-execution audit + statistics for the agent-core paper pipeline.

Wraps ``ExperimentExecutionAgent.run``: after the official runner has executed every variant, the
host (not a model) checks that the run actually tested the design and computes paired statistics
(``rigor_stats``). Findings are stable codes, written three ways:

* ``results/audit.json`` + ``results/statistics.json`` / ``statistics.md`` — full detail on disk;
  ``audit.json`` carries a ``verdict``: ``passed`` (no unwaived error), ``failed`` or ``unverified``
  (the audit itself crashed — never read as a pass);
* ``AUDIT ERROR|WARN <code>: ...`` lines appended to ``ExperimentResult.notes`` — the manager and
  reflection see these in the execution handoff;
* flat scalars at the front of each variant's metrics (see ``rigor_stats.annotate``) — so the
  intervals reach reporting's ``results.json`` and pass its numeric-traceability lint.

During a revision the wrapper first asks ``revision_state.active_guard()`` which cells to execute:
frozen cells are referenced (their recorded metrics re-attached), only new cells run, at most the
revision's cap.

Audit codes (errors mean "the numbers do not test the design"; warnings are threats to validity):

==========================  =====  ==========================================================
MISSING_PRIMARY_METRIC      error  the primary (first declared) metric is absent from a variant
MISSING_DECLARED_METRIC     warn   a secondary declared metric is absent
NON_FINITE_METRIC           error  NaN / inf in a top-level metric
STALE_METRICS               error  metrics.json older than this execution (not rewritten);
                                   frozen revision cells are exempt (they are referenced)
IDENTICAL_OUTPUTS           error  identical predictions *and* identical per-item process traces —
                                   the intervention never took effect (wrong code path)
IDENTICAL_PREDICTIONS       warn   identical predictions although the per-item process differed —
                                   a genuine negative result is possible; do not credit a mechanism
IDENTICAL_ALIAS             warn   two names for the same configuration (same policy and budget)
CONSTRAINT_INACTIVE         error  a constrained variant's activation flag is below the frozen
                                   protocol's gate for its tier (``tier_gates``), else 30%
ITEM_SET_MISMATCH           warn   variants were scored on different item sets
NO_ITEM_RECORDS             warn   no per-item records, no interval possible
UNDERPOWERED                warn   fewer paired items than ``MIN_ITEMS``
SMOKE_SIZED                 error  a "full" run scored a smoke-sized item set (<= 3)
DECLARED_DEVIATIONS         warn   the code reported deviations from the design
CONDITION_UNRESOLVED        error  a variant's model / budget / dataset cannot be established
PRIMARY_METRIC_UNMAPPED     error  the primary metric has no verified per-item field
FROZEN_RESULT_CHANGED       error  (revision) a frozen result file changed after it was frozen
CELL_CAP_REFUSED            error  (revision) new cells beyond the host cap were not executed
INVALID_EXCEPTION           warn   an ``audit_exceptions.json`` entry lacks the required fields
==========================  =====  ==========================================================

An error can be waived only by a structured entry in ``audit_exceptions.json`` (experiment code
directory or results directory): ``{"exceptions": [{"code", "variants", "reason",
"affected_comparisons", "affected_claims"}]}``. Integrity errors (non-finite, stale, changed frozen
results) cannot be waived.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.paper_pipeline import rigor_stats

MIN_ITEMS = 100
MIN_ACTIVE_RATE = 0.30
EXCEPTIONS_FILE = "audit_exceptions.json"
UNWAIVABLE = ("NON_FINITE_METRIC", "STALE_METRICS", "FROZEN_RESULT_CHANGED", "AUDIT_CRASHED")
_WAIVER_FIELDS = ("reason", "affected_comparisons", "affected_claims", "source")
_PRED_KEYS = ("predicted_answer", "predicted", "prediction", "pred", "answer", "output", "response")
# per-item "was the constraint under study active" flags the code protocol asks for
_ACTIVE_KEYS = ("constraint_active", "budget_binding", "budget_active", "binding", "compression_triggered")
_REFERENCE_HINTS = ("full", "unbounded", "unconstrained", "oracle", "no_compress", "reference")
# per-item fields that vary run to run without the policy doing anything different
_NOISY_TRACE = ("latency", "time", "duration", "seed", "timestamp")
_NAME_KEYS = ("method", "variant", "design_name", "variant_with_cap", "post_hoc")


def _is_reference(name: str, metrics: dict[str, Any]) -> bool:
    """An unconstrained reference arm is expected never to bind."""
    lowered = name.lower()
    explicit_unbudgeted = "budget_tokens" in metrics and metrics["budget_tokens"] in (None, 0, "none")
    return explicit_unbudgeted or any(h in lowered for h in _REFERENCE_HINTS)


_METRIC_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?Metric\s+`?([A-Za-z][A-Za-z0-9_]*)`?", re.MULTILINE)


@dataclass
class Finding:
    level: str  # error | warn
    code: str
    detail: str
    variants: list[str] = field(default_factory=list)
    waived_by: dict[str, Any] | None = None


def declared_metrics(design_text: str, fallback: list[str]) -> list[str]:
    names = [m for m in _METRIC_LINE.findall(design_text or "")]
    names += [m for m in fallback if m]
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


def _prediction_signature(records: list[dict[str, Any]]) -> dict[str, str] | None:
    sig: dict[str, str] = {}
    for index, record in enumerate(records):
        key = next((k for k in _PRED_KEYS if k in record), None)
        if key is None:
            return None
        sig[rigor_stats.record_key(record, index)] = json.dumps(record[key], sort_keys=True, ensure_ascii=False)
    return sig


def _trace_signature(records: list[dict[str, Any]]) -> dict[str, str]:
    """Per item, the numeric process trace (tokens, calls, turns, flags) minus noisy fields."""
    out: dict[str, str] = {}
    for index, record in enumerate(records):
        trace = {k: v for k, v in record.items() if _is_trace_field(k, v)}
        out[rigor_stats.record_key(record, index)] = json.dumps(trace, sort_keys=True)
    return out


def _is_trace_field(key: str, value: Any) -> bool:
    if rigor_stats.as_number(value) is None or key == "budget_tokens":
        return False
    if key in rigor_stats.ID_KEYS or key in _NAME_KEYS:
        return False
    return not any(n in key.lower() for n in _NOISY_TRACE)


def activation_gate(name: str, metrics: dict[str, Any]) -> tuple[float, str]:
    """Minimum activation rate for this variant: the frozen protocol's gate for its tier if the
    experiment recorded one (``tier_gates`` or ``tier_calibration.activation_gates``), else 30%.
    """
    tier = metrics.get("tier") or rigor_stats.split_condition(rigor_stats.split_setting(name)[1])[1]
    calibration = metrics.get("tier_calibration") if isinstance(metrics.get("tier_calibration"), dict) else {}
    for source, gates in (("tier_gates", metrics.get("tier_gates")),
                          ("tier_calibration.activation_gates", calibration.get("activation_gates"))):
        if isinstance(gates, dict) and tier:
            lookup = {str(k).lower(): v for k, v in gates.items()}
            value = rigor_stats.as_number(lookup.get(str(tier).lower()))
            if value is not None:
                return value, f"frozen protocol {source}[{tier}]"
    return MIN_ACTIVE_RATE, "default"


def _written_before(path: Path | None, started_at: float) -> bool:
    return path is not None and path.is_file() and path.stat().st_mtime < started_at - 1


def audit(
    variants: dict[str, dict[str, Any]],
    *,
    declared: list[str],
    started_at: float,
    metrics_paths: dict[str, Path],
    frozen: set[str] | frozenset[str] = frozenset(),
) -> list[Finding]:
    findings: list[Finding] = []
    item_sets: dict[str, set[str]] = {}
    signatures: dict[str, dict[str, str]] = {}
    traces: dict[str, dict[str, str]] = {}
    primary = declared[0] if declared else None
    for name, metrics in variants.items():
        for key, value in metrics.items():
            if isinstance(value, float) and not math.isfinite(value):
                findings.append(Finding("error", "NON_FINITE_METRIC", f"{name}.{key} = {value}", [name]))
        if primary is not None and primary not in metrics:
            findings.append(Finding("error", "MISSING_PRIMARY_METRIC",
                                    f"{name}: primary metric `{primary}` is not in metrics.json", [name]))
        missing = [m for m in declared[1:] if m not in metrics]
        if missing:
            findings.append(Finding("warn", "MISSING_DECLARED_METRIC", f"{name}: missing declared metrics {missing}",
                                    [name]))
        path = metrics_paths.get(name)
        if name not in frozen and _written_before(path, started_at):
            findings.append(Finding("error", "STALE_METRICS", f"{name}: {path.name} was not rewritten by this run",
                                    [name]))
        records = rigor_stats.records_of(metrics)
        if not records:
            findings.append(Finding("warn", "NO_ITEM_RECORDS", f"{name}: no per-item records", [name]))
            continue
        ids = {rigor_stats.record_key(r, i) for i, r in enumerate(records)}
        item_sets[name] = ids
        if len(ids) <= 3:
            findings.append(Finding("error", "SMOKE_SIZED", f"{name}: only {len(ids)} items scored in a full run",
                                    [name]))
        sig = _prediction_signature(records)
        if sig:
            signatures[name] = sig
            traces[name] = _trace_signature(records)
        active_key = next((k for k in _ACTIVE_KEYS if any(k in r for r in records)), None)
        if active_key is not None and not _is_reference(name, metrics):
            rate = sum(1 for r in records if r.get(active_key)) / len(records)
            gate, source = activation_gate(name, metrics)
            if rate < gate:
                findings.append(Finding("error", "CONSTRAINT_INACTIVE",
                                        f"{name}: `{active_key}` true on only {rate:.1%} of items, below the "
                                        f"{gate:.0%} gate ({source}) — the budget/limit under study rarely bound, "
                                        "so this variant does not test the intervention as designed", [name]))
        deviations = metrics.get("deviations")
        if isinstance(deviations, list) and deviations:
            findings.append(Finding("warn", "DECLARED_DEVIATIONS", f"{name}: {'; '.join(map(str, deviations))[:300]}",
                                    [name]))

    if item_sets:
        sizes = {len(s) for s in item_sets.values()}
        common = set.intersection(*item_sets.values())
        if len(sizes) > 1 or len(common) < max(len(s) for s in item_sets.values()):
            findings.append(Finding("warn", "ITEM_SET_MISMATCH",
                                    f"item counts {dict((k, len(v)) for k, v in item_sets.items())}, "
                                    f"{len(common)} shared by all variants"))
        if len(common) < MIN_ITEMS:
            findings.append(Finding("warn", "UNDERPOWERED",
                                    f"{len(common)} paired items (< {MIN_ITEMS}); 95% CI on a rate is about "
                                    f"±{1.96 * 0.5 / math.sqrt(max(1, len(common))):.2f} wide"))

    names = sorted(signatures)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = set(signatures[a]) & set(signatures[b])
            if len(shared) < 10 or any(signatures[a][q] != signatures[b][q] for q in shared):
                continue
            findings.append(_identical(a, b, variants, traces, len(shared)))
    return findings


def _identical(a: str, b: str, variants: dict[str, dict[str, Any]], traces: dict[str, dict[str, str]],
               n: int) -> Finding:
    """Identical predictions: a duplicate alias, an intervention that never took effect, or a null."""
    ma, mb = variants[a], variants[b]
    same_policy = (ma.get("policy") or ma.get("method")) == (mb.get("policy") or mb.get("method"))
    same_budget = ma.get("budget_tokens") == mb.get("budget_tokens")
    if same_policy and same_budget and (ma.get("policy") or ma.get("method")) is not None:
        return Finding("warn", "IDENTICAL_ALIAS",
                       f"{a} and {b} are the same configuration (policy and budget) under two names and gave "
                       f"identical predictions on all {n} shared items — report it once", [a, b])
    shared = set(traces[a]) & set(traces[b])
    if all(traces[a][q] == traces[b][q] for q in shared):
        return Finding("error", "IDENTICAL_OUTPUTS",
                       f"{a} and {b} produced identical predictions and identical per-item process traces on all "
                       f"{n} shared items — the intervention never took effect (wrong code path or a non-binding "
                       "setting)", [a, b])
    return Finding("warn", "IDENTICAL_PREDICTIONS",
                   f"{a} and {b} produced identical predictions on all {n} shared items although their per-item "
                   "process differed — a genuine negative result is possible; do not credit a mechanism to the "
                   "difference", [a, b])


# --------------------------------------------------------------------------- exceptions + verdict
def load_exceptions(*folders: Path | None) -> tuple[list[dict[str, Any]], list[Finding]]:
    entries: list[dict[str, Any]] = []
    problems: list[Finding] = []
    for folder in folders:
        if folder is None or not (Path(folder) / EXCEPTIONS_FILE).is_file():
            continue
        path = Path(folder) / EXCEPTIONS_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(Finding("warn", "INVALID_EXCEPTION", f"{path}: unreadable ({exc})"))
            continue
        for entry in (data.get("exceptions") if isinstance(data, dict) else None) or []:
            why = _exception_problem(entry)
            if why:
                problems.append(Finding("warn", "INVALID_EXCEPTION", f"{path}: {why}: {json.dumps(entry)[:200]}"))
            else:
                entries.append({**entry, "source": str(path)})
    return entries, problems


def _exception_problem(entry: Any) -> str:
    if not isinstance(entry, dict):
        return "not an object"
    if not entry.get("code") or entry["code"] in UNWAIVABLE:
        return f"code {entry.get('code')!r} missing or not waivable"
    if not isinstance(entry.get("variants"), list) or not entry["variants"]:
        return "needs the affected `variants`"
    if len(str(entry.get("reason", "")).strip()) < 20:
        return "needs a substantive `reason`"
    comparisons, claims = entry.get("affected_comparisons"), entry.get("affected_claims")
    if not isinstance(comparisons, list) or not isinstance(claims, list) or not (comparisons or claims):
        return "needs `affected_comparisons` and `affected_claims` (lists, not both empty)"
    return ""


def apply_exceptions(findings: list[Finding], exceptions: list[dict[str, Any]]) -> None:
    for finding in findings:
        if finding.level != "error" or finding.code in UNWAIVABLE:
            continue
        for entry in exceptions:
            if entry["code"] == finding.code and finding.variants and set(finding.variants) <= set(entry["variants"]):
                finding.waived_by = {k: entry.get(k) for k in _WAIVER_FIELDS}
                break


def verdict(findings: list[Finding]) -> tuple[str, list[str]]:
    blocking = [f"{f.code}: {f.detail}" for f in findings if f.level == "error" and not f.waived_by]
    return ("failed" if blocking else "passed"), blocking


def _stats_findings(stats: rigor_stats.ExperimentStats) -> list[Finding]:
    out = []
    for line in stats.errors:
        code, _, detail = line.partition(" ")
        names = re.findall(r"[A-Za-z0-9_\-]+", detail.split(":", 1)[0]) if code == "CONDITION_UNRESOLVED" else []
        unmapped = re.findall(r"'([^']+)'", detail) if code == "PRIMARY_METRIC_UNMAPPED" else []
        out.append(Finding("error", code, detail, names or unmapped))
    return out


def _revision_findings(lines: list[str]) -> list[Finding]:
    out = []
    for line in lines:
        match = re.match(r"REVISION (ERROR|WARN) ([A-Z_]+):? ?(.*)", line)
        if match:
            level, code, detail = match.groups()
            cell = detail.split(":", 1)[0].strip() if code == "FROZEN_RESULT_CHANGED" else ""
            out.append(Finding(level.lower(), code, detail, [cell] if cell else []))
    return out


# --------------------------------------------------------------------------- wiring
def _read_design(plan) -> str:
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.common.workspace import project_root

    for raw in (plan.design_path,):
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = Path(project_root()) / raw
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    return ""


def _results_dir(plan) -> Path:
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.common.workspace import results_dir

    return Path(results_dir(plan.run_id))


def post_process(inputs, output, started_at: float, *, frozen: set[str] | frozenset[str] = frozenset(),
                 host_lines: list[str] | None = None, results: Path | None = None,
                 design_text: str | None = None) -> str:
    """Annotate ``output.result`` in place, write the audit/statistics files; returns the verdict."""
    result = output.result
    plan = inputs.plan
    host_lines = list(host_lines or [])
    completed = {v.name: v for v in result.variants if v.process_status == "completed" and v.metrics}
    results = results or _results_dir(plan)
    results.mkdir(parents=True, exist_ok=True)
    declared = declared_metrics(design_text if design_text is not None else _read_design(plan),
                                list(plan.metrics or []))
    findings = _revision_findings(host_lines)
    stats = None
    if completed:
        metrics_paths = {name: results / f"{name}.metrics.json" for name in completed}
        payload = {name: v.metrics for name, v in completed.items()}
        findings += audit(payload, declared=declared, started_at=started_at, metrics_paths=metrics_paths,
                          frozen=frozen)
        # Designs list a dozen "decision metrics"; intervals and paired tests go to the first two
        # (the primary metric and, by convention, its cost counterpart) so the handoff stays readable.
        stats = rigor_stats.compute(payload, primary=declared[:2])
        rigor_stats.annotate(payload, stats)
        for finding in _stats_findings(stats):
            # only the primary metric's mapping blocks; a secondary (cost) metric's is a threat to note
            if finding.code == "PRIMARY_METRIC_UNMAPPED" and finding.detail.split(":")[0] != (declared[:1] or [""])[0]:
                finding.level = "warn"
            findings.append(finding)
        (results / "statistics.json").write_text(json.dumps(stats.to_dict(), indent=2), encoding="utf-8")
        (results / "statistics.md").write_text(rigor_stats.markdown(stats), encoding="utf-8")
    code_dir = getattr(getattr(inputs, "implementation", None), "workspace_dir", None)
    exceptions, problems = load_exceptions(Path(code_dir) if code_dir else None, results)
    findings += problems
    apply_exceptions(findings, exceptions)
    outcome, blocking = verdict(findings)
    if not completed:
        outcome = "failed"
        blocking.append("NO_COMPLETED_VARIANTS: no variant completed with metrics")
    (results / "audit.json").write_text(json.dumps({
        "verdict": outcome,
        "blocking": blocking,
        "execution_started_at": started_at,
        "audited_at": time.time(),
        "declared_metrics": declared,
        "frozen_cells": sorted(frozen),
        "findings": [asdict(f) for f in findings],
        "exceptions": exceptions,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [line for line in host_lines if not re.match(r"REVISION (ERROR|WARN) ", line)]
    for f in findings:
        waived = f" [waived: {f.waived_by['reason'][:120]}]" if f.waived_by else ""
        lines.append(f"AUDIT {f.level.upper()} {f.code}: {f.detail}{waived}")
    if outcome == "passed" and not any(f.level == "error" for f in findings):
        lines.append("AUDIT OK: declared metrics present, outputs differ across variants, artifacts fresh")
    lines.append(f"AUDIT VERDICT {outcome.upper()}" + (f" ({len(blocking)} blocking)" if blocking else ""))
    for p in stats.pairs if stats else []:
        verdict_text = "CI excludes 0" if (p.ci_low > 0 or p.ci_high < 0) else "CI includes 0"
        lines.append(f"STATS [{p.role}] {p.metric} {p.a}-{p.b}: {p.mean_diff:+.4f} [{p.ci_low:+.4f}, "
                     f"{p.ci_high:+.4f}] p={p.p_value:.4f} p_holm={p.p_holm:.4f} (family {p.family}) "
                     f"n={p.n_paired} {p.test} ({verdict_text})")
    if stats and stats.pairs:
        lines.append("STATS RULES: claims of significance use p_holm within the family; only [confirmatory] rows "
                     "test the pre-registered hypotheses, [exploratory] rows are hypothesis-generating; "
                     f"{rigor_stats.SIGN_FLIP_TEST} is a Monte Carlo approximation, not an exact test; a CI inside "
                     f"+/-{rigor_stats.EQUIVALENCE_MARGIN:g} is a descriptive bound, not a formal equivalence test.")
    block = "\n".join(lines)
    result.notes = f"{result.notes}\n{block}".strip() if result.notes else block
    return outcome


def write_unverified(inputs, exc: BaseException, results: Path | None = None) -> None:
    """The audit crashed: record ``unverified`` so nothing downstream reads it as a pass."""
    try:
        results = results or _results_dir(inputs.plan)
        results.mkdir(parents=True, exist_ok=True)
        (results / "audit.json").write_text(json.dumps({
            "verdict": "unverified", "blocking": [f"AUDIT_CRASHED: {exc!r}"], "audited_at": time.time(),
            "findings": [asdict(Finding("error", "AUDIT_CRASHED", repr(exc)))],
        }, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 - best effort; the note below still says unverified
        pass


def run_audited(original, agent, inputs):
    """The wrapped ``ExperimentExecutionAgent.run`` (module-level so it can be tested with fakes)."""
    from jiuwenswarm.agents.harness.common.paper_pipeline.revision_state import active_guard

    started = time.time()
    guard = active_guard()
    plan = None
    if guard is not None:
        inputs, plan = guard.filter_inputs(inputs)
    output = original(agent, inputs)
    host_lines: list[str] = []
    if guard is not None:
        host_lines = guard.merge(output, plan)
    try:
        post_process(inputs, output, started, frozen=set(plan.reference) if plan else frozenset(),
                     host_lines=host_lines)
    except Exception as exc:  # the audit must never break an otherwise good execution
        write_unverified(inputs, exc)
        extra = "\n".join(host_lines)
        output.result.notes = (f"{output.result.notes}\n{extra}\nAUDIT ERROR AUDIT_CRASHED: {exc!r}\n"
                               "AUDIT VERDICT UNVERIFIED").strip()
    return output


def install_execution_audit() -> None:
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.experiment_execution import agent as module

    cls = module.ExperimentExecutionAgent
    original = cls.run
    if hasattr(original, "__wrapped__"):
        return

    def run(self, inputs):
        return run_audited(original, self, inputs)

    run.__wrapped__ = original  # type: ignore[attr-defined]
    cls.run = run
