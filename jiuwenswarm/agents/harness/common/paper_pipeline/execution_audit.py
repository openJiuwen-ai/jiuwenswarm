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
CONSTRAINT_INACTIVE         error  a constrained variant's activation flag is below the host
                                   protocol's gate for its tier (frozen before execution), else 30%
OUTPUT_GATE_IGNORED         warn   the experiment output reports its own activation gates; only the
                                   protocol's count (the output reports the actual rate)
REUSED_EVIDENCE_CHANGED     error  (revision) a ledger version selected for reuse no longer matches
                                   its hash; it is not attached
ITEM_SET_MISMATCH           warn   variants were scored on different item sets
NO_ITEM_RECORDS             warn   no per-item records, no interval possible
UNDERPOWERED                warn   fewer paired items than ``MIN_ITEMS``
SMOKE_SIZED                 error  a "full" run scored a smoke-sized item set (<= 3)
DECLARED_DEVIATIONS         warn   the code reported deviations from the design
CONDITION_UNRESOLVED        error  a variant's model / budget / dataset cannot be established
PRIMARY_METRIC_UNMAPPED     error  the primary metric has no verified per-item field
FROZEN_RESULT_CHANGED       error  (revision) a frozen result file changed after it was frozen
CELL_CAP_REFUSED            error  (revision) new cells beyond the host cap were not executed
CELL_RERUN_LIMIT            error  (revision) a cell reached its execution limit and was not re-run
INVALID_EXCEPTION           warn   an ``audit_exceptions.json`` entry lacks the required fields
CELL_FAILED                 error  a planned cell did not complete with metrics
PRIMARY_COMPARISON_FAILED   error  a required primary comparison lost a cell (warn under the
                                   ``descriptive`` delivery policy)
PRIMARY_COMPARISON_UNVERIFIED error a required primary comparison could not be validly computed
                                   (budget / model / dataset mismatch, item ids, incomplete
                                   primary metric); warn under ``descriptive``
==========================  =====  ==========================================================

An error can be waived only by a structured entry in ``audit_exceptions.json`` (experiment code
directory or results directory): ``{"exceptions": [{"code", "variants", "reason",
"affected_comparisons", "affected_claims"}]}``. Integrity errors (non-finite, stale, changed frozen
results) and missing evidence (failed cells, unverified primary comparisons) cannot be waived: a
waiver written by the experiment code must not lower the host's bar.

The protocol the audit applies (primary metric, required cells and comparisons, missing-value rule,
delivery policy) is frozen by the host *before* the execution runs (``evidence.save_protocol``), and
every audit is recorded as an evidence manifest (``evidence.record_execution``).
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.paper_pipeline import evidence, experiment_protocol, rigor_stats
from jiuwenswarm.agents.harness.common.paper_pipeline.experiment_protocol import declared_metrics  # noqa: F401

MIN_ITEMS = 100
MIN_ACTIVE_RATE = evidence.DEFAULT_GATE
EXCEPTIONS_FILE = "audit_exceptions.json"
UNWAIVABLE = ("NON_FINITE_METRIC", "STALE_METRICS", "FROZEN_RESULT_CHANGED", "AUDIT_CRASHED", "CELL_FAILED",
              "PRIMARY_COMPARISON_FAILED", "PRIMARY_COMPARISON_UNVERIFIED", "REUSED_EVIDENCE_CHANGED")
_WAIVER_FIELDS = ("reason", "affected_comparisons", "affected_claims", "source")
_PRED_KEYS = ("predicted_answer", "predicted", "prediction", "pred", "answer", "output", "response")
# per-item "was the constraint under study active" flags the code protocol asks for
_ACTIVE_KEYS = ("constraint_active", "budget_binding", "budget_active", "binding", "compression_triggered")
_REFERENCE_HINTS = ("full", "unbounded", "unconstrained", "oracle", "no_compress", "reference")
# per-item fields that vary run to run without the policy doing anything different
_NOISY_TRACE = ("latency", "time", "duration", "seed", "timestamp")
_NAME_KEYS = ("method", "variant", "design_name", "variant_with_cap", "post_hoc")
# called as hook(results_dir, manifest) after each execution is recorded (e.g. PaperEvidenceRail)
AFTER_RECORD_HOOKS: list = []


def _is_reference(name: str, metrics: dict[str, Any]) -> bool:
    """An unconstrained reference arm is expected never to bind."""
    lowered = name.lower()
    explicit_unbudgeted = "budget_tokens" in metrics and metrics["budget_tokens"] in (None, 0, "none")
    return explicit_unbudgeted or any(h in lowered for h in _REFERENCE_HINTS)


@dataclass
class Finding:
    level: str  # error | warn
    code: str
    detail: str
    variants: list[str] = field(default_factory=list)
    waived_by: dict[str, Any] | None = None


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


def activation_gate(name: str, protocol: dict[str, Any] | None = None) -> tuple[float, str]:
    """Minimum activation rate for this variant, from the host protocol frozen before execution (the
    gate of its tier, else 30%). What the experiment output says its gate was is never read here.
    """
    return evidence.gate_for(protocol or {}, name)


def output_gates(metrics: dict[str, Any]) -> dict[str, Any] | None:
    """Gates an experiment output reports for itself (legacy ``tier_gates`` /
    ``tier_calibration.activation_gates``): reported, compared, never applied.
    """
    calibration = metrics.get("tier_calibration") if isinstance(metrics.get("tier_calibration"), dict) else {}
    for gates in (metrics.get("tier_gates"), calibration.get("activation_gates")):
        if isinstance(gates, dict) and gates:
            return gates
    return None


def _written_before(path: Path | None, started_at: float) -> bool:
    return path is not None and path.is_file() and path.stat().st_mtime < started_at - 1


def audit(
    variants: dict[str, dict[str, Any]],
    *,
    declared: list[str],
    started_at: float,
    metrics_paths: dict[str, Path],
    frozen: set[str] | frozenset[str] = frozenset(),
    protocol: dict[str, Any] | None = None,
) -> list[Finding]:
    """``frozen``: referenced cells (pre-revision frozen or reused ledger versions), exempt from the
    freshness check. ``protocol``: the host protocol whose activation gates apply.
    """
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
        reported = output_gates(metrics)
        if reported is not None:
            findings.append(Finding("warn", "OUTPUT_GATE_IGNORED",
                                    f"{name}: the output reports activation gates {json.dumps(reported)[:160]}; the "
                                    "host applies only the protocol's gate (frozen before execution)", [name]))
        if active_key is not None and not _is_reference(name, metrics):
            rate = sum(1 for r in records if r.get(active_key)) / len(records)
            gate, source = activation_gate(name, protocol)
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
            cell = (detail.split(":", 1)[0].strip() if code in ("FROZEN_RESULT_CHANGED", "REUSED_EVIDENCE_CHANGED")
                    else "")
            out.append(Finding(level.lower(), code, detail, [cell] if cell else []))
    return out


# --------------------------------------------------------------------------- wiring
def _read_design(plan) -> tuple[str, Path | None]:
    """(design text, its path); ("", None) when the plan names no readable design."""
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.common.workspace import project_root

    for raw in (plan.design_path,):
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = Path(project_root()) / raw
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace"), path.resolve()
    return "", None


def _results_dir(plan) -> Path:
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.common.workspace import results_dir

    return Path(results_dir(plan.run_id))


def code_fingerprint(code_dir: Path | None) -> str | None:
    """Hash of the experiment code files (as the revision freeze hashes them); None without code."""
    from jiuwenswarm.agents.harness.common.paper_pipeline.revision_state import code_hashes

    files = code_hashes(code_dir)
    return evidence.sha256_json(files)[:16] if files else None


def frozen_item_sets(frozen_metrics: dict[str, dict[str, Any]]) -> dict[str, dict]:
    """Per setting, the item set every frozen (pre-revision) cell of that setting was scored on — the
    revision's pre-declared items when the design declares none. A setting whose frozen cells
    disagree declares nothing.
    """
    by_setting: dict[str, list[tuple[str, list[str]]]] = {}
    for name, metrics in sorted(frozen_metrics.items()):
        ids, _ = rigor_stats.item_ids(rigor_stats.records_of(metrics))
        by_setting.setdefault(evidence.setting_of(name), []).append((name, ids or []))
    out = {}
    for setting, cells in by_setting.items():
        sets = {tuple(sorted(ids)) for _, ids in cells}
        if len(sets) == 1 and cells[0][1]:
            dataset = rigor_stats.parse_condition(cells[0][0], frozen_metrics[cells[0][0]]).dataset
            entry, _ = experiment_protocol.item_set_entry(cells[0][1], dataset=dataset,
                                                          source="revision frozen cells")
            if entry is not None:
                out[setting] = entry
    return out


def freeze_protocol(results: Path, *, design_text: str, plan_metrics: list[str], planned: list[str],
                    frozen: set[str] | frozenset[str] = frozenset(), revision: int | None = None,
                    design_path: Path | None = None, code_dir: Path | None = None,
                    answering: dict[str, Any] | None = None,
                    frozen_metrics: dict[str, dict[str, Any]] | None = None) -> dict:
    """Host step *before* an execution: freeze what the execution will be held to."""
    rule, policy, gates = evidence.resolve_options(results)
    body = evidence.build_protocol(
        declared=declared_metrics(design_text, plan_metrics), planned=planned, frozen=frozen,
        design_text=design_text, revision=revision, missing_primary_rule=rule, delivery_policy=policy,
        plan_metrics=plan_metrics, design_path=design_path, code_dir=code_dir, operator_gates=gates,
        previous=evidence.load_protocol(results), retirements=evidence.load_retirements(results),
        fallback_item_sets=frozen_item_sets(frozen_metrics or {}) if revision is not None else None,
        answering=answering)
    return evidence.save_protocol(results, body)


def _protocol_findings(protocol: dict, comparisons: list[dict], statuses: dict[str, dict]) -> list[Finding]:
    level = "error" if protocol.get("delivery_policy", "confirmatory") == "confirmatory" else "warn"
    out = []
    for entry in evidence.required_comparison_status(protocol, comparisons, statuses):
        if entry["status"] == "verified":
            continue
        code = "PRIMARY_COMPARISON_FAILED" if entry["status"] == "failed" else "PRIMARY_COMPARISON_UNVERIFIED"
        out.append(Finding(level, code, f"{entry['metric']}: {entry['a']} vs {entry['b']} — {entry['reason']}",
                           [entry["a"], entry["b"]]))
    return out


def post_process(inputs, output, started_at: float, *, frozen: set[str] | frozenset[str] = frozenset(),
                 host_lines: list[str] | None = None, results: Path | None = None,
                 design_text: str | None = None, protocol: dict | None = None, planned: list[str] | None = None,
                 executed: list[str] | None = None, execution_id: str | None = None,
                 revision: int | None = None, reused: dict[str, dict] | None = None,
                 referenced_specs: dict[str, str] | None = None, design_path: Path | None = None) -> str:
    """Annotate ``output.result`` in place, write the audit/statistics files and the evidence
    manifest; returns the verdict. ``protocol``: frozen by ``run_audited`` before the execution;
    a direct call freezes one now from the same inputs. ``frozen`` / ``reused``: cells referenced
    (pre-revision frozen / ledger versions), not executed by this run.
    """
    result = output.result
    plan = inputs.plan
    host_lines = list(host_lines or [])
    reused = dict(reused or {})
    referenced = set(frozen) | set(reused)
    results = results or _results_dir(plan)
    results.mkdir(parents=True, exist_ok=True)
    planned = list(planned if planned is not None else [v.name for v in result.variants])
    executed = list(executed if executed is not None else [n for n in planned if n not in referenced])
    code_dir = getattr(getattr(inputs, "implementation", None), "workspace_dir", None)
    if protocol is None:
        if design_text is None:
            design_text, design_path = _read_design(plan)
        protocol = freeze_protocol(results, design_text=design_text, plan_metrics=list(plan.metrics or []),
                                   planned=planned, frozen=frozen, revision=revision, design_path=design_path,
                                   code_dir=Path(code_dir) if code_dir else None)
    # calibration cells run before the gates are frozen and retired cells left the design: neither is evidence
    outside = set(protocol.get("calibration_cells") or []) | {r["cell"] for r in protocol.get("retired") or []}
    completed = {v.name: v for v in result.variants
                 if v.process_status == "completed" and v.metrics and v.name not in outside}
    planned = [n for n in planned if n not in outside]
    executed = [n for n in executed if n not in outside]
    declared = list(protocol.get("declared_metrics") or [])
    execution_id = execution_id or evidence.new_execution_id()
    findings = _revision_findings(host_lines)
    for name in executed:
        if name not in completed and name in planned:
            variant = next((v for v in result.variants if v.name == name), None)
            status = getattr(variant, "process_status", "not returned") if variant is not None else "not returned"
            findings.append(Finding("error", "CELL_FAILED", f"{name}: {status}, no metrics", [name]))
    stats = None
    if completed:
        metrics_paths = {name: results / f"{name}.metrics.json" for name in completed}
        payload = {name: v.metrics for name, v in completed.items()}
        findings += audit(payload, declared=declared, started_at=started_at, metrics_paths=metrics_paths,
                          frozen=referenced, protocol=protocol)
        # Designs list a dozen "decision metrics"; intervals and paired tests go to the first two
        # (the primary metric and, by convention, its cost counterpart) so the handoff stays readable.
        stats = rigor_stats.compute(payload, primary=declared[:2],
                                    missing_rule=protocol.get("missing_primary_rule", "refuse"),
                                    extra_pairs=[(r["a"], r["b"]) for r in protocol.get("review_comparisons") or []])
        rigor_stats.annotate(payload, stats)
        for finding in _stats_findings(stats):
            # only the primary metric's mapping blocks; a secondary (cost) metric's is a threat to note
            if finding.code == "PRIMARY_METRIC_UNMAPPED" and finding.detail.split(":")[0] != (declared[:1] or [""])[0]:
                finding.level = "warn"
            findings.append(finding)
        (results / "statistics.json").write_text(json.dumps(stats.to_dict(), indent=2), encoding="utf-8")
        (results / "statistics.md").write_text(rigor_stats.markdown(stats), encoding="utf-8")
    statuses = {n: {"status": "completed" if n in completed else ("failed" if n in executed else "not_run")}
                for n in set(planned) | set(completed)}
    for name, variant in completed.items():
        statuses[name]["item_coverage"] = evidence.item_coverage(protocol, name, variant.metrics)
    findings += _protocol_findings(protocol, stats.comparisons if stats else [], statuses)
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
        "protocol_id": protocol["protocol_id"],
        "execution_id": execution_id,
        "execution_started_at": started_at,
        "audited_at": time.time(),
        "declared_metrics": declared,
        "frozen_cells": sorted(frozen),
        "reused_cells": {n: e.get("version") for n, e in sorted(reused.items())},
        "findings": [asdict(f) for f in findings],
        "exceptions": exceptions,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    manifest = evidence.record_execution(
        results, protocol, execution_id=execution_id, started_at=started_at, revision=revision, planned=planned,
        executed=executed, frozen=frozen, completed={n: v.metrics for n, v in completed.items()},
        findings=[asdict(f) for f in findings], stats=stats, verdict=outcome, blocking=blocking, reused=reused,
        referenced_specs=referenced_specs, code_sha256=code_fingerprint(Path(code_dir) if code_dir else None))
    for hook in list(AFTER_RECORD_HOOKS):
        try:
            hook(results, manifest)
        except Exception:  # noqa: BLE001, S110 - observers must not change the audit
            pass

    lines = [line for line in host_lines if not re.match(r"REVISION (ERROR|WARN) ", line)]
    for f in findings:
        waived = f" [waived: {f.waived_by['reason'][:120]}]" if f.waived_by else ""
        lines.append(f"AUDIT {f.level.upper()} {f.code}: {f.detail}{waived}")
    if outcome == "passed" and not any(f.level == "error" for f in findings):
        lines.append("AUDIT OK: declared metrics present, outputs differ across variants, artifacts fresh")
    lines.append(f"AUDIT VERDICT {outcome.upper()}" + (f" ({len(blocking)} blocking)" if blocking else ""))
    for entry in manifest["required_comparisons"]:
        tail = (f"outcome {entry.get('outcome')} (mean diff {entry.get('mean_diff')}, 95% CI {entry.get('ci95')})"
                if entry["status"] == "verified" else entry.get("reason", ""))
        lines.append(f"EVIDENCE primary comparison {entry['a']} vs {entry['b']}: {entry['status'].upper()} — {tail}")
    for entry in manifest["review_comparisons"]:
        tail = (f"outcome {entry.get('outcome')}" if entry["status"] == "verified" else entry.get("reason", ""))
        lines.append(f"EVIDENCE review item {entry['item']} needs {entry['a']} vs {entry['b']}: "
                     f"{entry['status'].upper()} — {tail}")
    if outside:
        lines.append(f"EVIDENCE not evidence (calibration / retired cells): {', '.join(sorted(outside))}")
    lines.append(f"EVIDENCE protocol {protocol['protocol_id']} execution {execution_id} policy "
                 f"{protocol.get('delivery_policy')}: primary hypothesis "
                 + ("tested" if manifest["primary_hypothesis_verified"] else "NOT verified (no claim may rest on it)"))
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


def reaudit(results: Path, *, design_text: str, plan_metrics: list[str] | None = None,
            code_dir: Path | None = None, design_path: Path | None = None, revision: Any = None,
            run_dir: Path | None = None) -> str:
    """Audit the results already on disk (no execution): for runs made before the evidence contract,
    after an operator fix, or to re-establish the protocol after the design changed. Every completed
    cell is referenced (not re-run), so freshness is not judged; its compatibility with the new
    protocol is: a cell whose ledger version was recorded under another spec fails verification
    (CELL_SPEC_CHANGED), one the ledger does not know is reported as unchecked (legacy).

    ``revision`` (with ``run_dir``): the open ``RevisionState`` — its frozen cells stay the
    pre-registered ones and the record belongs to it.
    """
    from types import SimpleNamespace

    results = Path(results)
    variants = []
    for path in sorted(results.glob("*.metrics.json")):
        try:
            metrics = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(metrics, dict) and str(metrics.get("status", "")).lower() not in ("failed", "error",
                                                                                         "harness_failed"):
            variants.append(SimpleNamespace(name=path.name[: -len(".metrics.json")], metrics=metrics,
                                            process_status="completed", exit_code=0))
    names = [v.name for v in variants]
    pre_registered, answering, index = set(names), None, None
    if revision is not None:
        from jiuwenswarm.agents.harness.common.paper_pipeline import revision_state

        manifest = revision_state.load_manifest(revision.folder(Path(run_dir))) or {}
        pre_registered = {c["name"] for c in manifest.get("cells", [])} & set(names)
        answering, index = revision_state.answering_of(revision.settings), revision.index
    specs = {}
    for name in names:
        if name in pre_registered and revision is not None:
            specs[name] = evidence.SPEC_FROZEN
        else:
            specs[name] = evidence.ledger_spec_of(results, name, evidence.sha256_file(results / f"{name}.metrics.json"))
    protocol = freeze_protocol(results, design_text=design_text, plan_metrics=list(plan_metrics or []), planned=names,
                               frozen=pre_registered, revision=index, design_path=design_path, code_dir=code_dir,
                               answering=answering,
                               frozen_metrics={v.name: v.metrics for v in variants if v.name in pre_registered})
    inputs = SimpleNamespace(plan=SimpleNamespace(metrics=list(plan_metrics or [])),
                             implementation=SimpleNamespace(workspace_dir=str(code_dir) if code_dir else ""))
    output = SimpleNamespace(result=SimpleNamespace(variants=variants, notes="", status="completed"))
    return post_process(inputs, output, time.time(), frozen=set(names), results=results, protocol=protocol,
                        planned=names, executed=[], revision=index, referenced_specs=specs)


def write_unverified(inputs, exc: BaseException, results: Path | None = None) -> None:
    """The audit crashed: record ``unverified`` so nothing downstream reads it as a pass."""
    try:
        results = results or _results_dir(inputs.plan)
        results.mkdir(parents=True, exist_ok=True)
        (results / "audit.json").write_text(json.dumps({
            "verdict": "unverified", "blocking": [f"AUDIT_CRASHED: {exc!r}"], "audited_at": time.time(),
            "findings": [asdict(Finding("error", "AUDIT_CRASHED", repr(exc)))],
        }, indent=2), encoding="utf-8")
        evidence.record_unverified(results, repr(exc))
    except Exception:  # noqa: BLE001 - best effort; the note below still says unverified
        pass


def run_audited(original, agent, inputs):
    """The wrapped ``ExperimentExecutionAgent.run`` (module-level so it can be tested with fakes).

    Order: freeze the protocol (before anything runs), then let the revision guard decide what to
    execute and what to reference (frozen cells, reusable ledger versions), execute, audit.
    """
    from jiuwenswarm.agents.harness.common.paper_pipeline.revision_state import active_guard, answering_of

    started = time.time()
    guard = active_guard()
    plan = None
    planned = [v.name for v in inputs.implementation.variants]
    frozen = guard.frozen_names(planned) if guard is not None else set()
    revision = guard.state.index if guard is not None else None
    code_dir = getattr(inputs.implementation, "workspace_dir", None)
    protocol, protocol_error = None, None
    try:  # frozen before the execution runs: nothing the execution writes can lower the bar
        design_text, design_path = _read_design(inputs.plan)
        protocol = freeze_protocol(_results_dir(inputs.plan), design_text=design_text, design_path=design_path,
                                   plan_metrics=list(getattr(inputs.plan, "metrics", None) or []),
                                   planned=planned, frozen=frozen, revision=revision,
                                   code_dir=Path(code_dir) if code_dir else None,
                                   answering=answering_of(guard.state.settings) if guard is not None else None,
                                   frozen_metrics=guard.frozen_metrics(frozen) if guard is not None else None)
    except Exception as exc:  # noqa: BLE001 - recorded below as an unverified audit
        protocol_error = exc
    if guard is not None:
        inputs, plan = guard.filter_inputs(inputs, protocol)
    output = original(agent, inputs)
    host_lines: list[str] = []
    if guard is not None:
        host_lines = guard.merge(output, plan)
    try:
        if protocol_error is not None:
            raise RuntimeError(f"protocol could not be frozen before execution: {protocol_error!r}")
        post_process(inputs, output, started, frozen=set(plan.reference) if plan else set(), host_lines=host_lines,
                     protocol=protocol, planned=planned, executed=list(plan.execute) if plan else planned,
                     revision=revision, reused=dict(plan.attached_reuse) if plan else None)
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
