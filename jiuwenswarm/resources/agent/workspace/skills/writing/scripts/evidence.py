"""Deterministic evidence boundary for the writing workflow.

This module deliberately does not infer completed experiments from prose.  It
records the supplied inputs, classifies their evidence mode, and creates the
small, replayable ledgers consumed by later writing stages.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any


COMPLETED_STATUSES = {"success", "completed", "complete", "finished"}
NON_RESULT_STATUSES = {"replan", "failed", "pending", "running", "cancelled"}


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def classify_evidence(m3: dict[str, Any], manifest: dict[str, Any] | None = None) -> tuple[str, list[str]]:
    """Return ``verified``, ``synthetic`` or ``unverified`` without guessing.

    Synthetic data is accepted only when the input or manifest says so
    explicitly.  A completed Module 3 result still needs an execution record;
    summaries and planned experiments are not sufficient evidence.
    """
    manifest = manifest or {}
    declared = str(manifest.get("evidence_mode") or m3.get("evidence_mode") or "").lower()
    if declared == "synthetic":
        return "synthetic", ["Input explicitly declares evidence_mode=synthetic."]
    status = str(manifest.get("module3_status") or m3.get("status") or "").lower()
    if status in NON_RESULT_STATUSES:
        return "unverified", [f"Module 3 status is {status.upper()}; it cannot support result claims."]
    runs = m3.get("experiment_runs") or (m3.get("experiment_results") or {}).get("experiment_runs") or []
    if status in COMPLETED_STATUSES and isinstance(runs, list) and runs:
        return "verified", []
    if status in COMPLETED_STATUSES:
        return "unverified", ["Module 3 is marked complete but has no traceable experiment_runs."]
    return "unverified", ["No explicit evidence_mode or completed Module 3 status with execution records."]


def build_inventory(paths: dict[str, str], m3: dict[str, Any], manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    mode, warnings = classify_evidence(m3, manifest)
    sources: dict[str, Any] = {}
    for name, raw_path in paths.items():
        path = Path(raw_path)
        sources[name] = {
            "path": str(path.resolve()), "exists": path.is_file(),
            "sha256": sha256_file(path) if path.is_file() else None,
        }
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "evidence_mode": mode,
        "module3_status": (manifest or {}).get("module3_status") or m3.get("status"),
        "declared_source_files": (manifest or {}).get("source_files") or {},
        "sources": sources,
        "warnings": warnings,
    }


def build_bibliography(m1: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge references/key papers without fabricating metadata."""
    candidates = list(m1.get("references") or []) + list(m1.get("key_papers") or [])
    entries: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(candidates):
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("id") or raw.get("citation_key") or "").strip()
        title = str(raw.get("title") or "").strip()
        fingerprint = (str(raw.get("doi") or raw.get("url") or title).casefold()).strip()
        if not key or not title:
            gaps.append({"index": index, "id": key or None, "reason": "missing citation key or title"})
            continue
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        # Preserve the supplied semantic evidence alongside citation metadata.
        # A title/key alone permits citation formatting but cannot establish a
        # related-work comparison or novelty claim.
        entry = {"id": key, "title": title, "authors": raw.get("authors") or raw.get("author"),
                 "year": raw.get("year"), "venue": raw.get("venue"), "doi": raw.get("doi"),
                 "url": raw.get("url"), "bibtex": raw.get("bibtex"),
                 "abstract": raw.get("abstract"), "summary": raw.get("summary"),
                 "contribution": raw.get("contribution"), "findings": raw.get("findings"),
                 "limitations": raw.get("limitations"), "method_key": raw.get("method_key"),
                 "relevance": raw.get("relevance")}
        missing = [field for field in ("authors", "year", "venue") if not entry[field]]
        if missing:
            gaps.append({"id": key, "reason": f"missing metadata: {', '.join(missing)}"})
        entries.append(entry)
    return entries, gaps


def assess_bibliography_quality(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Reject unmistakable fixture citations from a formal paper run.

    Missing optional metadata remains visible in ``bibliography_gaps.json``.
    This gate is deliberately narrower: it catches placeholder identities and
    non-resolving fixture URLs that must never appear in a released paper.
    """
    findings: list[dict[str, Any]] = []
    placeholder = re.compile(r"^(?:reference|paper|example|placeholder|test)(?:[\s_-]+[a-z0-9]+)?$", re.I)
    fake_person = re.compile(r"^(?:example|placeholder|test|author)(?:[\s_-]+[a-z0-9]+)?$", re.I)
    fake_venue = re.compile(r"^(?:example|placeholder|test)(?:[\s_-]+(?:venue|journal|conference|proceedings))?$", re.I)
    for entry in entries:
        citation_id = str(entry.get("id") or "").strip()
        title = str(entry.get("title") or "").strip()
        authors = entry.get("authors")
        author_values = authors if isinstance(authors, list) else [authors]
        venue = str(entry.get("venue") or "").strip()
        url = str(entry.get("url") or "").strip()
        reasons: list[str] = []
        if placeholder.fullmatch(title):
            reasons.append("placeholder title")
        if author_values and all(fake_person.fullmatch(str(value or "").strip()) for value in author_values):
            reasons.append("placeholder authors")
        if fake_venue.fullmatch(venue):
            reasons.append("placeholder venue")
        if url and ("example." in url.casefold() or url.casefold().endswith(".invalid")):
            reasons.append("fixture URL")
        if reasons:
            findings.append({"id": citation_id or None, "title": title or None, "reasons": reasons})
    return {
        "schema_version": 1,
        "status": "invalid" if findings else "valid",
        "finding_count": len(findings),
        "findings": findings,
    }


def _string_values(value: Any) -> list[str]:
    """Return non-empty strings from a scalar or a shallow list value."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _successful_runs(m3: dict[str, Any]) -> list[dict[str, Any]]:
    runs = m3.get("experiment_runs") or (m3.get("experiment_results") or {}).get("experiment_runs") or []
    return [run for run in runs if isinstance(run, dict) and run.get("success") is True]


def _metric_specs(value: Any) -> list[str]:
    """Read planned metric names from either the legacy string or object form."""
    items = value if isinstance(value, list) else [value]
    names: list[str] = []
    for item in items:
        if isinstance(item, str):
            if item.strip():
                names.append(item.strip())
        elif isinstance(item, dict):
            name = str(item.get("metric_name") or item.get("name") or "").strip()
            if name:
                names.append(name)
    return names


def _metric_available(required: str, runs: list[dict[str, Any]]) -> bool:
    """Check raw measurements and explicit mean/std/range aggregate requests.

    Module 3 stores per-run measurements (for example ``macro_f1``), while
    Module 2 may name its planned aggregate (``macro_f1_mean``).  An aggregate
    is available only when its raw metric is present; spread statistics also
    need at least two distinct successful runs for one method.
    """
    observed: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for index, run in enumerate(runs):
        method = str(run.get("method") or "").strip()
        run_id = str(run.get("run_id") or run.get("run_record_id") or index)
        for metric in run.get("metrics") or []:
            if not isinstance(metric, dict):
                continue
            name = str(metric.get("name") or "").strip()
            try:
                float(metric.get("value"))
            except (TypeError, ValueError):
                continue
            if name:
                observed[name][method].add(run_id)
    if required in observed:
        return True
    for suffix in ("_mean", "_std", "_range"):
        if not required.endswith(suffix):
            continue
        raw_name = required[: -len(suffix)]
        # Planning names duration aggregates as ``train_time_mean`` while the
        # execution contract records the base quantity with its SI unit, e.g.
        # ``train_time_seconds``.  Treat only this explicit unit spelling as
        # equivalent; other near-name matches remain unavailable.
        raw_candidates = [raw_name]
        if raw_name.endswith("_time"):
            raw_candidates.append(f"{raw_name}_seconds")
        observed_name = next((name for name in raw_candidates if name in observed), None)
        if observed_name is None:
            return False
        if suffix == "_mean":
            return True
        return any(len(run_ids) >= 2 for run_ids in observed[observed_name].values())
    return False


def _aggregate_metric_by_method(required: str, runs: list[dict[str, Any]]) -> dict[str, float]:
    """Compute a planned aggregate from traceable per-run measurements."""
    raw_name = required
    aggregate = "raw"
    for suffix, name in (("_mean", "mean"), ("_std", "std"), ("_range", "range")):
        if required.endswith(suffix):
            raw_name, aggregate = required[: -len(suffix)], name
            break
    raw_candidates = [raw_name]
    if raw_name.endswith("_time"):
        raw_candidates.append(f"{raw_name}_seconds")
    values: dict[str, list[float]] = defaultdict(list)
    for run in runs:
        method = str(run.get("method") or "").strip()
        if not method:
            continue
        for metric in run.get("metrics") or []:
            if not isinstance(metric, dict) or str(metric.get("name") or "").strip() not in raw_candidates:
                continue
            try:
                values[method].append(float(metric.get("value")))
            except (TypeError, ValueError):
                continue
    output: dict[str, float] = {}
    for method, series in values.items():
        if aggregate == "raw" and series:
            output[method] = series[-1]
        elif aggregate == "mean" and series:
            output[method] = fmean(series)
        elif aggregate == "std" and len(series) >= 2:
            output[method] = pstdev(series)
        elif aggregate == "range" and len(series) >= 2:
            output[method] = max(series) - min(series)
    return output


def _evaluate_metric_spec(spec: Any, runs: list[dict[str, Any]]) -> str:
    """Return a conservative claim status for one machine-readable metric test.

    Existing plans frequently describe the comparison in prose.  Those records
    remain ``inconclusive`` rather than being guessed from natural language.
    A plan can opt into a deterministic verdict with ``method``, ``operator``
    and ``threshold``, or with ``method``, ``comparison_method``, ``operator``
    and ``delta``.
    """
    if not isinstance(spec, dict):
        return "inconclusive"
    metric = str(spec.get("metric_name") or spec.get("name") or "").strip()
    method = str(spec.get("method") or spec.get("treatment") or "").strip()
    operator = str(spec.get("operator") or spec.get("comparator") or "").strip()
    if not metric or not method or operator not in {">", ">=", "<", "<="}:
        return "inconclusive"
    values = _aggregate_metric_by_method(metric, runs)
    if method not in values:
        return "untested"
    threshold = spec.get("threshold")
    comparison_method = str(spec.get("comparison_method") or spec.get("baseline") or "").strip()
    if comparison_method:
        if comparison_method not in values:
            return "untested"
        try:
            target = values[method] - values[comparison_method]
            threshold_value = float(spec.get("delta", 0.0))
        except (TypeError, ValueError):
            return "inconclusive"
    else:
        try:
            target = values[method]
            threshold_value = float(threshold)
        except (TypeError, ValueError):
            return "inconclusive"
    passed = {">": target > threshold_value, ">=": target >= threshold_value, "<": target < threshold_value, "<=": target <= threshold_value}[operator]
    return "supported" if passed else "not_supported"


def assess_paper_readiness(inputs: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    """Decide whether the supplied results can enter evidence-based writing.

    This boundary checks execution coverage and metric traceability before any
    result visual or prose is made.  It also evaluates an innovation claim only
    when its plan provides a machine-readable comparison predicate; prose-only
    predicates are reported as inconclusive rather than guessed.
    """
    findings: list[dict[str, Any]] = []

    def finding(code: str, owner: str, message: str, **context: Any) -> None:
        findings.append({"severity": "blocker", "code": code, "owner": owner, "message": message, **context})

    m1, m2, m3 = inputs["m1"], inputs["m2"], inputs["m3"]
    method_design = m2.get("method_design") or {}
    experiment_plan = m2.get("experiment_plan") or {}
    if inventory.get("evidence_mode") != "verified":
        finding("unverified_experiment_evidence", "experiment", "Module 3 does not provide verified successful execution records.")
        runs: list[dict[str, Any]] = []
    else:
        runs = _successful_runs(m3)
        if not runs:
            finding("no_successful_runs", "experiment", "Verified Module 3 input contains no successful experiment runs.")

    primary_experiments = _string_values(experiment_plan.get("primary_experiments"))
    if not primary_experiments:
        finding("primary_experiments_missing", "planning", "The plan does not identify the experiments that must support the paper's central results.")

    runs_by_experiment: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        experiment_id = str(run.get("experiment_id") or "").strip()
        if experiment_id:
            runs_by_experiment[experiment_id].append(run)

    planned_baselines = {
        str(item.get("name") or "").strip()
        for item in experiment_plan.get("baselines") or []
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    primary_methods = set(_string_values(
        experiment_plan.get("primary_methods")
        or method_design.get("primary_methods")
        or method_design.get("primary_method")
    ))
    matrix_methods: dict[str, set[str]] = defaultdict(set)
    for row in experiment_plan.get("experiment_matrix") or []:
        if isinstance(row, list) and len(row) >= 3:
            experiment_id, method = str(row[0] or "").strip(), str(row[2] or "").strip()
            if experiment_id and method:
                matrix_methods[experiment_id].add(method)
    scoped_baselines = {
        experiment_id: methods & planned_baselines
        for experiment_id, methods in matrix_methods.items()
    }
    scoped_primary_methods = {
        experiment_id: methods - planned_baselines
        for experiment_id, methods in matrix_methods.items()
    }
    if method_design.get("innovation_points") and not primary_methods and not any(scoped_primary_methods.values()):
        finding("primary_methods_missing", "planning", "The plan does not name the method or treatment whose central claims must be tested.")
    experiment_coverage: list[dict[str, Any]] = []
    for experiment_id in primary_experiments:
        scoped_runs = runs_by_experiment.get(experiment_id, [])
        methods = sorted({str(run.get("method") or "").strip() for run in scoped_runs if str(run.get("method") or "").strip()})
        metric_names = sorted({str(metric.get("name") or "").strip() for run in scoped_runs for metric in (run.get("metrics") or []) if isinstance(metric, dict) and str(metric.get("name") or "").strip()})
        required_baselines = scoped_baselines.get(experiment_id, planned_baselines)
        required_primary_methods = primary_methods or scoped_primary_methods.get(experiment_id, set())
        missing_baselines = sorted(required_baselines - set(methods))
        missing_primary_methods = sorted(required_primary_methods - set(methods))
        experiment_coverage.append({"experiment_id": experiment_id, "successful_run_count": len(scoped_runs), "methods": methods, "metric_names": metric_names, "missing_primary_methods": missing_primary_methods, "missing_planned_baselines": missing_baselines})
        if not scoped_runs:
            finding("primary_experiment_not_executed", "experiment", "A planned primary experiment has no successful run.", experiment_id=experiment_id)
            continue
        if not metric_names:
            finding("primary_experiment_has_no_numeric_metrics", "experiment", "A planned primary experiment has successful runs but no numeric metric records.", experiment_id=experiment_id)
        if required_baselines and len(methods) < 2:
            finding("comparison_missing", "experiment", "The plan requires baselines but this primary experiment contains fewer than two methods.", experiment_id=experiment_id, observed_methods=methods)
        if missing_baselines:
            finding("planned_baseline_not_executed", "experiment", "A baseline named in the plan is absent from this primary experiment.", experiment_id=experiment_id, missing_baselines=missing_baselines)
        if missing_primary_methods:
            finding("planned_primary_method_not_executed", "experiment", "A primary method named in the plan is absent from this primary experiment.", experiment_id=experiment_id, missing_primary_methods=missing_primary_methods)

    innovation_coverage: list[dict[str, Any]] = []
    for index, point in enumerate(method_design.get("innovation_points") or []):
        if not isinstance(point, dict):
            continue
        experiment_id = str(point.get("experiment_ref") or "").strip()
        required_metrics = _metric_specs(point.get("evidence_metric"))
        entry = {"index": index, "experiment_id": experiment_id or None, "required_metrics": required_metrics, "available": False, "missing_metrics": [], "claim_status": "untested"}
        if not experiment_id:
            finding("innovation_evidence_experiment_missing", "planning", "An innovation point does not name the experiment that supports it.", innovation_index=index)
        elif not runs_by_experiment.get(experiment_id):
            finding("innovation_evidence_not_executed", "experiment", "The experiment named by an innovation point has no successful run.", innovation_index=index, experiment_id=experiment_id)
        if not required_metrics:
            finding("innovation_evidence_metric_missing", "planning", "An innovation point does not name a traceable evidence metric.", innovation_index=index, experiment_id=experiment_id or None)
        else:
            scoped_runs = runs_by_experiment.get(experiment_id, [])
            missing_metrics = [metric for metric in required_metrics if not _metric_available(metric, scoped_runs)]
            entry["missing_metrics"] = missing_metrics
            entry["available"] = bool(experiment_id and not missing_metrics and scoped_runs)
            if missing_metrics:
                finding("innovation_evidence_metric_not_observed", "experiment", "A metric required by an innovation point is not traceable in its successful experiment runs.", innovation_index=index, experiment_id=experiment_id or None, missing_metrics=missing_metrics)
            elif scoped_runs:
                verdicts = [_evaluate_metric_spec(spec, scoped_runs) for spec in (point.get("evidence_metric") if isinstance(point.get("evidence_metric"), list) else [point.get("evidence_metric")])]
                if verdicts and all(verdict == "supported" for verdict in verdicts):
                    entry["claim_status"] = "supported"
                elif "not_supported" in verdicts:
                    entry["claim_status"] = "not_supported"
                else:
                    entry["claim_status"] = "inconclusive"
        innovation_coverage.append(entry)

    expected_hypotheses = {str(item.get("id") or "").strip() for item in m1.get("hypotheses") or [] if isinstance(item, dict) and str(item.get("id") or "").strip()}
    coverage_by_hypothesis = {str(item.get("hypothesis_id") or "").strip(): item for item in method_design.get("hypothesis_coverage") or [] if isinstance(item, dict) and str(item.get("hypothesis_id") or "").strip()}
    hypothesis_coverage: list[dict[str, Any]] = []
    for hypothesis_id in sorted(expected_hypotheses):
        coverage = coverage_by_hypothesis.get(hypothesis_id)
        if not coverage:
            finding("hypothesis_execution_link_missing", "planning", "A declared hypothesis has no planned experiment linkage.", hypothesis_id=hypothesis_id)
            hypothesis_coverage.append({"hypothesis_id": hypothesis_id, "available": False, "reason": "no_planning_link"})
            continue
        experiment_id = str(coverage.get("experiment_ref") or "").strip()
        declared_covered = coverage.get("covered") is not False
        available = bool(declared_covered and experiment_id and runs_by_experiment.get(experiment_id))
        hypothesis_coverage.append({"hypothesis_id": hypothesis_id, "experiment_id": experiment_id or None, "available": available})
        if not declared_covered or not experiment_id:
            finding("hypothesis_not_covered_by_plan", "planning", "A declared hypothesis is not marked as covered by a concrete experiment.", hypothesis_id=hypothesis_id)
        elif not runs_by_experiment.get(experiment_id):
            finding("hypothesis_support_experiment_not_executed", "experiment", "The experiment planned to test a hypothesis has no successful run.", hypothesis_id=hypothesis_id, experiment_id=experiment_id)

    owners = {item["owner"] for item in findings}
    recommended_actions = []
    if "planning" in owners:
        recommended_actions.append("replan_planning")
    if "experiment" in owners:
        recommended_actions.append("replan_experiment")
    ready = not findings
    claim_statuses = [item["claim_status"] for item in innovation_coverage]
    if ready and any(status in {"not_supported", "inconclusive"} for status in claim_statuses):
        recommended_actions.append("revise_claims")
    return {
        "schema_version": 1,
        "decision": ("ready_for_evidence_based_writing_with_claim_limits" if ready and "revise_claims" in recommended_actions else "ready_for_evidence_based_writing") if ready else "do_not_draft_results_paper",
        "status": "ready" if ready else "blocked",
        "scope": "execution coverage and metric traceability; claim-effect entailment is checked by the claim verifier.",
        "recommended_actions": recommended_actions or ["proceed_to_claim_verification"],
        "primary_experiment_coverage": experiment_coverage,
        "innovation_point_coverage": innovation_coverage,
        "hypothesis_coverage": hypothesis_coverage,
        "findings": findings,
    }


def build_execution_alignment(inputs: dict[str, Any]) -> dict[str, Any]:
    """Compare planned experiment scope with observed run records before writing.

    The comparison deliberately records only facts present in both module-two
    planning and module-three execution.  A writer therefore cannot turn a
    planned method, baseline, or experiment into an observed result merely
    because it appears in the plan.
    """
    plan = (inputs.get("m2") or {}).get("experiment_plan") or {}
    design = (inputs.get("m2") or {}).get("method_design") or {}
    m3 = inputs.get("m3") or {}
    raw_runs = m3.get("experiment_runs") or (m3.get("experiment_results") or {}).get("experiment_runs") or []
    runs = [item for item in raw_runs if isinstance(item, dict)]
    successful = [item for item in runs if item.get("success") is True]
    planned_experiments = _string_values(plan.get("primary_experiments"))
    planned_primary_methods = _string_values(plan.get("primary_methods") or design.get("primary_methods") or design.get("primary_method"))
    planned_baselines = sorted({str(item.get("name") or "").strip() for item in plan.get("baselines") or [] if isinstance(item, dict) and str(item.get("name") or "").strip()})
    planned_datasets = sorted({str(item.get("name") or "").strip() for item in plan.get("datasets") or [] if isinstance(item, dict) and str(item.get("name") or "").strip()})
    planned_metrics = _string_values(plan.get("metrics"))
    execution_config = (inputs.get("m2") or {}).get("execution_config") or {}
    planned_seeds = sorted({seed for seed in execution_config.get("seeds") or [] if isinstance(seed, int) and not isinstance(seed, bool)})
    matrix_datasets: dict[str, set[str]] = defaultdict(set)
    matrix_methods: dict[str, set[str]] = defaultdict(set)
    for row in plan.get("experiment_matrix") or []:
        if isinstance(row, list) and len(row) >= 2:
            experiment_id, dataset = (str(row[0] or "").strip(), str(row[1] or "").strip())
            if experiment_id and dataset:
                matrix_datasets[experiment_id].add(dataset)
            if len(row) >= 3:
                method = str(row[2] or "").strip()
                if experiment_id and method:
                    matrix_methods[experiment_id].add(method)
    planned_primary_by_experiment = {
        experiment_id: sorted(set(planned_primary_methods) or (methods - set(planned_baselines)))
        for experiment_id, methods in matrix_methods.items()
    }
    planned_baselines_by_experiment = {
        experiment_id: sorted(methods & set(planned_baselines))
        for experiment_id, methods in matrix_methods.items()
    }
    observed_experiments = sorted({str(run.get("experiment_id") or "").strip() for run in successful if str(run.get("experiment_id") or "").strip()})
    observed_methods_by_experiment = {
        experiment_id: sorted({str(run.get("method") or "").strip() for run in successful if str(run.get("experiment_id") or "").strip() == experiment_id and str(run.get("method") or "").strip()})
        for experiment_id in observed_experiments
    }
    observed_datasets_by_experiment = {
        experiment_id: sorted({str(run.get("dataset") or "").strip() for run in successful if str(run.get("experiment_id") or "").strip() == experiment_id and str(run.get("dataset") or "").strip()})
        for experiment_id in observed_experiments
    }
    observed_seeds_by_experiment = {
        experiment_id: sorted({run.get("seed") for run in successful if str(run.get("experiment_id") or "").strip() == experiment_id and isinstance(run.get("seed"), int) and not isinstance(run.get("seed"), bool)})
        for experiment_id in observed_experiments
    }
    observed_metrics_by_experiment: dict[str, set[str]] = defaultdict(set)
    for run in successful:
        experiment_id = str(run.get("experiment_id") or "").strip()
        for metric in run.get("metrics") or []:
            if isinstance(metric, dict) and str(metric.get("name") or "").strip():
                observed_metrics_by_experiment[experiment_id].add(str(metric["name"]).strip())
    for record in m3.get("metric_records") or []:
        if isinstance(record, dict):
            experiment_id, metric = str(record.get("experiment_id") or "").strip(), str(record.get("metric") or "").strip()
            if experiment_id and metric:
                observed_metrics_by_experiment[experiment_id].add(metric)
    differences: list[dict[str, Any]] = []
    for experiment_id in planned_experiments:
        observed_methods = set(observed_methods_by_experiment.get(experiment_id, []))
        if experiment_id not in observed_experiments:
            differences.append({"kind": "planned_experiment_not_executed", "experiment_id": experiment_id, "owner": "experiment"})
            continue
        for method in planned_primary_by_experiment.get(experiment_id, planned_primary_methods):
            if method not in observed_methods:
                differences.append({"kind": "planned_primary_method_not_executed", "experiment_id": experiment_id, "method": method, "owner": "experiment"})
        for method in planned_baselines_by_experiment.get(experiment_id, planned_baselines):
            if method not in observed_methods:
                differences.append({"kind": "planned_baseline_not_executed", "experiment_id": experiment_id, "method": method, "owner": "experiment"})
        expected_datasets = matrix_datasets.get(experiment_id) or set(planned_datasets)
        observed_datasets = set(observed_datasets_by_experiment.get(experiment_id, []))
        for dataset in sorted(expected_datasets - observed_datasets):
            differences.append({"kind": "planned_dataset_not_executed", "experiment_id": experiment_id, "dataset": dataset, "owner": "experiment"})
        if planned_datasets:
            for dataset in sorted(observed_datasets - set(planned_datasets)):
                differences.append({"kind": "executed_dataset_not_in_plan", "experiment_id": experiment_id, "dataset": dataset, "owner": "planning"})
        observed_metrics = observed_metrics_by_experiment.get(experiment_id, set())
        for metric in planned_metrics:
            if metric not in observed_metrics:
                differences.append({"kind": "planned_metric_not_observed", "experiment_id": experiment_id, "metric": metric, "owner": "experiment"})
        if planned_metrics:
            for metric in sorted(observed_metrics - set(planned_metrics)):
                differences.append({"kind": "observed_metric_not_in_plan", "experiment_id": experiment_id, "metric": metric, "owner": "planning"})
        if planned_seeds:
            observed_seeds = set(observed_seeds_by_experiment.get(experiment_id, []))
            for seed in sorted(set(planned_seeds) - observed_seeds):
                differences.append({"kind": "planned_seed_not_executed", "experiment_id": experiment_id, "seed": seed, "owner": "experiment"})
            for seed in sorted(observed_seeds - set(planned_seeds)):
                differences.append({"kind": "executed_seed_not_in_config", "experiment_id": experiment_id, "seed": seed, "owner": "planning"})
    for experiment_id in observed_experiments:
        if experiment_id not in planned_experiments:
            differences.append({"kind": "executed_experiment_not_in_primary_plan", "experiment_id": experiment_id, "owner": "planning"})
    return {
        "schema_version": 1,
        "planned": {
            "primary_experiments": planned_experiments,
            "primary_methods": planned_primary_methods,
            "primary_methods_by_experiment": planned_primary_by_experiment,
            "baselines": planned_baselines,
            "baselines_by_experiment": planned_baselines_by_experiment,
            "datasets": planned_datasets,
            "metrics": planned_metrics,
            "seeds": planned_seeds,
        },
        "observed": {"successful_run_count": len(successful), "experiment_ids": observed_experiments, "methods_by_experiment": observed_methods_by_experiment, "datasets_by_experiment": observed_datasets_by_experiment, "metrics_by_experiment": {key: sorted(value) for key, value in sorted(observed_metrics_by_experiment.items())}, "seeds_by_experiment": observed_seeds_by_experiment},
        "unverifiable_fields": ["dataset_version", "hyperparameters"] if not any(isinstance(run.get("config"), dict) or isinstance(run.get("hyperparameters"), dict) for run in successful) else ["dataset_version"],
        "differences": differences,
        "status": "aligned" if not differences else "mismatch",
    }


def build_execution_integrity(inputs: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    """Validate that run records are uniquely identified and numerically usable."""
    m3 = inputs.get("m3") or {}
    plan = (inputs.get("m2") or {}).get("experiment_plan") or {}
    plan_declares_datasets = any(
        isinstance(item, dict) and str(item.get("name") or "").strip()
        for item in plan.get("datasets") or []
    )
    raw_runs = m3.get("experiment_runs") or (m3.get("experiment_results") or {}).get("experiment_runs") or []
    findings: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    metric_units: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    successful_count = 0
    failed_count = 0
    for index, run in enumerate(raw_runs if isinstance(raw_runs, list) else []):
        if not isinstance(run, dict):
            findings.append({"code": "non_object_run_record", "index": index})
            continue
        run_id = str(run.get("run_id") or run.get("run_record_id") or "").strip()
        if not run_id:
            findings.append({"code": "missing_run_id", "index": index})
        elif run_id in seen_ids:
            findings.append({"code": "duplicate_run_id", "run_id": run_id})
        else:
            seen_ids.add(run_id)
        for field in ("experiment_id", "method"):
            if not str(run.get(field) or "").strip():
                findings.append({"code": f"missing_{field}", "run_id": run_id or None})
        if plan_declares_datasets and not str(run.get("dataset") or "").strip():
            findings.append({"code": "missing_dataset_for_planned_scope", "run_id": run_id or None})
        if run.get("success") is True:
            successful_count += 1
            metrics = run.get("metrics")
            if not isinstance(metrics, list) or not metrics:
                findings.append({"code": "successful_run_without_metrics", "run_id": run_id or None})
                continue
            for metric in metrics:
                if not isinstance(metric, dict) or not str(metric.get("name") or "").strip():
                    findings.append({"code": "invalid_metric_record", "run_id": run_id or None})
                    continue
                try:
                    valid = math.isfinite(float(metric.get("value")))
                except (TypeError, ValueError):
                    valid = False
                if not valid:
                    findings.append({"code": "nonfinite_metric_value", "run_id": run_id or None, "metric": metric.get("name")})
                unit = str(metric.get("unit") or "").strip()
                if unit:
                    metric_units[(str(run.get("experiment_id") or ""), str(run.get("method") or ""), str(metric.get("name") or ""))].add(unit)
        elif run.get("success") is False:
            failed_count += 1
    for (experiment_id, method, metric), units in metric_units.items():
        if len(units) > 1:
            findings.append({"code": "inconsistent_metric_unit", "experiment_id": experiment_id, "method": method, "metric": metric, "units": sorted(units)})

    implementation_manifest = m3.get("implementation_manifest")
    if isinstance(implementation_manifest, dict):
        implementations = implementation_manifest.get("implementations") or {}
        metric_specs = implementation_manifest.get("metrics") or {}
        if implementation_manifest.get("execution_approved") is not True and successful_count:
            findings.append({"code": "execution_not_approved_by_manifest"})
        observed_methods = sorted({
            str(run.get("method") or "").strip()
            for run in raw_runs if isinstance(run, dict) and run.get("success") is True and str(run.get("method") or "").strip()
        })
        for method in observed_methods:
            spec = implementations.get(method) if isinstance(implementations, dict) else None
            if not isinstance(spec, dict):
                findings.append({"code": "implementation_provenance_missing", "method": method})
            elif spec.get("ready") is not True or spec.get("smoke_test_passed") is not True:
                findings.append({"code": "implementation_not_verified", "method": method})
        observed_metric_names = sorted({
            str(metric.get("name") or "").strip()
            for run in raw_runs if isinstance(run, dict) and run.get("success") is True
            for metric in (run.get("metrics") or []) if isinstance(metric, dict) and str(metric.get("name") or "").strip()
        })
        for metric_name in observed_metric_names:
            spec = metric_specs.get(metric_name) if isinstance(metric_specs, dict) else None
            if not isinstance(spec, dict):
                findings.append({"code": "metric_definition_missing", "metric": metric_name})
                continue
            if spec.get("verified") is not True:
                findings.append({"code": "metric_implementation_not_verified", "metric": metric_name})
            direction = str(spec.get("direction") or "").upper()
            if any(token in metric_name.casefold() for token in ("time", "latency", "runtime")) and direction not in {"MINIMIZE", "MIN"}:
                findings.append({"code": "metric_direction_invalid", "metric": metric_name, "direction": direction or None})

    effective_records = m3.get("effective_execution_records")
    if isinstance(effective_records, list):
        effective_by_id = {
            str(item.get("run_record_id") or ""): item
            for item in effective_records if isinstance(item, dict)
        }
        for run in raw_runs if isinstance(raw_runs, list) else []:
            if not isinstance(run, dict) or run.get("success") is not True:
                continue
            run_id = str(run.get("run_record_id") or run.get("run_id") or "")
            effective = effective_by_id.get(run_id)
            if not effective or not effective.get("config_sha256") or not isinstance(effective.get("effective_config"), dict):
                findings.append({"code": "effective_run_config_missing", "run_id": run_id or None})

    orchestration_status = m3.get("stage3_orchestration_status")
    if isinstance(orchestration_status, dict) and str(orchestration_status.get("module_agent_outcome") or "").upper() == "REPLAN":
        findings.append({
            "code": "stage3_replan_overridden_without_audited_handoff",
            "executor": orchestration_status.get("executor"),
        })
    for name, declared in (inventory.get("declared_source_files") or {}).items():
        if not isinstance(declared, dict):
            findings.append({"code": "invalid_declared_source", "source": name})
            continue
        source_path = Path(str(declared.get("path") or ""))
        expected_hash = str(declared.get("sha256") or "").strip().lower()
        if not source_path.is_file():
            findings.append({"code": "declared_source_missing", "source": name})
        elif not expected_hash:
            findings.append({"code": "declared_source_hash_missing", "source": name})
        elif sha256_file(source_path).lower() != expected_hash:
            findings.append({"code": "declared_source_hash_mismatch", "source": name})
    return {"schema_version": 1, "status": "valid" if not findings else "invalid", "finding_count": len(findings), "run_outcomes": {"successful": successful_count, "failed": failed_count, "total": successful_count + failed_count}, "findings": findings}


def _normalise_metric_phrase(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _reconcile_claim_evidence_scope(
    claims: list[dict[str, Any]], aggregates: list[dict[str, Any]]
) -> None:
    """Fail safely when an upstream prose verdict cites another experiment.

    Module 3 is authoritative for observed measurements, but generated
    hypothesis summaries can occasionally attach a value from a derived or
    auxiliary experiment to the planned parent experiment.  Resolve only an
    unambiguous metric-name + numeric-value match.  The verdict itself is kept;
    the unsupported prose rationale is replaced by a bounded traceability note.
    """
    numeric_fields = ("mean", "minimum", "maximum", "population_stddev")
    for claim in claims:
        reason = str(claim.get("verdict_reason") or "").strip()
        if not reason:
            continue
        reason_phrase = _normalise_metric_phrase(reason)
        reason_numbers = [float(value) for value in re.findall(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?", reason)]
        if not reason_numbers:
            continue
        matched_experiments: set[str] = set()
        for aggregate in aggregates:
            metric_phrase = _normalise_metric_phrase(aggregate.get("metric"))
            if not metric_phrase or metric_phrase not in reason_phrase:
                continue
            values = [aggregate.get(field) for field in numeric_fields]
            for observed in values:
                if not isinstance(observed, (int, float)) or isinstance(observed, bool):
                    continue
                if any(math.isclose(float(observed), number, rel_tol=1e-5, abs_tol=5e-5) for number in reason_numbers):
                    experiment_id = str(aggregate.get("experiment_id") or "").strip()
                    if experiment_id:
                        matched_experiments.add(experiment_id)
                    break
        declared = {
            str(value).strip()
            for value in claim.get("evidence_experiment_ids") or []
            if str(value).strip()
        }
        if str(claim.get("experiment_id") or "").strip():
            declared.add(str(claim["experiment_id"]).strip())
        if not matched_experiments or matched_experiments <= declared:
            continue
        original_experiment = str(claim.get("experiment_id") or "").strip()
        if original_experiment:
            claim["planned_experiment_id"] = original_experiment
        claim["evidence_experiment_ids"] = sorted(matched_experiments)
        if len(matched_experiments) == 1:
            claim["experiment_id"] = next(iter(matched_experiments))
        else:
            claim.pop("experiment_id", None)
        claim["verdict_reason"] = (
            "The upstream verdict is retained, but its numerical rationale was "
            "withheld because the cited measurement resolves to a different "
            "experiment scope. Use the registered aggregates for quantitative statements."
        )
        claim["evidence_consistency"] = "normalised_cross_scope_rationale"


def _bind_claim_evidence_identities(
    claims: list[dict[str, Any]], aggregates: list[dict[str, Any]]
) -> None:
    """Attach replayable aggregate identities to every evaluated claim.

    Equal floating-point values are common across baselines, ablations, and
    copied configurations.  A value alone therefore cannot identify evidence.
    The binding below is restricted to the experiment scope declared by the
    upstream hypothesis evaluation.  A comparison is recorded only when one
    and only one ordered aggregate pair in that scope reproduces the reported
    delta for the named metric; otherwise writers must report the upstream
    verdict/delta without inventing comparison-arm identities.
    """
    for claim in claims:
        if str(claim.get("evaluation_status") or "") != "evaluated":
            continue
        experiment_ids = {
            str(value).strip() for value in claim.get("evidence_experiment_ids") or []
            if str(value).strip()
        }
        experiment_id = str(claim.get("experiment_id") or "").strip()
        if experiment_id:
            experiment_ids.add(experiment_id)
        scoped = [
            item for item in aggregates
            if str(item.get("experiment_id") or "").strip() in experiment_ids
        ]
        binding: dict[str, Any] = {
            "identity_fields": ["aggregate_id", "experiment_id", "method", "metric"],
            "experiment_ids": sorted(experiment_ids),
            "aggregate_ids": [str(item["id"]) for item in scoped if item.get("id")],
            "comparison_status": "unresolved",
            "comparison": None,
        }
        reason = str(claim.get("verdict_reason") or "")
        reason_phrase = _normalise_metric_phrase(reason)
        signed = [
            float(value) for value in re.findall(r"(?<![A-Za-z0-9.])([+-]\d+(?:\.\d+)?)", reason)
        ]
        metric_names = sorted({
            str(item.get("metric") or "") for item in scoped
            if str(item.get("metric") or "")
            and _normalise_metric_phrase(item.get("metric")) in reason_phrase
        })
        if signed and len(metric_names) == 1:
            delta = signed[0]
            metric = metric_names[0]
            metric_rows = [item for item in scoped if str(item.get("metric") or "") == metric]
            candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for left in metric_rows:
                for right in metric_rows:
                    if left is right or left.get("experiment_id") != right.get("experiment_id"):
                        continue
                    if math.isclose(
                        float(left.get("mean")), float(right.get("mean")) + delta,
                        rel_tol=1e-4, abs_tol=5e-5,
                    ):
                        candidates.append((left, right))
            unique = {
                (str(left.get("id")), str(right.get("id"))): (left, right)
                for left, right in candidates
            }
            if len(unique) == 1:
                left, right = next(iter(unique.values()))
                def identity(item: dict[str, Any]) -> dict[str, Any]:
                    return {
                        "aggregate_id": item.get("id"),
                        "experiment_id": item.get("experiment_id"),
                        "method": item.get("method"),
                        "metric": item.get("metric"),
                        "mean": item.get("mean"),
                    }
                binding["comparison_status"] = "resolved"
                binding["comparison"] = {
                    "metric": metric,
                    "observed_difference": delta,
                    "minuend": identity(left),
                    "subtrahend": identity(right),
                }
            elif len(unique) > 1:
                binding["comparison_status"] = "ambiguous"
        claim["canonical_evidence"] = binding


def write_evidence_ledgers(output_dir: str | Path, inputs: dict[str, Any], inventory: dict[str, Any]) -> dict[str, str]:
    """Persist initial, explicit evidence ledgers before any LLM is called."""
    root = Path(output_dir)
    evidence_dir, stages_dir = root / "evidence", root / "stages"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stages_dir.mkdir(parents=True, exist_ok=True)
    bibliography, gaps = build_bibliography(inputs["m1"])
    paper_readiness = assess_paper_readiness(inputs, inventory)
    bibliography_quality = assess_bibliography_quality(bibliography)
    if bibliography_quality["status"] != "valid":
        paper_readiness["status"] = "blocked"
        paper_readiness["decision"] = "do_not_draft_results_paper"
        paper_readiness.setdefault("findings", []).append({
            "severity": "blocker",
            "code": "placeholder_bibliography",
            "owner": "conception",
            "message": "The bibliography contains fixture or placeholder references and cannot be used in a formal paper.",
            "citation_ids": [item.get("id") for item in bibliography_quality["findings"]],
        })
        actions = paper_readiness.setdefault("recommended_actions", [])
        if "replan_conception" not in actions:
            actions.insert(0, "replan_conception")
    execution_alignment = build_execution_alignment(inputs)
    execution_integrity = build_execution_integrity(inputs, inventory)
    hypothesis_coverage = {
        str(item.get("hypothesis_id") or ""): item
        for item in paper_readiness.get("hypothesis_coverage") or []
        if isinstance(item, dict) and item.get("hypothesis_id")
    }
    results = inputs["m3"].get("experiment_results") or {}
    raw_hypothesis_evaluations = (
        results.get("hypothesis_evaluations")
        or inputs["m3"].get("hypothesis_evaluations")
        or []
    )
    hypothesis_evaluations = {
        str(item.get("hypothesis_id") or "").strip(): item
        for item in raw_hypothesis_evaluations
        if isinstance(item, dict) and str(item.get("hypothesis_id") or "").strip()
    }
    verdict_status = {
        "SUPPORTED": "supported",
        "NOT_SUPPORTED": "not_supported",
        "INCONCLUSIVE": "inconclusive",
    }
    claims = []
    for hypothesis in inputs["m1"].get("hypotheses", []):
        if not isinstance(hypothesis, dict):
            continue
        hypothesis_id = str(hypothesis.get("id") or "")
        coverage = hypothesis_coverage.get(hypothesis_id) or {}
        evaluation = hypothesis_evaluations.get(hypothesis_id) or {}
        evaluation_status = "evaluated" if coverage.get("available") is True else "planned"
        support_status = verdict_status.get(str(evaluation.get("verdict") or "").upper())
        # Execution and evidential support are different axes.  Module 3 already
        # emits an audited hypothesis verdict; discarding it and calling every
        # executed hypothesis merely ``evaluated`` leaves writers and reviewers
        # to reinterpret strong natural-language claims, which creates endless
        # revision loops.  If an experiment ran but no machine-readable verdict
        # is available, fail closed to inconclusive rather than implying support.
        if support_status is None:
            support_status = "inconclusive" if evaluation_status == "evaluated" else "planned"
        claim = {
            "id": hypothesis_id,
            "text": hypothesis.get("claim"),
            "kind": "hypothesis",
            "status": support_status,
            "evaluation_status": evaluation_status,
        }
        evidence_experiment_ids = [
            str(value) for value in evaluation.get("evidence_experiment_ids") or []
            if str(value).strip()
        ]
        if evidence_experiment_ids:
            claim["evidence_experiment_ids"] = evidence_experiment_ids
        if evaluation.get("reason"):
            claim["verdict_reason"] = str(evaluation["reason"])
        if coverage.get("experiment_id"):
            claim["experiment_id"] = coverage["experiment_id"]
        claims.append(claim)
    innovation_coverage = paper_readiness.get("innovation_point_coverage") or []
    for index, point in enumerate((inputs["m2"].get("method_design") or {}).get("innovation_points") or []):
        if not isinstance(point, dict):
            continue
        coverage = next((item for item in innovation_coverage if item.get("index") == index), {})
        claims.append({
            "id": f"innovation-{index + 1}", "text": point.get("claim"), "kind": "innovation",
            "status": coverage.get("claim_status", "untested"),
            "experiment_id": coverage.get("experiment_id"),
            "required_metrics": coverage.get("required_metrics", _metric_specs(point.get("evidence_metric"))),
        })
    measurements: list[dict[str, Any]] = []
    aggregate_values: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    if inventory["evidence_mode"] == "verified":
        for run in inputs["m3"].get("experiment_runs", []):
            if isinstance(run, dict):
                measurements.append({"run_id": run.get("run_id") or run.get("run_record_id"),
                    "experiment_id": run.get("experiment_id"), "source": "experiment_runs", "record": run})
                if run.get("success") is not True:
                    continue
                experiment_id = str(run.get("experiment_id") or "")
                method = str(run.get("method") or "")
                for metric in run.get("metrics") or []:
                    if not isinstance(metric, dict):
                        continue
                    try:
                        aggregate_values[(experiment_id, method, str(metric.get("name") or ""))].append(float(metric["value"]))
                    except (KeyError, TypeError, ValueError):
                        continue
    aggregates = [
        {
            "experiment_id": experiment_id, "method": method, "metric": metric,
            "run_count": len(values), "mean": fmean(values),
            "population_stddev": pstdev(values) if len(values) > 1 else None,
            "uncertainty_status": "estimated" if len(values) > 1 else "not_estimable_single_run",
            "minimum": min(values), "maximum": max(values),
        }
        for (experiment_id, method, metric), values in sorted(aggregate_values.items())
        if values
    ]
    for index, aggregate in enumerate(aggregates, start=1):
        aggregate["id"] = f"aggregate-{index}"
    _reconcile_claim_evidence_scope(claims, aggregates)
    _bind_claim_evidence_identities(claims, aggregates)
    payloads = {
        stages_dir / "00_input_inventory.json": inventory,
        evidence_dir / "claims.json": {"schema_version": 1, "claims": claims},
        evidence_dir / "measurements.json": {
            "schema_version": 1, "measurements": measurements,
            "aggregates": aggregates,
            "aggregation_scope": "Each aggregate is scoped to exactly one experiment_id, method, and metric.",
        },
        evidence_dir / "bibliography.json": {"schema_version": 1, "entries": bibliography},
        evidence_dir / "bibliography_gaps.json": {"schema_version": 1, "gaps": gaps},
        evidence_dir / "bibliography_quality.json": bibliography_quality,
        evidence_dir / "paper_readiness.json": paper_readiness,
        evidence_dir / "execution_alignment.json": execution_alignment,
        evidence_dir / "execution_integrity.json": execution_integrity,
    }
    for path, payload in payloads.items():
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {path.stem: str(path) for path in payloads}


def write_blueprint(output_dir: str | Path, inputs: dict[str, Any], inventory: dict[str, Any]) -> dict[str, str]:
    """Create an auditable pre-writing outline and asset plan.

    This is intentionally conservative: a method schematic can be planned from
    method components, while result plots/tables remain blocked until their
    measurements are present and traceable.
    """
    root = Path(output_dir)
    stages, figures, tables = root / "stages", root / "assets" / "figures", root / "assets" / "tables"
    stages.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)
    tables.mkdir(parents=True, exist_ok=True)
    components = (inputs["m2"].get("method_design") or {}).get("components") or []
    figure_plan = [{
        "id": "method_overview", "kind": "figure", "purpose": "method schematic",
        "section": "method", "placement_after_paragraph_id": "method-overview",
        "source_paths": ["m2.method_design.components"],
        "status": "planned" if components else "blocked_missing_data",
        "caption_outline": "Overview of the proposed method components.",
    }]
    table_status = "planned" if inventory["evidence_mode"] == "verified" else "blocked_missing_data"
    table_plan = [{
        "id": "main_results", "kind": "table", "purpose": "main result comparison",
        "section": "experiments", "placement_after_paragraph_id": "results-overview",
        "source_paths": ["evidence/measurements.json"], "status": table_status,
        "required_columns": ["run_id", "experiment_id", "dataset", "method", "metric", "value"],
        "caption_outline": "Main results from traceable experiment runs.",
    }]
    blueprint = {
        "schema_version": 1, "evidence_mode": inventory["evidence_mode"],
        "title_status": "requires_english_writer",  # Chinese topics are never copied into TeX title.
        "section_order": ["introduction", "related_work", "method", "experiments", "limitations", "conclusion"],
        "claim_ids": [str(h.get("id")) for h in inputs["m1"].get("hypotheses", []) if isinstance(h, dict)] + [f"innovation-{index + 1}" for index, item in enumerate((inputs["m2"].get("method_design") or {}).get("innovation_points") or []) if isinstance(item, dict)],
        "asset_ids": [item["id"] for item in figure_plan + table_plan],
    }
    paths = {stages / "02_blueprint.json": blueprint, figures / "plan.json": figure_plan, tables / "plan.json": table_plan}
    for path, payload in paths.items():
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {path.stem + ("_" + path.parent.name if path.name == "plan.json" else ""): str(path) for path in paths}
