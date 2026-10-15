"""Deterministic, provenance-carrying writing assets."""
from __future__ import annotations

import hashlib
import json
import shutil
import textwrap
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def materialize_method_schematic(root: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    figure_dir = root / "assets" / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    components = (inputs["m2"].get("method_design") or {}).get("components") or []
    asset: dict[str, Any] = {"id": "method_overview", "kind": "figure", "label": "fig:method_overview", "status": "blocked_missing_data", "path": None, "source": "m2.method_design.components"}
    if not components:
        return asset
    try:
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(7.2, 2.1)); ax.set_axis_off(); count = len(components)
        for index, component in enumerate(components):
            name = textwrap.fill(str(component.get("name") or f"Component {index + 1}"), width=14)
            x = (index + .5) / count
            ax.text(x, .5, name, ha="center", va="center", fontsize=7.5, bbox={"boxstyle": "round,pad=0.35", "fc": "#E8F1FA", "ec": "#34699A"}, transform=ax.transAxes)
            if index + 1 < count:
                ax.annotate("", xy=((index + 1) / count - .02, .5), xytext=((index + .5) / count + .08, .5), xycoords=ax.transAxes, arrowprops={"arrowstyle": "->", "color": "#34699A"})
        path = figure_dir / "method_overview.pdf"; fig.savefig(path, bbox_inches="tight"); plt.close(fig)
        asset.update({"status": "available", "path": str(path), "sha256": _digest(path), "caption": "Overview of the proposed method components."})
    except Exception as exc:
        asset["warning"] = f"method schematic unavailable: {exc}"
    return asset


def materialize_verified_result_assets(root: Path, inputs: dict[str, Any]) -> list[dict[str, Any]]:
    """Copy one supplied result figure and derive one table from traceable runs."""
    m3 = inputs.get("m3") if isinstance(inputs.get("m3"), dict) else {}
    results = m3.get("experiment_results") or {}
    asset_root = Path(str(m3.get("artifact_root") or ""))
    figures_dir, tables_dir = root / "assets" / "figures", root / "assets" / "tables"
    figures_dir.mkdir(parents=True, exist_ok=True); tables_dir.mkdir(parents=True, exist_ok=True)
    assets: list[dict[str, Any]] = []
    for raw in results.get("figure_artifacts") or []:
        if not isinstance(raw, dict) or not raw.get("path"):
            continue
        relative = Path(str(raw["path"])); source = asset_root / relative
        if source.suffix.lower() == ".svg":
            source = source.with_suffix(".pdf")
        if source.is_file() and source.suffix.lower() == ".pdf":
            destination = figures_dir / "main_results.pdf"; shutil.copy2(source, destination)
            assets.append({"id": "main_results_figure", "kind": "figure", "label": "fig:main_results", "status": "available", "path": str(destination), "sha256": _digest(destination), "source": str(source), "caption": "Accuracy comparisons across repeated verified runs."})
            break
    grouped: dict[str, list[float]] = defaultdict(list)
    for run in results.get("experiment_runs") or []:
        if not isinstance(run, dict) or not run.get("success"):
            continue
        for metric in run.get("metrics") or []:
            if isinstance(metric, dict) and metric.get("name") == "accuracy" and isinstance(metric.get("value"), (int, float)):
                grouped[str(run.get("method") or "unknown")].append(float(metric["value"]))
    if grouped:
        row_values = []
        for method, values in sorted(grouped.items()):
            escaped_method = method.replace("_", r"\_")
            row_values.append(f"{escaped_method} & {fmean(values):.4f} & {len(values)}" + r" \\")
        rows = "\n".join(row_values)
        table_path = tables_dir / "main_results.tex"
        table_path.write_text("\\begin{table}[t]\n\\centering\n\\caption{Accuracy from verified completed runs.}\n\\label{tab:main_results}\n\\begin{tabular}{lrr}\n\\toprule\nMethod & Mean accuracy & Runs \\\\ \n\\midrule\n" + rows + "\n\\bottomrule\n\\end{tabular}\n\\end{table}\n", encoding="utf-8")
        assets.append({"id": "main_results_table", "kind": "table", "label": "tab:main_results", "status": "available", "path": str(table_path), "sha256": _digest(table_path), "source": "m3.experiment_results.experiment_runs", "caption": "Accuracy from verified completed runs."})
    return assets


def write_asset_manifest(
    output_dir: str | Path, inputs: dict[str, Any], *, visual_plan: list[dict[str, Any]] | None = None,
    capability_catalog: dict[str, Any] | None = None,
) -> str:
    """Materialize a planner-approved, evidence-backed visual manifest.

    The legacy fixed three-asset behavior deliberately no longer applies: each
    paper receives a plan based on its own available evidence and can grow as
    Module 3 exposes richer, typed visual data.
    """
    from scripts.visuals import build_visual_capability_catalog, default_visual_plan, materialize_visual_assets
    root = Path(output_dir); manifest_path = root / "assets" / "manifest.json"; manifest_path.parent.mkdir(parents=True, exist_ok=True)
    catalog = capability_catalog or build_visual_capability_catalog(inputs, "verified")
    plan = visual_plan if visual_plan is not None else default_visual_plan(catalog)
    assets = materialize_visual_assets(root, inputs, plan)
    manifest_path.write_text(json.dumps({"schema_version": 2, "visual_plan": plan, "capability_catalog": catalog, "assets": assets}, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(manifest_path)
