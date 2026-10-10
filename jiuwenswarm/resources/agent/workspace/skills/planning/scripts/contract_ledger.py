"""Shared, versioned commitments for the method and experiment planners.

The ledger is deliberately small.  It records identifiers and metric names that
must survive the hand-off between two independently prompted agents; it does
not try to replace either rich planning artifact.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
from typing import Any


def _items(value: Any) -> list[dict[str, Any]]:
    return [x for x in value or [] if isinstance(x, dict)] if isinstance(value, list) else []


def build_contract_ledger(inputs: dict[str, Any]) -> dict[str, Any]:
    """Allocate stable hypothesis/experiment IDs before either design agent runs."""
    hypotheses: list[dict[str, str]] = []
    for index, raw in enumerate(_items(inputs.get("hypotheses")), start=1):
        hypothesis_id = str(raw.get("id") or f"H{index}").strip().upper()
        if not hypothesis_id:
            continue
        hypotheses.append({
            "hypothesis_id": hypothesis_id,
            "experiment_id": f"EXP-{hypothesis_id}-MAIN",
        })
    return {
        "version": 1,
        "hypotheses": hypotheses,
        "metric_names": [],
        "comparisons": [],
        "decisions": [],
    }


def _metric_names(method_design: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for section in ("innovation_points", "hypothesis_coverage"):
        for item in _items(method_design.get(section)):
            evidence = item.get("evidence_metric")
            if not isinstance(evidence, list):
                continue
            for criterion in _items(evidence):
                name = criterion.get("metric_name")
                if isinstance(name, str) and name.strip() and name.strip() not in names:
                    names.append(name.strip())
    return names


def update_contract_ledger(
    ledger: dict[str, Any], *, method_design: dict[str, Any] | None = None,
    experiment_plan: dict[str, Any] | None = None, decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a new ledger containing only explicit commitments from artifacts."""
    updated = deepcopy(ledger)
    updated["version"] = int(updated.get("version", 0)) + 1
    if isinstance(method_design, dict):
        updated["metric_names"] = _metric_names(method_design)
    if isinstance(experiment_plan, dict):
        primary = [x.strip() for x in experiment_plan.get("primary_experiments") or []
                   if isinstance(x, str) and x.strip()]
        updated["primary_experiments"] = primary
        metrics = experiment_plan.get("metrics") or []
        updated["planned_metrics"] = [
            x.strip() if isinstance(x, str) else str(x.get("name", "")).strip()
            for x in metrics if isinstance(x, (str, dict))
        ]
    if decisions:
        updated["decisions"] = [x for x in decisions if isinstance(x, dict)]
    return updated


def write_contract_ledger(output_dir: str | Path, ledger: dict[str, Any]) -> str:
    path = Path(output_dir) / "contract_ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path.resolve())
