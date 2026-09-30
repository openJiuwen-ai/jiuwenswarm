"""Evidence-bound visual planning and deterministic scientific rendering.

LLMs select a purpose and a registered template.  They never execute plotting
code or supply chart numbers; this module reads the immutable Module 3 records
and emits versioned PDF/PNG/TeX assets with provenance.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import textwrap
from collections import defaultdict
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any


# This catalog is intentionally broader than one experiment domain.  Entries
# whose data contract is absent remain unavailable; they are not invitations to
# invent data.  New Module 3 adapters may expose the named contracts later.
TEMPLATE_CATALOG: dict[str, dict[str, Any]] = {
    "method_protocol_flow": {"kind": "figure", "requires": ["method_components"]},
    "model_architecture_flow": {"kind": "figure", "requires": ["method_components"]},
    "algorithm_flow": {"kind": "figure", "requires": ["method_components"]},
    # Intervals and seed trajectories have no evidential meaning when every
    # method has one run.  Keep them unavailable rather than emitting a blank
    # canvas or an apparent uncertainty display from a single point.
    "metric_dot_interval": {"kind": "figure", "requires": ["metric_rows", "repeated_runs"]},
    "metric_strip": {"kind": "figure", "requires": ["metric_rows"]},
    "metric_bar": {"kind": "figure", "requires": ["metric_rows"]},
    "metric_boxplot": {"kind": "figure", "requires": ["metric_rows", "repeated_runs"]},
    "metric_violin": {"kind": "figure", "requires": ["metric_rows", "repeated_runs"]},
    "seed_trajectory": {"kind": "figure", "requires": ["metric_rows", "seed_values", "repeated_runs"]},
    "pareto_scatter": {"kind": "figure", "requires": ["metric_rows", "timing_metric"]},
    "learning_curve": {"kind": "figure", "requires": ["curve_series"]},
    "line_comparison": {"kind": "figure", "requires": ["curve_series"]},
    "roc_curve": {"kind": "figure", "requires": ["prediction_scores"]},
    "precision_recall_curve": {"kind": "figure", "requires": ["prediction_scores"]},
    "calibration_curve": {"kind": "figure", "requires": ["prediction_scores"]},
    "confusion_matrix": {"kind": "figure", "requires": ["prediction_labels"]},
    "feature_importance": {"kind": "figure", "requires": ["feature_attributions"]},
    "attention_heatmap": {"kind": "figure", "requires": ["matrix_data"]},
    "correlation_heatmap": {"kind": "figure", "requires": ["matrix_data"]},
    "qualitative_grid": {"kind": "figure", "requires": ["qualitative_examples"]},
    "stage3_result_figure": {"kind": "figure", "requires": ["auditable_figure_artifacts"]},
    "main_metrics_table": {"kind": "table", "requires": ["metric_rows"]},
    "experiment_protocol_table": {"kind": "table", "requires": ["experiment_protocol"]},
    "ablation_table": {"kind": "table", "requires": ["ablation_records"]},
    "per_seed_results_table": {"kind": "table", "requires": ["metric_rows", "seed_values", "repeated_runs"]},
}

# A catalog entry may describe a future data contract, but it is selectable only
# after this process has a deterministic renderer for it.  This prevents an LLM
# from treating a descriptive registry entry as executable functionality.
IMPLEMENTED_TEMPLATES = {
    "method_protocol_flow", "model_architecture_flow", "algorithm_flow",
    "metric_dot_interval", "metric_strip", "metric_bar", "metric_boxplot", "metric_violin",
    "seed_trajectory", "pareto_scatter", "main_metrics_table", "experiment_protocol_table",
    "stage3_result_figure",
}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_PALETTE = ("#159D8E", "#4C78A8", "#7A5AA6", "#D98B3A", "#B85C70", "#7A8793")


def _publication_rc() -> dict[str, Any]:
    """Portable journal-style defaults with CJK-safe installed-font fallback."""
    try:
        from matplotlib import font_manager
        candidates = (
            "Microsoft YaHei", "Microsoft YaHei UI", "Noto Sans CJK SC",
            "Noto Sans SC", "Source Han Sans SC", "SimHei", "Arial",
            "Helvetica", "DejaVu Sans", "Liberation Sans",
        )
        installed = {item.name for item in font_manager.fontManager.ttflist}
        fonts = [name for name in candidates if name in installed]
    except Exception:
        fonts = []
    if "DejaVu Sans" not in fonts:
        fonts.append("DejaVu Sans")
    return {
        "font.family": "sans-serif", "font.sans-serif": fonts,
        "font.size": 8.0, "axes.labelsize": 8.5, "axes.titlesize": 10.0,
        "xtick.labelsize": 7.4, "ytick.labelsize": 7.4,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.linewidth": 0.75, "axes.edgecolor": "#8D98A3",
        "axes.unicode_minus": False, "legend.frameon": False,
        "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none",
        "figure.facecolor": "white", "axes.facecolor": "white",
        "savefig.facecolor": "white",
    }


def _display_label(value: object) -> str:
    """Humanize identifiers without blind title-casing canonical model names."""
    raw = str(value or "").strip()
    exact = {
        "macro_f1": "Macro-F1", "micro_f1": "Micro-F1",
        "weighted_f1": "Weighted F1", "accuracy": "Accuracy",
        "train_time_seconds": "Training time (s)",
        "inference_time_seconds": "Inference time (s)",
    }.get(raw.casefold())
    if exact:
        return exact
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    text = re.sub(r"[_-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _compact_labels(values: list[str], *, limit: int = 28) -> list[str]:
    """Return short, unique display labels; full names remain in source JSON."""
    labels: list[str] = []
    for value in values:
        text = _display_label(value)
        for separator in (" for ", ": ", " ("):
            head, found, _ = text.partition(separator)
            if found and len(head) >= 5:
                text = head
                break
        if len(text) > limit:
            words = text.split()
            shortened = ""
            for word in words:
                candidate = (shortened + " " + word).strip()
                if len(candidate) > limit - 1:
                    break
                shortened = candidate
            text = (shortened or text[: limit - 1]).rstrip() + "…"
        labels.append(text)
    counts: dict[str, int] = defaultdict(int)
    result: list[str] = []
    for label in labels:
        counts[label] += 1
        result.append(label if labels.count(label) == 1 else f"{label} · {counts[label]}")
    return result


def _abbreviated_labels(values: list[str], *, limit: int = 22) -> tuple[list[str], dict[str, str]]:
    """Create stable display-only abbreviations and an explicit reverse map."""
    displays: list[str] = []
    mapping: dict[str, str] = {}
    used: dict[str, int] = defaultdict(int)
    stop = {"for", "and", "of", "the", "with", "using", "framework", "method", "module"}
    for original in values:
        human = _display_label(original)
        if len(human) <= limit:
            display = human
        else:
            words = [word for word in re.findall(r"[A-Za-z0-9]+", human) if word.casefold() not in stop]
            acronym = "".join(word[0].upper() for word in words if word)
            display = acronym if 2 <= len(acronym) <= 8 else _compact_labels([human], limit=limit)[0]
        used[display] += 1
        if used[display] > 1:
            display = f"{display}-{used[display]}"
        displays.append(display)
        if display != original:
            mapping[display] = original
    return displays, mapping


def _abbreviation_note(mapping: dict[str, str]) -> str:
    if not mapping:
        return ""
    expanded = "; ".join(f"{short} = {full}" for short, full in mapping.items())
    # A caption is a locator, not a second methods section.  Repeating several
    # long canonical identifiers can dominate a figure/table and trigger tiny
    # type or an otherwise empty float page.  The reversible mapping remains
    # in the manifest for downstream agents; concise figures point readers to
    # the surrounding canonical prose instead.
    if len(mapping) <= 2 and len(expanded) <= 140:
        return "Display-label mapping (canonical names): " + expanded + "."
    return (
        "Display labels are readability-only abbreviations; the full canonical "
        "method identifiers remain in the surrounding Methods/Experiments prose "
        "and in this asset's provenance record."
    )


def _flow_display_labels(values: list[str], *, limit: int = 27) -> tuple[list[str], dict[str, str]]:
    """Create readable diagram labels while preserving exact component names.

    These are semantic display aliases, not identifiers.  The reverse map is
    persisted in the asset manifest and caption so downstream modules always
    continue to join on the canonical names.
    """
    role_suffixes = {"builder", "trainer", "evaluator", "monitor", "manager", "generator"}
    labels: list[str] = []
    mapping: dict[str, str] = {}
    used: dict[str, int] = defaultdict(int)
    for original in values:
        human = _display_label(original)
        words = human.split()
        suffix = words[-1].casefold() if words else ""
        if suffix in role_suffixes and len(words) > 1:
            words.pop()
        if suffix == "builder" and words and words[-1].casefold() == "feature":
            words[-1] = "features"
        phrase = re.sub(r"\band\b", "&", " ".join(words), flags=re.I)
        if len(phrase) > limit:
            # Prefer an intelligible phrase over an opaque acronym.  Preserve
            # the functional suffix because it explains the node's role.
            role = words[-1] if words else "stage"
            core = words[:-1]
            while core and len(" ".join(core + [role])) > limit:
                core.pop(0)
            phrase = " ".join(core + [role]) if core else role
        normalized_words = [
            word if (word == "&" or word.isupper() or index == 0) else word.casefold()
            for index, word in enumerate(phrase.split())
        ]
        phrase = " ".join(normalized_words) or "Recorded stage"
        used[phrase] += 1
        display = phrase if used[phrase] == 1 else f"{phrase} {used[phrase]}"
        labels.append(display)
        if display != original:
            mapping[display] = original
    return labels, mapping


def _style_axis(axis, *, grid_axis: str) -> None:
    axis.set_axisbelow(True)
    axis.grid(axis=grid_axis, color="#E7EBEF", linewidth=0.7, alpha=0.9)
    axis.spines["left"].set_color("#9AA4AE")
    axis.spines["bottom"].set_color("#9AA4AE")
    axis.tick_params(length=3.0, width=0.65, colors="#46515B")


def _runs(inputs: dict[str, Any]) -> list[dict[str, Any]]:
    m3 = inputs.get("m3") if isinstance(inputs.get("m3"), dict) else {}
    records = m3.get("experiment_runs") or (m3.get("experiment_results") or {}).get("experiment_runs") or []
    return [record for record in records if isinstance(record, dict) and record.get("success") is True]


def _seed(record: dict[str, Any]) -> Any:
    if record.get("seed") is not None:
        return record.get("seed")
    config = record.get("config") or record.get("configuration") or {}
    if isinstance(config, dict) and config.get("seed") is not None:
        return config.get("seed")
    # Some adapters preserve the traceable run identifier but omit a duplicate
    # top-level seed field.  Recover only the explicit ``seed-42``/``seed_42``
    # token; never infer a seed from ordering.
    for value in (record.get("run_id"), record.get("run_record_id"), record.get("config_path")):
        match = re.search(r"(?:^|[-_])seed[-_](\d+)(?:$|[-_.])", str(value or ""), re.I)
        if match:
            return int(match.group(1))
    return None


def metric_rows(inputs: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in _runs(inputs):
        for metric in record.get("metrics") or []:
            if not isinstance(metric, dict):
                continue
            try:
                value = float(metric["value"])
            except (KeyError, TypeError, ValueError):
                continue
            rows.append({
                "run_id": record.get("run_id") or record.get("run_record_id"),
                "experiment_id": record.get("experiment_id"),
                "method": str(record.get("method") or "unknown"), "seed": _seed(record),
                "metric": str(metric.get("name") or ""), "value": value,
                "unit": metric.get("unit"),
            })
    return rows


_EMPIRICAL_TEMPLATES = {
    "metric_dot_interval", "metric_strip", "metric_bar", "metric_boxplot",
    "metric_violin", "seed_trajectory", "pareto_scatter", "main_metrics_table",
    "stage3_result_figure",
}


def _auditable_stage3_figures(inputs: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Classify Module 3 figures without trusting their filenames or captions.

    A reusable result figure must be inside ``artifact_root``, listed in the
    stage-three artifact manifest, and scoped to exactly one experiment.  This
    deliberately rejects the colleague pipeline's cross-experiment comparison
    figures: their pixels may be attractive, but the current writing contract
    cannot prove that rows from different experiments are comparable.
    """
    m3 = inputs.get("m3") if isinstance(inputs.get("m3"), dict) else {}
    results = m3.get("experiment_results") if isinstance(m3.get("experiment_results"), dict) else {}
    root_value = str(m3.get("artifact_root") or "").strip()
    root = Path(root_value).resolve() if root_value else None
    listed_paths: set[str] = set()
    manifest_path = Path(str(m3.get("artifact_manifest_path") or ""))
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        for entry in manifest.get("artifacts") or [] if isinstance(manifest, dict) else []:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                listed_paths.add(Path(entry["path"]).as_posix())

    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, item in enumerate(results.get("figure_artifacts") or []):
        if not isinstance(item, dict):
            continue
        artifact_id = str(item.get("figure_id") or item.get("artifact_id") or item.get("name") or f"figure-{index + 1}")
        relative_value = str(item.get("path") or "").strip()
        scopes = sorted({str(value) for value in item.get("source_experiment_ids") or [] if str(value).strip()})
        reason = None
        source: Path | None = None
        if not root or not relative_value:
            reason = "missing artifact_root or path"
        else:
            relative = Path(relative_value)
            if relative.is_absolute() or ".." in relative.parts:
                reason = "artifact path is not a safe relative path"
            else:
                source = (root / relative).resolve()
                if root not in source.parents or not source.is_file():
                    reason = "artifact file is missing or escapes artifact_root"
                elif relative.as_posix() not in listed_paths:
                    reason = "artifact is absent from the stage-three manifest"
        displayed_metrics = sorted({str(value).strip() for value in item.get("displayed_metrics") or [] if str(value).strip()})
        caption_assertions = item.get("caption_assertions")
        if not reason and len(scopes) != 1:
            reason = "result figure must declare exactly one experiment scope"
        # A file hash proves where pixels came from, but not what they mean.
        # Reusing a third-party Module 3 chart therefore requires a compact
        # semantic contract.  In particular, it prevents a caption from
        # claiming metrics or threshold lines that are absent from the plot.
        if not reason and not displayed_metrics:
            reason = "result figure lacks semantic display contract: displayed_metrics"
        if not reason and not isinstance(caption_assertions, list):
            reason = "result figure lacks semantic display contract: caption_assertions"
        record = {
            "id": artifact_id,
            "path": relative_value,
            "caption": str(item.get("caption") or "Stage-three result figure."),
            "source_experiment_ids": scopes,
            "experiment_id": scopes[0] if len(scopes) == 1 else None,
            "displayed_metrics": displayed_metrics,
            "caption_assertions": caption_assertions if isinstance(caption_assertions, list) else [],
            "display_label_map": item.get("display_label_map") if isinstance(item.get("display_label_map"), dict) else {},
            "source_sha256": _digest(source) if source and source.is_file() else None,
        }
        (rejected if reason else eligible).append({**record, **({"reason": reason} if reason else {})})
    return eligible, rejected


def _experiment_ids(rows: list[dict[str, Any]]) -> list[str]:
    """Return explicit experiment scopes in stable order.

    A missing experiment ID is never folded into another experiment.  It is
    retained as an empty-string scope so a caller can reject it explicitly.
    """
    return sorted({str(row.get("experiment_id") or "") for row in rows})


def _preferred_experiment_ids(inputs: dict[str, Any]) -> list[str]:
    m2 = inputs.get("m2") if isinstance(inputs.get("m2"), dict) else {}
    plan = m2.get("experiment_plan") if isinstance(m2.get("experiment_plan"), dict) else {}
    return [
        str(value)
        for value in plan.get("primary_experiments") or []
        if isinstance(value, (str, int)) and str(value).strip()
    ]


def _claim_catalog(inputs: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose stable innovation claim IDs to visual planning without prose inference."""
    m2 = inputs.get("m2") if isinstance(inputs.get("m2"), dict) else {}
    design = m2.get("method_design") or {}
    claims: list[dict[str, Any]] = []
    for index, point in enumerate(design.get("innovation_points") or []):
        if not isinstance(point, dict):
            continue
        metrics: list[str] = []
        raw_metrics = point.get("evidence_metric")
        for item in raw_metrics if isinstance(raw_metrics, list) else [raw_metrics]:
            if isinstance(item, str) and item.strip():
                metrics.append(item.strip())
            elif isinstance(item, dict):
                name = str(item.get("metric_name") or item.get("name") or "").strip()
                if name:
                    metrics.append(name)
        claims.append({"id": f"innovation-{index + 1}", "experiment_id": str(point.get("experiment_ref") or "").strip(), "metrics": metrics})
    return claims


def _default_experiment_id(catalog: dict[str, Any], required_metrics: set[str] | None = None) -> str | None:
    """Select one declared scope for deterministic fallback visuals.

    The fallback is intentionally narrow: it may choose a planning-declared
    primary experiment (or the sole available scope), but never combines all
    scopes merely because their method and metric names match.
    """
    scopes = catalog.get("experiment_scopes") or {}
    candidates = [
        experiment_id for experiment_id in catalog.get("preferred_experiment_ids") or []
        if experiment_id in scopes
    ]
    candidates.extend(
        experiment_id for experiment_id in catalog.get("experiment_ids") or []
        if experiment_id not in candidates
    )
    for experiment_id in candidates:
        scope = scopes.get(experiment_id) or {}
        if required_metrics and not required_metrics.issubset(set(scope.get("metrics") or [])):
            continue
        return experiment_id
    return None


_REPEAT_RUN_TEMPLATES = {"metric_dot_interval", "metric_boxplot", "metric_violin", "seed_trajectory", "per_seed_results_table"}


def _has_repeatability(catalog: dict[str, Any], experiment_id: str, metric: str, *, require_distinct_seeds: bool = False) -> bool:
    """Whether a particular plotted series, not the entire paper, repeats.

    A paper can contain many experiments and several metrics per run.  Neither
    is evidence of repeated measurement for one plotted metric.  The catalog
    therefore stores counts keyed by experiment, metric and method, with run
    identity deduplicated before this helper is called.
    """
    scope = (catalog.get("repeatability_by_scope") or {}).get(experiment_id) or {}
    metric_scope = scope.get(metric) or {}
    field = "method_seed_counts" if require_distinct_seeds else "method_run_counts"
    return any(int(value) >= 2 for value in (metric_scope.get(field) or {}).values())


def build_visual_capability_catalog(inputs: dict[str, Any], evidence_mode: str) -> dict[str, Any]:
    rows = metric_rows(inputs) if evidence_mode in {"verified", "synthetic"} else []
    m2 = inputs.get("m2") if isinstance(inputs.get("m2"), dict) else {}
    design = m2.get("method_design") or {}
    m3 = inputs.get("m3") if isinstance(inputs.get("m3"), dict) else {}
    results = m3.get("experiment_results") or {}
    auditable_figures, rejected_figures = _auditable_stage3_figures(inputs)
    metrics = sorted({row["metric"] for row in rows if row["metric"]})
    timing = sorted(metric for metric in metrics if "time" in metric.casefold() or "latency" in metric.casefold() or "runtime" in metric.casefold())
    experiment_ids = _experiment_ids(rows)
    repeatability_by_scope: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
    for experiment_id in experiment_ids:
        if not experiment_id:
            continue
        per_metric: dict[str, dict[str, dict[str, int]]] = {}
        scope_rows = [row for row in rows if row["experiment_id"] == experiment_id]
        for metric in sorted({row["metric"] for row in scope_rows if row["metric"]}):
            metric_rows_for_scope = [row for row in scope_rows if row["metric"] == metric]
            methods = sorted({row["method"] for row in metric_rows_for_scope})
            per_metric[metric] = {
                "method_run_counts": {
                    method: len({str(row["run_id"]) for row in metric_rows_for_scope if row["method"] == method})
                    for method in methods
                },
                "method_seed_counts": {
                    method: len({str(row["seed"]) for row in metric_rows_for_scope if row["method"] == method and row["seed"] is not None})
                    for method in methods
                },
            }
        repeatability_by_scope[experiment_id] = per_metric
    experiment_scopes = {
        experiment_id: {
            "run_count": len({row["run_id"] for row in rows if str(row.get("experiment_id") or "") == experiment_id}),
            "method_run_counts": {
                method: len({row["run_id"] for row in rows if str(row.get("experiment_id") or "") == experiment_id and row["method"] == method})
                for method in sorted({row["method"] for row in rows if str(row.get("experiment_id") or "") == experiment_id})
            },
            "metrics": sorted({row["metric"] for row in rows if str(row.get("experiment_id") or "") == experiment_id and row["metric"]}),
            "methods": sorted({row["method"] for row in rows if str(row.get("experiment_id") or "") == experiment_id}),
        }
        for experiment_id in experiment_ids
        if experiment_id
    }
    capabilities = {
        "method_components": bool(design.get("components")),
        "metric_rows": bool(rows),
        "repeated_runs": any(
            count >= 2
            for per_metric in repeatability_by_scope.values()
            for evidence in per_metric.values()
            for count in (evidence.get("method_run_counts") or {}).values()
        ),
        "seed_values": bool(rows) and all(row["seed"] is not None for row in rows),
        "timing_metric": bool(timing),
        "curve_series": bool(results.get("curve_series") or m3.get("curve_series")),
        "prediction_labels": bool(results.get("prediction_artifacts") or m3.get("prediction_artifacts")),
        "prediction_scores": bool(results.get("prediction_score_artifacts") or m3.get("prediction_score_artifacts")),
        "feature_attributions": bool(results.get("feature_attributions") or m3.get("feature_attributions")),
        "matrix_data": bool(results.get("matrix_artifacts") or m3.get("matrix_artifacts")),
        "qualitative_examples": bool(results.get("qualitative_examples") or m3.get("qualitative_examples")),
        "auditable_figure_artifacts": bool(auditable_figures),
        "ablation_records": bool(results.get("ablation_runs") or m3.get("ablation_runs")),
        "experiment_protocol": bool((m2.get("experiment_plan") or {}).get("datasets") or design),
    }
    supported = {
        name: {
            **spec,
            "renderer_available": name in IMPLEMENTED_TEMPLATES,
            "available": name in IMPLEMENTED_TEMPLATES and all(capabilities.get(key, False) for key in spec["requires"]),
        }
        for name, spec in TEMPLATE_CATALOG.items()
    }
    return {
        "schema_version": 1, "evidence_mode": evidence_mode, "capabilities": capabilities,
        "metrics": metrics, "timing_metrics": timing, "methods": sorted({row["method"] for row in rows}),
        "run_count": len({row["run_id"] for row in rows}),
        "experiment_ids": [experiment_id for experiment_id in experiment_ids if experiment_id],
        "experiment_scopes": experiment_scopes,
        "repeatability_by_scope": repeatability_by_scope,
        "preferred_experiment_ids": _preferred_experiment_ids(inputs),
        "claims": _claim_catalog(inputs),
        "stage3_figure_artifacts": auditable_figures,
        "rejected_stage3_figure_artifacts": rejected_figures,
        "supported_templates": supported,
    }


def summarize_visual_evidence(inputs: dict[str, Any]) -> dict[str, Any]:
    """Compact numeric summary safe to give the visual planner, unlike raw paths."""
    grouped: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for row in metric_rows(inputs):
        grouped[(str(row.get("experiment_id") or ""), row["method"], row["metric"])].append(row["value"])
    return {
        "aggregates": [
            {"experiment_id": experiment_id, "method": method, "metric": metric, "run_count": len(values),
             "mean": fmean(values), "population_stddev": pstdev(values) if len(values) > 1 else None,
             "uncertainty_status": "estimated" if len(values) > 1 else "not_estimable_single_run",
             "minimum": min(values), "maximum": max(values)}
            for (experiment_id, method, metric), values in sorted(grouped.items()) if values
        ]
    }


def _primary_metric(catalog: dict[str, Any]) -> str | None:
    metrics = list(catalog.get("metrics") or [])
    priorities = ("accuracy", "macro_f1", "f1", "auc", "r2", "rmse", "mae")
    return next((metric for preferred in priorities for metric in metrics if metric.casefold() == preferred), metrics[0] if metrics else None)


def _main_table_metrics(catalog: dict[str, Any], experiment_id: str | None, requested: object = None) -> list[str]:
    """Choose the efficacy metrics shown in the main-results table.

    Timing metrics have their own paired efficiency figure.  The main table
    otherwise reports every recorded efficacy metric in its single experiment
    scope, so prose cannot discuss a second primary metric that readers cannot
    inspect in the table.  A planner may request a subset only when every name
    is present in that scope.
    """
    scope_metrics = list(((catalog.get("experiment_scopes") or {}).get(str(experiment_id) or "") or {}).get("metrics") or [])
    requested_metrics = requested if isinstance(requested, list) else []
    selected = [str(metric) for metric in requested_metrics if str(metric) in scope_metrics]
    if selected:
        return list(dict.fromkeys(selected))
    efficacy = [metric for metric in scope_metrics if metric not in (catalog.get("timing_metrics") or [])]
    return efficacy or scope_metrics


def _run_count_caption(method_counts: dict[str, Any]) -> tuple[str, str]:
    """Return truthful run-count and uncertainty text for aggregate charts."""
    counts: dict[str, int] = {}
    for method, raw_count in method_counts.items():
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            continue
        if count > 0:
            counts[str(method)] = count
    run_note = "; ".join(f"{method}={count}" for method, count in sorted(counts.items()))
    run_note = f"Runs per method: {run_note}." if run_note else ""
    if any(count > 1 for count in counts.values()):
        uncertainty = (
            "Error bars denote population standard deviation for methods with repeated verified runs; "
            "a method with one verified run has no uncertainty interval."
        )
    else:
        uncertainty = "No uncertainty interval is drawn because every method has one verified run."
    return run_note, uncertainty


def default_visual_plan(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """Conservative fallback used for deterministic tests and invalid plans."""
    caps = catalog["capabilities"]; primary = _primary_metric(catalog); timing = (catalog.get("timing_metrics") or [None])[0]
    primary_scope = _default_experiment_id(catalog, {primary} if primary else None)
    efficiency_scope = _default_experiment_id(catalog, {primary, timing} if primary and timing else None)
    assets: list[dict[str, Any]] = []
    def matching_claims(experiment_id: str | None, metric: str | None) -> list[str]:
        if not experiment_id:
            return []
        values = []
        for claim in catalog.get("claims") or []:
            if claim.get("experiment_id") != experiment_id:
                continue
            declared = claim.get("metrics") or []
            if metric and declared and metric not in declared and f"{metric}_mean" not in declared:
                continue
            values.append(str(claim.get("id")))
        return values
    if caps["method_components"]:
        assets.append({"id": "method_protocol", "kind": "figure", "template": "method_protocol_flow", "purpose": "Explain the method or experimental pipeline.", "section": "method", "placement_after_paragraph_id": "", "claim_ids": [], "caption": "Overview of the proposed method and evaluation pipeline."})
    for index, supplied in enumerate((catalog.get("stage3_figure_artifacts") or [])[:3]):
        experiment_id = str(supplied.get("experiment_id") or "")
        assets.append({
            "id": f"stage3_{re.sub(r'[^a-z0-9_]+', '_', str(supplied.get('id') or index).casefold()).strip('_')}",
            "kind": "figure", "template": "stage3_result_figure",
            "purpose": "Present a stage-three result figure within its registered experiment scope.",
            "section": "experiments", "placement_after_paragraph_id": "",
            "claim_ids": matching_claims(experiment_id, None),
            "experiment_id": experiment_id, "artifact_id": supplied.get("id"),
            "caption": supplied.get("caption") or "Stage-three result figure.",
        })
    if primary and primary_scope:
        assets.append({"id": "main_metrics_table", "kind": "table", "template": "main_metrics_table", "purpose": "Compare verified aggregate results.", "section": "experiments", "placement_after_paragraph_id": "", "claim_ids": matching_claims(primary_scope, primary), "experiment_id": primary_scope, "metric": primary, "metrics": _main_table_metrics(catalog, primary_scope), "caption": f"Aggregate efficacy metrics from verified runs in experiment {primary_scope}."})
    if primary and primary_scope and _has_repeatability(catalog, primary_scope, primary):
        assets.append({"id": "main_metric_interval", "kind": "figure", "template": "metric_dot_interval", "purpose": "Show central performance and run-to-run variation.", "section": "experiments", "placement_after_paragraph_id": "", "claim_ids": matching_claims(primary_scope, primary), "experiment_id": primary_scope, "metric": primary, "caption": f"{primary} across repeated verified runs in experiment {primary_scope}; points show individual runs."})
    if primary and primary_scope and caps["seed_values"] and _has_repeatability(catalog, primary_scope, primary, require_distinct_seeds=True):
        assets.append({"id": "seed_stability", "kind": "figure", "template": "seed_trajectory", "purpose": "Show sensitivity to random seed.", "section": "experiments", "placement_after_paragraph_id": "", "claim_ids": matching_claims(primary_scope, primary), "experiment_id": primary_scope, "metric": primary, "caption": f"Per-seed {primary} for each evaluated method in experiment {primary_scope}."})
    if primary and timing and efficiency_scope:
        assets.append({"id": "performance_efficiency", "kind": "figure", "template": "pareto_scatter", "purpose": "Show performance-efficiency trade-offs.", "section": "experiments", "placement_after_paragraph_id": "", "claim_ids": matching_claims(efficiency_scope, primary), "experiment_id": efficiency_scope, "x_metric": timing, "y_metric": primary, "caption": f"Mean {primary} versus mean {timing} for verified runs in experiment {efficiency_scope}."})
    if caps["experiment_protocol"]:
        assets.append({"id": "experiment_protocol", "kind": "table", "template": "experiment_protocol_table", "purpose": "Make the evaluation protocol reproducible.", "section": "experiments", "placement_after_paragraph_id": "", "claim_ids": [], "caption": "Experimental protocol and available evaluation settings."})
    return assets


def normalize_visual_plan(raw: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    raw_assets = raw.get("assets") if isinstance(raw, dict) else None
    candidates = raw_assets if isinstance(raw_assets, list) else []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    claim_records = {
        str(claim.get("id")): claim for claim in catalog.get("claims") or []
        if isinstance(claim, dict) and str(claim.get("id") or "").strip()
    }
    known_claims = set(claim_records)
    claim_status = {claim_id: str(claim.get("status") or "") for claim_id, claim in claim_records.items()}

    def truthful_text(purpose: str, caption: str, claim_ids: list[str]) -> tuple[str, str]:
        limited = [(claim_id, claim_status.get(claim_id)) for claim_id in claim_ids if claim_status.get(claim_id) in {"not_supported", "inconclusive"}]
        if not limited:
            return purpose, caption
        statuses = ", ".join(f"{claim_id}={status}" for claim_id, status in limited)
        return (
            f"Evaluate the registered claim under its observed evidence status ({statuses}).",
            f"Evidence for {statuses}; the visual reports the observed result and does not establish the planned direction.",
        )

    def matching_claims(experiment_id: str, metric: str) -> list[str]:
        return [
            str(claim["id"])
            for claim in catalog.get("claims") or []
            if isinstance(claim, dict)
            and claim.get("experiment_id") == experiment_id
            and (not claim.get("metrics") or metric in claim.get("metrics", []) or f"{metric}_mean" in claim.get("metrics", []))
        ]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        template = str(item.get("template") or "")
        spec = (catalog.get("supported_templates") or {}).get(template)
        asset_id = str(item.get("id") or "").strip().lower().replace("-", "_")
        if not spec or not spec.get("available") or not asset_id or asset_id in seen:
            continue
        if str(item.get("kind") or "") != spec["kind"]:
            continue
        metric = str(item.get("metric") or "")
        artifact_id = str(item.get("artifact_id") or "")
        if template in {"metric_dot_interval", "metric_strip", "metric_bar", "metric_boxplot", "metric_violin", "seed_trajectory"} and metric not in catalog.get("metrics", []):
            continue
        if template == "pareto_scatter" and (str(item.get("x_metric") or "") not in catalog.get("timing_metrics", []) or str(item.get("y_metric") or "") not in catalog.get("metrics", [])):
            continue
        experiment_id = str(item.get("experiment_id") or "").strip()
        if template == "stage3_result_figure":
            supplied = {
                str(value.get("id")): value for value in catalog.get("stage3_figure_artifacts") or []
                if isinstance(value, dict)
            }
            selected = supplied.get(artifact_id)
            if not selected:
                continue
            experiment_id = str(selected.get("experiment_id") or "")
        if template in _EMPIRICAL_TEMPLATES:
            if not experiment_id and len(catalog.get("experiment_ids") or []) == 1:
                experiment_id = str(catalog["experiment_ids"][0])
            if not experiment_id or experiment_id not in (catalog.get("experiment_scopes") or {}):
                continue
        # Repeat-dependent templates must be checked within their own
        # experiment/metric scope.  A repeated run elsewhere in the paper, or
        # several different metrics emitted by one run, must not unlock an
        # uncertainty or seed plot for this asset.
        if template in _REPEAT_RUN_TEMPLATES:
            scope_metric = metric or str(item.get("y_metric") or "")
            if not _has_repeatability(
                catalog, experiment_id, scope_metric,
                require_distinct_seeds=template in {"seed_trajectory", "per_seed_results_table"},
            ):
                continue
        # A model-supplied claim ID is only valid for the same experiment as
        # the visual.  Filter cross-experiment IDs before manifest creation;
        # otherwise an attractive but unrelated chart can poison the evidence
        # graph and fail only after all writing work has completed.
        claim_ids = [
            str(value) for value in item.get("claim_ids", [])
            if str(value) in known_claims
            and str(claim_records[str(value)].get("experiment_id") or "") == experiment_id
        ]
        if template in _EMPIRICAL_TEMPLATES and not claim_ids:
            claim_ids = matching_claims(experiment_id, metric or str(item.get("y_metric") or ""))
        purpose, caption = truthful_text(
            str(item.get("purpose") or "Evidence-backed scientific visual."),
            str(item.get("caption") or "Evidence-backed result."), claim_ids,
        )
        seen.add(asset_id)
        normalized.append({
            "id": asset_id, "kind": spec["kind"], "template": template,
            "purpose": purpose,
            "section": str(item.get("section") or ("method" if "flow" in template else "experiments")),
            "placement_after_paragraph_id": str(item.get("placement_after_paragraph_id") or ""),
            "claim_ids": claim_ids,
            "experiment_id": experiment_id or None, "metric": metric or None,
            "metrics": _main_table_metrics(catalog, experiment_id, item.get("metrics")) if template == "main_metrics_table" else [],
            "x_metric": str(item.get("x_metric") or "") or None,
            "y_metric": str(item.get("y_metric") or "") or None,
            "artifact_id": artifact_id or None,
            "caption": caption,
        })
    # A non-empty but invalid LLM response must not leave the workflow without
    # visuals that can be generated safely from its verified evidence.
    normalized = normalized or default_visual_plan(catalog)
    # Empirical run-level comparisons require a compact numerical anchor in
    # addition to plots.  It prevents prose such as "Table 1" from becoming a
    # dangling reference and gives readers exact aggregates behind a chart.
    if (catalog.get("supported_templates") or {}).get("main_metrics_table", {}).get("available") and not any(item["kind"] == "table" for item in normalized):
        primary = _primary_metric(catalog)
        if primary:
            experiment_id = _default_experiment_id(catalog, {primary})
            if experiment_id:
                normalized.append({"id": "main_metrics_table", "kind": "table", "template": "main_metrics_table", "purpose": "Report exact verified aggregate results.", "section": "experiments", "placement_after_paragraph_id": "", "claim_ids": matching_claims(experiment_id, primary), "experiment_id": experiment_id, "metric": primary, "metrics": _main_table_metrics(catalog, experiment_id), "x_metric": None, "y_metric": None, "caption": f"Aggregate efficacy metrics from verified runs in experiment {experiment_id}."})
    # Captions are part of the generated-asset contract, not free-form prose.
    # The deterministic renderers below always draw these uncertainty values
    # and table columns, so include the same facts in the replayable plan the
    # visual reviewer sees.  This prevents a planner from accidentally making
    # the plan and the rendered manifest disagree about what readers can infer.
    for asset in normalized:
        experiment_id = str(asset.get("experiment_id") or "")
        template = str(asset.get("template") or "")
        caption = str(asset.get("caption") or "")
        # A method diagram records implementation structure.  It never proves
        # an empirical claim, even if a planner happened to attach one.
        if template not in _EMPIRICAL_TEMPLATES:
            asset["claim_ids"] = []
            asset["purpose"] = "Explain recorded method components; this schematic is not empirical evidence."
        # Empirical captions are generated from the template's data contract.
        # Do not let an LLM turn a numerical display into a threshold, novelty,
        # or hypothesis conclusion; those claims belong in evidence-checked
        # prose that cites the asset.
        method_counts = ((catalog.get("experiment_scopes") or {}).get(experiment_id) or {}).get("method_run_counts") or {}
        run_count_note, uncertainty_note = _run_count_caption(method_counts)
        if template == "metric_dot_interval" and experiment_id:
            caption = (
                f"Observed {asset.get('metric')} across verified runs in experiment {experiment_id}. "
                f"Dots are individual verified runs and the summary marker is the arithmetic mean. "
                f"{uncertainty_note} {run_count_note}"
            )
        elif template == "metric_bar" and experiment_id:
            caption = (
                f"Arithmetic mean {asset.get('metric')} from verified runs in experiment {experiment_id}. "
                f"{uncertainty_note} {run_count_note}"
            )
        elif template == "pareto_scatter" and experiment_id:
            positive_counts = [int(value) for value in method_counts.values() if str(value).isdigit() and int(value) > 0]
            if positive_counts and all(count == 1 for count in positive_counts):
                caption = (
                    f"Single-run efficiency trade-off in experiment {experiment_id}: "
                    f"x = {asset.get('x_metric')} and y = {asset.get('y_metric')}. "
                    "Each point is the single verified observation for one method; no averaging or uncertainty interval is shown. "
                    f"{run_count_note}"
                )
            else:
                caption = (
                    f"Within-method efficiency trade-off in experiment {experiment_id}: "
                    f"x = {asset.get('x_metric')} and y = {asset.get('y_metric')}. "
                    "Each point is the arithmetic mean of x/y records joined within that method by run_id; methods are not paired to one another. "
                    f"No uncertainty interval is shown. {run_count_note}"
                )
        elif template == "main_metrics_table" and experiment_id:
            positive_counts = [int(value) for value in method_counts.values() if str(value).isdigit() and int(value) > 0]
            uncertainty_text = (
                "Population standard deviation is reported only for methods with at least two verified runs; "
                "it is marked not estimable for a single run."
                if any(count > 1 for count in positive_counts)
                else "Every method has one verified run, so population standard deviation is not estimable."
            )
            caption = (
                f"Verified aggregate metrics ({', '.join(asset.get('metrics') or [asset.get('metric')])}) for experiment {experiment_id}. "
                f"For each shown metric, the table reports mean, standard-deviation availability, and run count. "
                f"{uncertainty_text} {run_count_note}"
            )
        limited = [
            f"{claim_id}={claim_status.get(claim_id)}"
            for claim_id in asset.get("claim_ids") or []
            if claim_status.get(claim_id) in {"not_supported", "inconclusive"}
        ]
        if limited:
            caption = _append_caption_note(
                caption,
                f"Evidence for {', '.join(limited)}; the visual reports the observed result and does not establish the planned direction.",
            )
        asset["caption"] = caption
    return normalized


def _select_rows(rows: list[dict[str, Any]], metric: str | None, experiment_id: str | None) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if (metric is None or row["metric"] == metric)
        and experiment_id is not None
        and str(row.get("experiment_id") or "") == experiment_id
    ]


def _paired_metric_rows(
    rows: list[dict[str, Any]],
    experiment_id: str | None,
    x_metric: str | None,
    y_metric: str | None,
) -> list[dict[str, Any]]:
    """Return matched metric pairs without silently dropping incomplete runs."""
    if not experiment_id or not x_metric or not y_metric:
        raise ValueError("paired plot requires experiment_id, x_metric, and y_metric")
    grouped: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for row in rows:
        if str(row.get("experiment_id") or "") != experiment_id:
            continue
        if row["metric"] not in {x_metric, y_metric}:
            continue
        run_id = str(row.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("paired plot requires a non-empty run_id for every selected record")
        key = (row["method"], run_id)
        if row["metric"] in grouped[key]:
            raise ValueError(
                f"paired plot found duplicate {row['metric']} for method={row['method']} run_id={run_id}"
            )
        grouped[key][row["metric"]] = row["value"]

    if not grouped:
        raise ValueError("paired performance/timing records are unavailable")
    incomplete = [
        f"{method}/{run_id}" for (method, run_id), values in grouped.items()
        if x_metric not in values or y_metric not in values
    ]
    if incomplete:
        raise ValueError(
            "paired plot has incomplete metric records for "
            + ", ".join(sorted(incomplete)[:8])
        )
    return [
        {"method": method, "run_id": run_id, "x": values[x_metric], "y": values[y_metric]}
        for (method, run_id), values in sorted(grouped.items())
    ]


def _append_caption_note(caption: str, note: str) -> str:
    normalized = caption.strip()
    if note.casefold() in normalized.casefold():
        return normalized
    return f"{normalized} {note}".strip()


def _save_snapshot(root: Path, asset_id: str, payload: Any) -> str:
    path = root / "assets" / "data" / f"{asset_id}.json"; path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _figure_paths(root: Path, asset_id: str) -> tuple[Path, Path]:
    directory = root / "assets" / "figures"; directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{asset_id}.pdf", directory / f"{asset_id}.png"


def _render_figure(root: Path, asset: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    if asset["template"] == "stage3_result_figure":
        return _copy_stage3_result_figure(root, asset, inputs)
    try:
        import matplotlib
        # Writing runs are headless batch jobs. Selecting Agg before pyplot
        # avoids a host-dependent Tk import turning valid data into a fake
        # "blocked_missing_data" asset.
        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt
    except Exception as exc:
        return {**asset, "status": "blocked_renderer_unavailable", "warning": str(exc)}
    template = asset["template"]; rows = metric_rows(inputs); metric = asset.get("metric"); experiment_id = asset.get("experiment_id")
    pdf_path, png_path = _figure_paths(root, asset["id"])
    svg_path = pdf_path.with_suffix(".svg")
    display_label_map: dict[str, str] = {}
    try:
        with plt.rc_context(_publication_rc()):
          if template in {"method_protocol_flow", "model_architecture_flow", "algorithm_flow"}:
            components = ((inputs.get("m2") or {}).get("method_design") or {}).get("components") or []
            if not components:
                raise ValueError("method flow has no recorded components")
            # A single recorded component is still a valid schema-level flow
            # when its recorded input/output contracts are available. Rendering
            # it alone as a wide, one-node "pipeline" is visually misleading.
            if len(components) == 1:
                component = components[0]
                nodes = [
                    {"label": str(component.get("input_schema") or "Recorded input"), "role": "input"},
                    {"label": str(component.get("name") or "Recorded component"), "role": "component"},
                    {"label": str(component.get("output_schema") or "Recorded output"), "role": "output"},
                ]
            else:
                nodes = [
                    {"label": str(component.get("name") or f"Component {index + 1}"), "role": "component"}
                    for index, component in enumerate(components)
                ]
            from matplotlib.patches import FancyBboxPatch
            full_labels = [str(node["label"]) for node in nodes]
            short_labels, display_label_map = _flow_display_labels(full_labels)
            count = len(nodes)
            # Keep the canvas close to the actual workflow.  A tall blank
            # band around a horizontal diagram becomes a conspicuous LaTeX
            # page gap even with bbox_inches='tight'.
            fig, ax = plt.subplots(figsize=(7.2, 1.78)); ax.set_axis_off()
            ax.set_xlim(0, 1); ax.set_ylim(0, 1)
            box_w = min(.165, .72 / max(count, 1)); box_h = .34; bottom = .29
            # Keep the complete rounded stroke inside the axes.  The previous
            # fixed endpoint centres let a wide first/last card extend beyond
            # x=0/1, so Matplotlib clipped the outside half of its border.
            border_safe_pad = .038
            left_margin = border_safe_pad + box_w / 2
            right_margin = 1 - border_safe_pad - box_w / 2
            centers = ([.5] if count == 1 else
                       [left_margin + index * (right_margin - left_margin) / (count - 1) for index in range(count)])
            semantic_colors = {
                "adapted": ("#247C91", "#EDF6F8"),
                "standard": ("#6F5A9A", "#F3F0F8"),
                "novel": ("#B87522", "#FBF4E8"),
                "input": ("#4477AA", "#EDF3F8"),
                "output": ("#25845F", "#EDF7F2"),
                "component": ("#526E7A", "#F0F4F5"),
            }
            ax.text(.04, .94, "METHOD WORKFLOW", transform=ax.transAxes, ha="left", va="center",
                    fontsize=7.0, weight="bold", color="#56636D")
            for index, (node, short, x) in enumerate(zip(nodes, short_labels, centers)):
                component = components[index] if index < len(components) else {}
                stage_kind = str(component.get("novelty_degree") or node.get("role") or "component").casefold()
                accent, tint = semantic_colors.get(stage_kind, semantic_colors["component"])
                left = x - box_w / 2
                card = FancyBboxPatch((left, bottom), box_w, box_h,
                                      boxstyle="round,pad=0.012,rounding_size=0.025",
                                      transform=ax.transAxes, facecolor=tint, edgecolor=accent,
                                      linewidth=1.0, zorder=2)
                ax.add_patch(card)
                ax.text(x, bottom + box_h + .045, f"STEP {index + 1}", transform=ax.transAxes,
                        ha="center", va="center", fontsize=5.0, weight="bold", color=accent, zorder=5)
                wrapped = "\n".join(textwrap.wrap(short, width=17, break_long_words=False,
                                                  break_on_hyphens=False)[:2])
                ax.text(x, bottom + .17, wrapped, transform=ax.transAxes, ha="center", va="center",
                        fontsize=7.1, weight="bold", color="#21313B", linespacing=1.18, zorder=3)
                ax.text(x, bottom + .055, stage_kind.upper(), transform=ax.transAxes, ha="center", va="center",
                        fontsize=5.0, weight="bold", color=accent, zorder=3)
                if index + 1 < count:
                    ax.annotate("", xy=(centers[index + 1] - box_w / 2 - .012, .505),
                                xytext=(x + box_w / 2 + .012, .505), xycoords=ax.transAxes,
                                arrowprops={"arrowstyle": "-|>", "color": "#7C8992", "lw": 1.0,
                                            "shrinkA": 0, "shrinkB": 0})
            note = _abbreviation_note(display_label_map)
            if note:
                ax.text(.5, .09, "Display labels are shortened; exact component names are defined in the caption.",
                        transform=ax.transAxes, ha="center", va="center", fontsize=5.8,
                        color="#66727B")
                asset = {**asset, "caption": _append_caption_note(str(asset["caption"]), note)}
            snapshot = {"components": components, "nodes": nodes, "edges": max(0, len(nodes) - 1)}
          elif template == "metric_dot_interval":
            selected = _select_rows(rows, metric, experiment_id); groups: dict[str, list[float]] = defaultdict(list)
            for row in selected: groups[row["method"]].append(row["value"])
            if not groups: raise ValueError(f"no verified rows for {metric}")
            methods = sorted(groups, key=lambda name: fmean(groups[name]))
            means = [fmean(groups[m]) for m in methods]; errors = [pstdev(groups[m]) if len(groups[m]) > 1 else 0 for m in methods]
            repeated = any(len(groups[m]) > 1 for m in methods)
            fig, ax = plt.subplots(figsize=(7.2, max(2.7, 1.25 + .48 * len(methods)))); y = list(range(len(methods)))
            for index, method in enumerate(methods):
                ax.scatter([*groups[method]], [index] * len(groups[method]), color="#A8C6C1", alpha=.55, s=24, zorder=2)
            ax.errorbar(means, y, xerr=errors if repeated else None, fmt="o", color="#167C71", capsize=3, zorder=3)
            labels, display_label_map = _abbreviated_labels(methods, limit=26)
            ax.set_yticks(y, labels); ax.set_xlabel(_display_label(metric)); _style_axis(ax, grid_axis="x"); snapshot = selected
            run_note, uncertainty_note = _run_count_caption({method: len(values) for method, values in groups.items()})
            caption = _append_caption_note(str(asset["caption"]), uncertainty_note)
            if run_note:
                caption = _append_caption_note(caption, run_note)
            asset = {
                **asset,
                "caption": caption,
                "uncertainty": ({
                    "statistic": "population_stddev",
                    "applies_to_methods_with_minimum_runs": 2,
                    "experiment_id": experiment_id,
                } if repeated else None),
                "mark_encoding": {
                    "raw_points": "individual verified runs",
                    "summary_marker": "arithmetic mean for each method",
                    "error_bars": "population standard deviation where a method has at least two verified runs" if repeated else "none",
                },
            }
          elif template in {"metric_boxplot", "metric_violin", "metric_bar", "metric_strip"}:
            selected = _select_rows(rows, metric, experiment_id); groups: dict[str, list[float]] = defaultdict(list)
            for row in selected: groups[row["method"]].append(row["value"])
            if not groups: raise ValueError(f"no verified rows for {metric}")
            methods = sorted(groups, key=lambda name: fmean(groups[name]))
            values = [groups[method] for method in methods]
            labels, display_label_map = _abbreviated_labels(methods, limit=26)
            fig, ax = plt.subplots(figsize=(7.2, max(2.8, 1.3 + .48 * len(methods))))
            if template == "metric_boxplot":
                ax.boxplot(values, vert=False, tick_labels=labels, showmeans=True, meanprops={"marker": "D", "markerfacecolor": "#167C71", "markeredgecolor": "#167C71"})
                for index, group in enumerate(values, start=1): ax.scatter(group, [index] * len(group), color="#A8C6C1", alpha=.65, zorder=3)
            elif template == "metric_violin":
                ax.violinplot(values, positions=range(len(methods)), vert=False, showmeans=True, showextrema=True)
                ax.set_yticks(range(len(methods)), labels)
            elif template == "metric_bar":
                means = [fmean(group) for group in values]; errors = [pstdev(group) if len(group) > 1 else 0 for group in values]
                repeated = any(len(group) > 1 for group in values)
                colors = ["#AEB8C2"] * len(methods)
                best = max(range(len(means)), key=means.__getitem__)
                colors[best] = "#159D8E"
                bars = ax.barh(range(len(methods)), means, xerr=errors if repeated else None, capsize=3,
                               color=colors, edgecolor="white", linewidth=.7)
                ax.set_yticks(range(len(methods)), labels)
                span = max(means) - min(means) if len(means) > 1 else abs(means[0])
                pad = max(span * .05, abs(max(means)) * .012, .005)
                for bar, value in zip(bars, means):
                    ax.text(value + pad, bar.get_y() + bar.get_height() / 2, f"{value:.3f}", va="center", fontsize=7.2, color="#34424D")
            else:
                for index, group in enumerate(values): ax.scatter(group, [index] * len(group), color="#167C71", alpha=.75)
                ax.set_yticks(range(len(methods)), labels)
            ax.set_xlabel(_display_label(metric)); _style_axis(ax, grid_axis="x"); snapshot = selected
            if template == "metric_bar":
                run_note, uncertainty_note = _run_count_caption({method: len(group) for method, group in groups.items()})
                caption = _append_caption_note(str(asset["caption"]), uncertainty_note)
                if run_note:
                    caption = _append_caption_note(caption, run_note)
                asset = {
                    **asset,
                    "caption": caption,
                    "uncertainty": ({
                        "statistic": "population_stddev",
                        "applies_to_methods_with_minimum_runs": 2,
                        "experiment_id": experiment_id,
                    } if repeated else None),
                    "mark_encoding": {
                        "bars": "arithmetic mean for each method",
                        "error_bars": "population standard deviation where a method has at least two verified runs" if repeated else "none",
                    },
                }
          elif template == "seed_trajectory":
            selected = _select_rows(rows, metric, experiment_id); groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in selected: groups[row["method"]].append(row)
            if not groups or any(row["seed"] is None for row in selected): raise ValueError("per-seed rows are unavailable")
            fig, ax = plt.subplots(figsize=(7.2, 3.8))
            ordered = sorted(groups.items())
            labels, display_label_map = _abbreviated_labels([item[0] for item in ordered], limit=24)
            for index, ((method, values), label) in enumerate(zip(ordered, labels)):
                values.sort(key=lambda row: row["seed"]); ax.plot([row["seed"] for row in values], [row["value"] for row in values], marker="o", lw=1.5, color=_PALETTE[index % len(_PALETTE)], label=label)
            ax.set_xlabel("Random seed"); ax.set_ylabel(_display_label(metric)); _style_axis(ax, grid_axis="y"); ax.legend(fontsize=7, ncol=min(3, len(ordered)), loc="upper center", bbox_to_anchor=(.5, -0.22)); snapshot = selected
          elif template == "pareto_scatter":
            x_metric, y_metric = asset.get("x_metric"), asset.get("y_metric")
            paired = _paired_metric_rows(rows, experiment_id, x_metric, y_metric)
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for pair in paired:
                grouped[pair["method"]].append(pair)
            points = [
                {
                    "method": method,
                    "x": fmean(pair["x"] for pair in values),
                    "y": fmean(pair["y"] for pair in values),
                    "paired_run_count": len(values),
                    "run_ids": [pair["run_id"] for pair in values],
                }
                for method, values in sorted(grouped.items())
            ]
            if not points: raise ValueError("no paired performance and efficiency rows")
            fig, ax = plt.subplots(figsize=(7.2, 4.2))
            labels, display_label_map = _abbreviated_labels([point["method"] for point in points], limit=22)
            for index, (point, label) in enumerate(zip(points, labels), start=1):
                color = _PALETTE[(index - 1) % len(_PALETTE)]
                ax.scatter(point["x"], point["y"], s=52, color=color, edgecolors="white", linewidths=.8, zorder=3, label=f"{index}  {label}")
                ax.annotate(str(index), (point["x"], point["y"]), xytext=(0, 7), textcoords="offset points", ha="center", fontsize=7, weight="bold", color="#34424D")
            ax.set_xlabel(_display_label(x_metric)); ax.set_ylabel(_display_label(y_metric)); _style_axis(ax, grid_axis="both")
            ax.legend(ncol=2 if len(points) > 3 else 1, loc="upper center", bbox_to_anchor=(.5, -0.2), fontsize=6.8, handletextpad=.4, columnspacing=1.2)
            snapshot = points
            paired_count_note = "; ".join(
                f"{point['method']}={point['paired_run_count']}" for point in points
            )
            single_run = all(point["paired_run_count"] == 1 for point in points)
            if single_run:
                caption = (
                    f"Single-run efficiency trade-off in experiment {experiment_id}: "
                    f"x = {x_metric} and y = {y_metric}. Each point is the single verified "
                    "x/y observation for one method; no averaging or uncertainty interval is shown. "
                    f"Runs per method: {paired_count_note}."
                )
            else:
                caption = (
                    f"Within-method efficiency trade-off in experiment {experiment_id}: "
                    f"x = {x_metric} and y = {y_metric}. Each point is the arithmetic mean of "
                    "x/y records joined within that method by run_id; methods are not paired to one another. "
                    f"No uncertainty interval is shown. Paired runs per method: {paired_count_note}."
                )
            asset = {
                **asset,
                "caption": caption,
                "uncertainty": None,
                "mark_encoding": {
                    "x": (f"single observed {x_metric}" if single_run else f"within-method arithmetic mean of {x_metric}"),
                    "y": (f"single observed {y_metric}" if single_run else f"within-method arithmetic mean of {y_metric}"),
                    "points": ("one verified run per method" if single_run else "one within-method aggregate"),
                    "error_bars": "none",
                    "labels": "numeric keys linked to the display-label legend",
                },
                "pairing": {
                    "experiment_id": experiment_id,
                    "x_metric": x_metric,
                    "y_metric": y_metric,
                    "key": ["method", "run_id"],
                    "aggregation": "single_observation" if single_run else "within_method_arithmetic_mean",
                    "matched_across_methods": False,
                    # The source snapshot contains all coordinates, but the
                    # manifest must also declare precisely which run records
                    # were joined for every displayed point.  This applies to
                    # any paired two-metric plot, not a task-specific name.
                    "coordinate_records": [
                        {
                            "method": point["method"],
                            "run_ids": list(point["run_ids"]),
                            "paired_run_count": point["paired_run_count"],
                            "x_metric": x_metric,
                            "y_metric": y_metric,
                        }
                        for point in points
                    ],
                },
            }
          else:
            raise ValueError(f"renderer for template {template} requires its declared Module 3 data contract")
          fig.tight_layout(pad=0.45)
          # Use a small, fixed export padding.  It keeps strokes intact while
          # avoiding the large white frame that a presentation-sized source
          # figure can otherwise carry into the manuscript.
          export = {"bbox_inches": "tight", "pad_inches": 0.025}
          fig.savefig(svg_path, metadata={"Date": None, "Creator": "JiuwenSwarm Writing"}, **export)
          fig.savefig(pdf_path, metadata={"CreationDate": None, "ModDate": None, "Creator": "JiuwenSwarm Writing"}, **export)
          fig.savefig(png_path, dpi=300, metadata={"Software": "JiuwenSwarm Writing"}, **export)
          plt.close(fig)
        snapshot_path = _save_snapshot(root, asset["id"], snapshot)
        if display_label_map and template not in {"method_protocol_flow", "model_architecture_flow", "algorithm_flow"}:
            asset = {**asset, "caption": _append_caption_note(str(asset["caption"]), _abbreviation_note(display_label_map))}
        return {**asset, "status": "available", "label": f"fig:{asset['id']}", "path": str(pdf_path), "editable_path": str(svg_path), "preview_path": str(png_path), "sha256": _digest(pdf_path), "source": snapshot_path, "source_sha256": _digest(Path(snapshot_path)), "display_label_map": display_label_map, "generator": "scripts/visuals.py"}
    except Exception as exc:
        return {**asset, "status": "blocked_missing_data", "warning": str(exc)}


def _copy_stage3_result_figure(
    root: Path,
    asset: dict[str, Any],
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Copy an inventory-verified, single-experiment Module 3 figure."""
    eligible, _ = _auditable_stage3_figures(inputs)
    selected = next(
        (item for item in eligible if str(item.get("id")) == str(asset.get("artifact_id"))),
        None,
    )
    if selected is None:
        return {**asset, "status": "blocked_missing_data", "warning": "stage-three figure is not auditable"}
    try:
        m3 = inputs.get("m3") if isinstance(inputs.get("m3"), dict) else {}
        artifact_root = Path(str(m3.get("artifact_root") or "")).resolve()
        source = (artifact_root / Path(str(selected["path"]))).resolve()
        destination_dir = root / "assets" / "figures"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{asset['id']}{source.suffix.casefold()}"
        shutil.copy2(source, destination)
        return {
            **asset,
            "status": "available",
            "label": f"fig:{asset['id']}",
            "path": str(destination),
            "sha256": _digest(destination),
            "source": str(source),
            "source_sha256": _digest(source),
            "source_experiment_ids": selected.get("source_experiment_ids") or [],
            "display_contract": {
                "displayed_metrics": selected.get("displayed_metrics") or [],
                "caption_assertions": selected.get("caption_assertions") or [],
                "display_label_map": selected.get("display_label_map") or {},
            },
            "provenance_status": "artifact_manifest_and_scope_verified",
            "generator": "module3_artifact_copy",
        }
    except (OSError, ValueError) as exc:
        return {**asset, "status": "blocked_missing_data", "warning": str(exc)}


_TEX_ESCAPE = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def _tex(value: object) -> str:
    """Escape LaTeX special characters in one pass without re-escaping."""
    return "".join(_TEX_ESCAPE.get(character, character) for character in str(value))


def _compact_result_table_caption(
    asset: dict[str, Any], metrics: list[str], grouped: dict[str, dict[str, list[float]]],
) -> str:
    """Keep result-table captions informative without duplicating the Results.

    Planner outputs often enumerate every method and every count in a caption.
    That prose belongs in the Results paragraph; on a narrow conference page it
    makes the table less readable without adding provenance.  Each cell already
    records mean / SD / n, while the manifest retains exact run identifiers.
    """
    experiment_id = str(asset.get("experiment_id") or "the registered experiment")
    labels = ", ".join(_display_label(metric) for metric in metrics)
    counts = [len(grouped[method][metric]) for method in grouped for metric in metrics]
    if counts and all(count == 1 for count in counts):
        uncertainty = "All displayed aggregates are single verified runs, so population SD is not estimable."
    else:
        uncertainty = "Cells report mean / population SD / verified-run count."
    return f"Verified aggregate results for {experiment_id} ({labels}). {uncertainty}"


def _render_table(root: Path, asset: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    rows = metric_rows(inputs); template = asset["template"]; experiment_id = asset.get("experiment_id"); path = root / "assets" / "tables" / f"{asset['id']}.tex"; path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if template == "main_metrics_table":
            metrics = [str(metric) for metric in asset.get("metrics") or [] if str(metric)] or [str(asset.get("metric") or "")]
            selected = [row for metric in metrics for row in _select_rows(rows, metric, experiment_id)]
            grouped: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
            for row in selected:
                grouped[row["method"]][row["metric"]].append(row["value"])
            if not grouped or any(not grouped[method].get(metric) for method in grouped for metric in metrics):
                raise ValueError(f"no complete verified rows for main-table metrics: {', '.join(metrics)}")
            methods = sorted(grouped)
            display_methods, display_label_map = _abbreviated_labels(methods, limit=24)
            method_labels = dict(zip(methods, display_methods))
            def aggregate_cell(values: list[float]) -> str:
                standard_deviation = f"{pstdev(values):.4f}" if len(values) > 1 else r"\textemdash{}"
                return f"{fmean(values):.4f} / {standard_deviation} / {len(values)}"
            caption = _compact_result_table_caption(asset, metrics, grouped)
            caption = _append_caption_note(caption, _abbreviation_note(display_label_map))
            # Never use resizebox here: shrinking a many-column table can
            # silently make its text smaller than the manuscript's readable
            # table font.  Long method names are already abbreviated and each
            # metric cell is deliberately compact; wrap the method column and
            # keep a stable footnote-size floor instead.
            # A table wider than three result metrics is rendered as compact
            # vertical panels.  This is content-agnostic and retains every
            # metric, unlike a resizebox that makes a five- or six-column
            # result table unreadable or lets it run into the page margin.
            metric_panels = [metrics[index:index + 3] for index in range(0, len(metrics), 3)]
            def tabular_panel(panel: list[str], *, continued: bool) -> str:
                method_width = "0.29\\linewidth" if len(panel) >= 3 else "0.36\\linewidth"
                metric_width = "0.21\\linewidth" if len(panel) >= 3 else "0.28\\linewidth"
                alignment = "p{" + method_width + "}" + ("p{" + metric_width + "}") * len(panel)
                header = " & ".join([
                    "Method",
                    *[_tex(_display_label(metric)) + r" \shortstack{mean / SD / n}" for metric in panel],
                ])
                body = "\n".join(
                    _tex(method_labels[method]) + " & " + " & ".join(
                        aggregate_cell(grouped[method][metric]) for metric in panel
                    ) + " \\\\" for method in methods
                )
                lead = "\\vspace{3pt}\\\\\\textit{Continued metrics.}\\\\\n" if continued else ""
                return lead + "\\begin{tabular}{" + alignment + "}\n\\toprule\n" + header + " \\\\ \n\\midrule\n" + body + "\n\\bottomrule\n\\end{tabular}\n"
            panels = "".join(tabular_panel(panel, continued=index > 0) for index, panel in enumerate(metric_panels))
            content = (
                # Generated tables are inserted at the paragraph that cites
                # them.  A normal `[t]` float is allowed to leap over that
                # paragraph (and, near a page boundary, can leave an almost
                # empty preceding page).  Keep this bounded, single-column
                # table together with its caption at the declared anchor.
                "\\begin{table}[H]\n\\centering\n\\footnotesize\n"
                "\\caption{" + _tex(caption) + "}\n\\label{tab:" + asset["id"] + "}\n"
                "\\setlength{\\tabcolsep}{3pt}\n\\renewcommand{\\arraystretch}{1.12}\n"
                + panels + "\\end{table}\n"
            ); snapshot = selected
            asset = {**asset, "caption": caption, "display_label_map": display_label_map}
        elif template == "experiment_protocol_table":
            plan = ((inputs.get("m2") or {}).get("experiment_plan") or {}); datasets = ", ".join(str(item.get("name")) for item in plan.get("datasets") or [] if isinstance(item, dict)) or "Not recorded"; metrics = ", ".join(str(value) for value in plan.get("metrics") or []) or "Not recorded"; methods = ", ".join(sorted({row["method"] for row in rows})) or "Not recorded"
            # This protocol table is intentionally a non-splitting float: a
            # caption separated from its rows is harder to read than moving
            # the complete table to the next page.  Long tables require a
            # dedicated longtable renderer rather than being silently split.
            content = "\\begin{table}[H]\n\\centering\n\\small\n\\caption{" + _tex(asset["caption"]) + "}\n\\label{tab:" + asset["id"] + "}\n\\setlength{\\tabcolsep}{3pt}\n\\begin{tabular}{p{0.16\\linewidth}p{0.76\\linewidth}}\n\\toprule\nItem & Value \\\\ \n\\midrule\nDataset(s) & " + _tex(datasets) + " \\\\ \nMethods & " + _tex(methods) + " \\\\ \nMetrics & " + _tex(metrics) + " \\\\ \n\\bottomrule\n\\end{tabular}\n\\end{table}\n"; snapshot = {"datasets": datasets, "methods": methods, "metrics": metrics}
        else:
            raise ValueError(f"renderer for template {template} requires its declared Module 3 data contract")
        path.write_text(content, encoding="utf-8"); snapshot_path = _save_snapshot(root, asset["id"], snapshot)
        return {**asset, "status": "available", "label": f"tab:{asset['id']}", "path": str(path), "sha256": _digest(path), "source": snapshot_path, "source_sha256": _digest(Path(snapshot_path)), "generator": "scripts/visuals.py"}
    except Exception as exc:
        return {**asset, "status": "blocked_missing_data", "warning": str(exc)}


def materialize_visual_assets(root: Path, inputs: dict[str, Any], plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    (root / "assets" / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__), root / "assets" / "scripts" / "visuals.py")
    return [_render_figure(root, item, inputs) if item["kind"] == "figure" else _render_table(root, item, inputs) for item in plan]
